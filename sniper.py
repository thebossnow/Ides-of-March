# This version uses pure CLOB WebSocket for sub-second new_market detection and aviation data forecasting.
"""
weather_market_sniper.py - Real-time WebSocket sniper for Polymarket temperature markets.

Architecture:
    1. FORECAST CACHE THREAD: Runs every FORECAST_REFRESH_INTERVAL_S (default 5 min),
       fetching BOTH standard GFS/ECMWF AND Google DeepMind GraphCast (via Open-Meteo
       gfs_graphcast025 model) ensemble forecasts, plus live METAR observations from
       AviationWeather.gov for all tracked cities. GraphCast is weighted more heavily
       than standard GFS when blending forecasts because it consistently outperforms
       traditional NWP models on 1-10 day temperature forecasts (see: Lam et al. 2023,
       "Learning skillful medium-range global weather forecasting", Science).
       Results are stored in thread-safe dicts so that when a new_market event arrives,
       blended probabilities are already computed and ready (zero forecast-fetch latency).

       # Future improvement: add Google WeatherNext 2 via Google Vertex AI / Earth Engine
       # for even better accuracy, especially on 0-3 day horizons where WeatherNext 2
       # shows further gains over GraphCast on surface temperature variables.

    2. WEBSOCKET LISTENER THREAD: Maintains a persistent connection to the Polymarket
       CLOB Market WebSocket at wss://ws-subscriptions-clob.polymarket.com/ws/market.
       Subscribes with {"type": "market", "custom_feature_enabled": true} to receive
       new_market events. On each qualifying temperature market, it instantly reads
       the pre-cached forecast, evaluates the top 2 brackets, and places GTC orders
       via executor.place_gtc_order(). No polling. No 30-minute scans. Pure push.

    Target: <500ms from new_market WebSocket event to order submission (the forecast
    is already cached; the only latency is order signing + HTTP POST to CLOB).

Dependencies (beyond what the bot already uses):
    pip install websockets --break-system-packages

Usage:
    from weather_market_sniper import start_weather_sniper, stop_weather_sniper

    # In bot.py, after initializing notifier and other components:
    start_weather_sniper(notifier=notifier)

    # On shutdown:
    stop_weather_sniper()
"""

# Load .env so OPENMETEO_API_KEY etc. are available regardless of launch method
from dotenv import load_dotenv
load_dotenv()

import asyncio
import os
import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional

import requests
import requests_cache
from retry_requests import retry as _retry_requests

from markets import parse_market_metadata, _identify_city, get_book_asks
from executor import place_gtc_order, place_buy_order, DRY_RUN
from logger import log_trade
from strategy import ENTRY_THRESHOLD
from positions import record_entry, is_token_traded_today
from strategy import (
    forecast_probability,
    bayesian_metar_probability,
    find_edge,
    should_trade,
    convert_forecast_to_market_unit,
)
from risk_manager_v2 import get_safe_position_size
from weather_v2 import get_forecast, get_forecast_low, CITIES, _resolve_coords
from aviation_weather import get_current_metar_temps
from weather import AVIATION_ICAO
from notifier import TelegramNotifier

logger = logging.getLogger(__name__)


# ==========================================================================
# Configuration
# ==========================================================================

# --- WebSocket ---
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Subscribe payload: "custom_feature_enabled: true" enables new_market events.
# This is the ONLY detection mechanism. No polling, no Gamma API scanning.
SUBSCRIBE_PAYLOAD = {
    "type": "market",
    "custom_feature_enabled": True,
}

# WebSocket keepalive PING every 50 seconds (server timeout is ~60s)
WS_PING_INTERVAL_S = 50
WS_RECV_TIMEOUT_S = 300  # data silence before forced reconnect (PING/PONG handles dead conns)

# Reconnection with exponential backoff
INITIAL_RECONNECT_DELAY_S = 1.0
MAX_RECONNECT_DELAY_S = 60.0
RECONNECT_BACKOFF_FACTOR = 2.0

# Liquidity polling for new markets: poll intervals (seconds), give up after 30 min.
# Fast-path: WS price_change event can trigger an immediate check before first poll.
LIQUIDITY_POLL_SCHEDULE_S = [5, 15, 30, 60, 120, 300, 300, 300]
LIQUIDITY_TIMEOUT_S = 1800  # 30 minutes

# --- Forecast cache ---
FORECAST_REFRESH_INTERVAL_S = 300  # 5 minutes
FORECAST_DAYS = 10

# Paid Open-Meteo API key (loads from .env via executor.py dotenv call).
# Present → requests go to customer-api host with apikey param (high quota).
# Missing → falls back to free endpoint (subject to daily/monthly limits).
OPENMETEO_API_KEY = os.environ.get("OPENMETEO_API_KEY")

# --- GraphCast configuration ---
# Google DeepMind's GraphCast ML weather model, served free via Open-Meteo.
# Uses the GFS-initialized GraphCast at 0.25-degree resolution ("gfs_graphcast025").
# GraphCast outperforms traditional GFS/ECMWF on 1-10 day temperature forecasts
# because it learns nonlinear atmospheric dynamics from decades of ERA5 reanalysis
# data, capturing patterns that physics-based NWP models miss (especially for
# extreme heat/cold events and tropical cities with poor radiosonde coverage).
#
# API: https://api.open-meteo.com/v1/gfs?models=gfs_graphcast025
# Docs: https://open-meteo.com/en/docs/gfs-api
# Paper: Lam et al., Science 2023 - "Learning skillful medium-range global weather forecasting"
GRAPHCAST_API_URL = "https://customer-api.open-meteo.com/v1/gfs"
GRAPHCAST_MODEL = "gfs_graphcast025"

# Blending weight: how much to trust GraphCast vs standard GFS.
# 0.65 means 65% GraphCast + 35% standard GFS for the blended forecast.
# GraphCast earns the higher weight because:
#   - Lower RMSE on 2m temperature at 1-10 day lead times (Lam et al. 2023)
#   - Better at capturing temperature extremes (tails of the distribution)
#   - More stable run-to-run (less "forecast jumping" between model cycles)
# We keep 35% GFS weight as a hedge because:
#   - GraphCast is initialized FROM GFS, so a bad GFS init propagates
#   - GFS has better resolution for coastal/mountain micro-climates
#   - GraphCast can occasionally produce unrealistic values at 10+ day horizons
# --- ECMWF HRES configuration ---
# ECMWF's high-resolution deterministic forecast (IFS, 0.25-degree).
# Consistently the best single-model NWP forecast globally. Using the
# Open-Meteo ecmwf_ifs025 model which provides ECMWF HRES data for free.
# API: https://api.open-meteo.com/v1/ecmwf?models=ecmwf_ifs025
ECMWF_API_URL = "https://customer-api.open-meteo.com/v1/ecmwf"
ECMWF_MODEL = "ecmwf_ifs025"

# 3-way blending weights: ECMWF HRES is the anchor, GraphCast adds ML skill,
# GFS is a hedge. Weights sum to 1.0.
#   ECMWF:     Best physics-based NWP, gold standard for 1-10 day
#   GraphCast: Best ML model, excels at extremes and tropical cities
#   GFS:       Independent signal, good coastal/mountain micro-climates
ECMWF_WEIGHT = 0.40
GRAPHCAST_WEIGHT = 0.35
GFS_WEIGHT = 0.25  # 1.0 - ECMWF_WEIGHT - GRAPHCAST_WEIGHT

# --- Trading safety ---
MAX_USD_PER_NEW_MARKET = 25.0
MAX_USD_PER_BRACKET = 25.0
SNIPER_BANKROLL = 0.0  # Fallback: $0 so Kelly sizes to nothing if live fetch fails

# --- Entry price bounds (0.01 to 0.40) ---
MIN_ENTRY_PRICE = 0.01
MAX_ENTRY_PRICE = 0.80

# Minimum model probability to consider a bracket
MIN_BRACKET_PROB = 0.30

# --- CTF split/merge strategy (gated; default OFF) ---
# When SNIPER_SPLIT_ENABLED=true AND ctf.DRY_RUN=false, the sniper will:
#   1. Detect when a YES bid sits above (model_prob + SPLIT_MIN_EDGE).
#   2. CTF.splitPosition for $X to mint matched YES+NO at par.
#   3. Sell YES into the overpriced bid (FOK), keep NO at implied (1-bid).
# Otherwise it falls through to the existing GTC ladder (BUY YES).
SNIPER_SPLIT_ENABLED = os.getenv("SNIPER_SPLIT_ENABLED", "false").lower() in ("true", "1", "yes")
SPLIT_MIN_EDGE       = float(os.getenv("SPLIT_MIN_EDGE", "0.05"))   # YES bid must be ≥ this much above model
MAX_USD_PER_SPLIT    = float(os.getenv("MAX_USD_PER_SPLIT", "10.0"))


# ==========================================================================
# Global state (thread-safe)
# ==========================================================================

# Shared requests-cache + retry session for all Open-Meteo batch calls.
# Caches responses for 1 hour so repeated refresh cycles within the TTL
# are served from disk — zero HTTP calls, zero 429s.
_om_cache_session = requests_cache.CachedSession(
    '/root/sniperweatherbot/.om_cache', expire_after=3600
)
_om_session = _retry_requests(_om_cache_session, retries=5, backoff_factor=0.5)

# Standard GFS/ECMWF forecast cache: { city_key: { date_str: temp_celsius } }
_forecast_cache: dict = {}
_forecast_cache_lock = threading.Lock()

# GraphCast forecast cache (separate from GFS so we can blend at read time):
# { city_key: { date_str: temp_celsius } }
_graphcast_cache: dict = {}
_graphcast_cache_lock = threading.Lock()

# ECMWF HRES forecast cache: { city_key: { date_str: temp_celsius } }
_ecmwf_cache: dict = {}
_ecmwf_cache_lock = threading.Lock()

_metar_cache: dict = {}
_metar_cache_lock = threading.Lock()

_last_forecast_refresh: float = 0.0

_sniped_markets: dict = {}  # condition_id -> epoch float; pruned after 24 h
_sniped_lock = threading.Lock()

# Pending snipes awaiting order-book liquidity.
# token_id -> {signals, market_data, slug, question, condition_id, queued_at}
_pending_snipes: dict = {}
_pending_lock = threading.Lock()

_shutdown_flag = threading.Event()
_shutdown_event: Optional[asyncio.Event] = None
_sniper_thread: Optional[threading.Thread] = None
_forecast_thread: Optional[threading.Thread] = None

_notifier = None


# ==========================================================================
# Entry price calculation (strictly 0.01 to 0.50)
# ==========================================================================

def _fetch_best_ask(token_id: str) -> float | None:
    """
    One-shot snapshot of the best (lowest) ask for this token. Returns None if
    the book is empty or the fetch fails. Used for peg-to-book entry pricing so
    we don't bid above observed market consensus on markets that already have
    sellers at listing time.
    """
    try:
        book_asks = get_book_asks(token_id)
    except Exception as e:
        logger.debug(f"_fetch_best_ask({token_id[:12]}...) failed: {e}")
        return None
    if not book_asks:
        return None
    try:
        return float(book_asks[0][0])
    except (TypeError, ValueError, IndexError):
        return None


def _fetch_best_bid(token_id: str) -> float | None:
    """
    One-shot snapshot of the best (highest) bid for this token.
    Used by the CTF split-and-sell path to detect when a YES bid is
    overpriced relative to the model. Returns None on book-empty / fetch-fail.
    """
    try:
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=5,
        )
        resp.raise_for_status()
        bids = (resp.json() or {}).get("bids") or []
    except Exception as e:
        logger.debug(f"_fetch_best_bid({token_id[:12]}...) failed: {e}")
        return None
    if not bids:
        return None
    try:
        # CLOB bids array is sorted descending by price; first entry is the best
        return float(bids[0]["price"])
    except (KeyError, TypeError, ValueError):
        return None


def _compute_entry_price(model_prob: float, best_ask: float | None = None) -> float:
    """
    Dynamic entry pricing with peg-to-book.

    Base price:
        entry_price = model_prob * DISCOUNT_FACTOR   (DISCOUNT_FACTOR=0.65)

    Peg-to-book: if a best_ask is observable on the orderbook, cap the entry
    price at best_ask - 1 tick. This prevents two failure modes on new markets:
      1. Bidding above real sellers (we'd fill instantly at an overpaid price
         instead of our edge-discount price).
      2. Bidding far below a real consensus ask and never getting filled
         while the true fair value drifts away from our stale model discount.

    The model-discount price remains the ceiling: we never pay MORE than our
    edge-adjusted target just because the book says to. We only step DOWN to
    stay inside the spread.

    Examples (model discount = $0.26):
        best_ask = None    -> $0.26 (no book, use model)
        best_ask = $0.40   -> $0.26 (model cheaper, unchanged)
        best_ask = $0.25   -> $0.24 (step inside spread; best_ask - 1 tick)
        best_ask = $0.10   -> $0.09 (book much tighter; peg down)
    """
    DISCOUNT_FACTOR = 0.65
    TICK = 0.01

    model_price = round(model_prob * DISCOUNT_FACTOR, 2)
    price = model_price

    if best_ask is not None and best_ask > 0:
        peg_price = round(best_ask - TICK, 2)
        if peg_price < price:
            price = peg_price

    # Clamp to valid range
    price = max(MIN_ENTRY_PRICE, min(MAX_ENTRY_PRICE, price))

    return price


_bankroll_cache_value: float = 0.0
_bankroll_cache_ts: float = 0.0
_BANKROLL_CACHE_TTL = 120  # seconds; re-fetch at most once per 2 min


def _get_live_sniper_bankroll() -> float:
    """
    Fetch live USDC collateral balance for Kelly sizing.
    Cached for _BANKROLL_CACHE_TTL seconds so repeated per-bracket calls
    during a single new_market event don't each wait 1s for the API.
    Returns 0.0 on failure so Kelly sizes to $0 (no trades on fake bankroll).
    """
    global _bankroll_cache_value, _bankroll_cache_ts
    if _bankroll_cache_value > 0 and time.monotonic() - _bankroll_cache_ts < _BANKROLL_CACHE_TTL:
        return _bankroll_cache_value

    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    from executor import get_client, SIG_TYPE
    try:
        client = get_client()
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=SIG_TYPE,
        )
        client.update_balance_allowance(params)
        time.sleep(1)
        result = client.get_balance_allowance(params)
        if isinstance(result, dict) and "balance" in result:
            bal = int(result["balance"]) / 1_000_000
            if bal >= 0:
                logger.debug(f"Sniper live bankroll: ${bal:.2f}")
                _bankroll_cache_value = round(bal, 2)
                _bankroll_cache_ts = time.monotonic()
                return _bankroll_cache_value
    except Exception as e:
        logger.warning(f"_get_live_sniper_bankroll failed: {e}")
    logger.error("Sniper bankroll fetch failed. Returning $0 (no trades).")
    return SNIPER_BANKROLL  # 0.0 — Kelly sizes to nothing


# ==========================================================================
# Background forecast cache refresh (Open-Meteo + METAR)
# ==========================================================================

def _batch_fetch_openmeteo(url: str, days: int, model: Optional[str] = None) -> dict:
    """
    Fetches daily max temp for ALL tracked cities in a single Open-Meteo request.

    Open-Meteo accepts repeated latitude/longitude params (one pair per city)
    and returns a JSON array — one element per location in the same order.
    Combined with requests_cache this means the 18-city × 3-model nested loops
    that previously issued 54 HTTP calls now issue 2 (GraphCast + ECMWF).

    Returns:
        { city_key: { date_str: temp_celsius } }  — empty dict on total failure.
    """
    city_keys = list(CITIES.keys())
    params = [
        ("daily", "temperature_2m_max"),
        ("temperature_unit", "celsius"),
        ("forecast_days", min(days, 16)),
    ]
    for ck in city_keys:
        _c = _resolve_coords(ck)
        params.append(("latitude",  _c["lat"]))
        params.append(("longitude", _c["lon"]))
    if model:
        params.append(("models", model))
    if OPENMETEO_API_KEY:
        params.append(("apikey", OPENMETEO_API_KEY))

    try:
        resp = _om_session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Open-Meteo batch fetch failed (url={url} model={model}): {e}")
        return {}

    if isinstance(data, dict):
        data = [data]

    result = {}
    for i, city_key in enumerate(city_keys):
        if i >= len(data):
            break
        daily = data[i].get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])
        result[city_key] = {
            d: float(t) for d, t in zip(dates, temps) if t is not None
        }
    return result


def _fetch_graphcast_forecast(city_key: str, days: int = 10) -> Optional[dict]:
    """Single-city GraphCast fetch — used only for blocking cache-miss fallback."""
    if city_key not in CITIES:
        return None
    city = _resolve_coords(city_key)
    params = {
        "latitude": city["lat"], "longitude": city["lon"],
        "daily": "temperature_2m_max", "temperature_unit": "celsius",
        "timezone": city["tz"], "forecast_days": min(days, 16),
        "models": GRAPHCAST_MODEL,
    }
    if OPENMETEO_API_KEY:
        params["apikey"] = OPENMETEO_API_KEY
    try:
        resp = _om_session.get(GRAPHCAST_API_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return dict(zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]))
    except Exception as e:
        logger.debug(f"GraphCast fetch failed for {city_key}: {e}")
        return None



def _fetch_ecmwf_forecast(city_key: str, days: int = 10) -> Optional[dict]:
    """Single-city ECMWF fetch — used only for blocking cache-miss fallback."""
    if city_key not in CITIES:
        return None
    city = _resolve_coords(city_key)
    params = {
        "latitude": city["lat"], "longitude": city["lon"],
        "daily": "temperature_2m_max", "temperature_unit": "celsius",
        "timezone": city["tz"], "forecast_days": min(days, 10),
        "models": ECMWF_MODEL,
    }
    if OPENMETEO_API_KEY:
        params["apikey"] = OPENMETEO_API_KEY
    try:
        resp = _om_session.get(ECMWF_API_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return dict(zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]))
    except Exception as e:
        logger.debug(f"ECMWF fetch failed for {city_key}: {e}")
        return None

def _refresh_all_forecasts() -> None:
    """
    Fetches fresh forecasts for ALL tracked cities from multiple sources:
      Phase 1a: Standard GFS/ECMWF via Open-Meteo /v1/forecast (weather.get_forecast)
      Phase 1b: GraphCast ML model via Open-Meteo /v1/gfs?models=gfs_graphcast025
      Phase 2:  Live METAR observations from AviationWeather.gov

    GraphCast and GFS are cached separately so that get_best_temperature_brackets()
    can blend them with configurable weights at evaluation time. This gives us the
    best of both worlds: GraphCast's superior ML-based prediction skill plus GFS's
    reliable physics-based baseline as a hedge.

    Called every FORECAST_REFRESH_INTERVAL_S (5 min) by the background thread.
    """
    global _last_forecast_refresh

    logger.info("Sniper forecast refresh: updating all cities...")
    refresh_start = time.monotonic()

    # --- Phase 1a: Standard GFS/ECMWF forecasts (via weather.get_forecast) ---
    new_gfs_cache = {}
    gfs_count = 0
    for city_key in CITIES:
        try:
            forecast_data = get_forecast(city_key, days=FORECAST_DAYS)
            if forecast_data:
                new_gfs_cache[city_key] = forecast_data
                gfs_count += 1
        except Exception as e:
            logger.debug(f"GFS forecast refresh failed for {city_key}: {e}")

    with _forecast_cache_lock:
        _forecast_cache.update(new_gfs_cache)

    # --- Phase 1b: GraphCast batch (1 request for all cities) ---
    try:
        gc_all = _batch_fetch_openmeteo(
            GRAPHCAST_API_URL, days=FORECAST_DAYS, model=GRAPHCAST_MODEL
        )
        gc_count = len(gc_all)
        with _graphcast_cache_lock:
            _graphcast_cache.update(gc_all)
    except Exception as e:
        logger.warning(f"GraphCast batch refresh failed: {e}")
        gc_count = 0

    # --- Phase 1c: ECMWF batch (1 request for all cities) ---
    try:
        ecmwf_all = _batch_fetch_openmeteo(
            ECMWF_API_URL, days=min(FORECAST_DAYS, 10), model=ECMWF_MODEL
        )
        ecmwf_count = len(ecmwf_all)
        with _ecmwf_cache_lock:
            _ecmwf_cache.update(ecmwf_all)
    except Exception as e:
        logger.warning(f"ECMWF batch refresh failed: {e}")
        ecmwf_count = 0

    # --- Phase 2: Live METAR observations (AviationWeather.gov) ---
    all_icao = list(AVIATION_ICAO.values())
    icao_to_city = {icao: city_key for city_key, icao in AVIATION_ICAO.items()}

    metar_data = {}
    metar_count = 0
    if all_icao:
        try:
            raw_metar = get_current_metar_temps(all_icao)
            for icao, obs in raw_metar.items():
                city_key = icao_to_city.get(icao)
                if city_key:
                    metar_data[city_key] = obs
                    metar_count += 1
        except Exception as e:
            logger.warning(f"METAR batch fetch failed: {e}")

    with _metar_cache_lock:
        _metar_cache.clear()
        _metar_cache.update(metar_data)
    elapsed = time.monotonic() - refresh_start
    _last_forecast_refresh = time.monotonic()
    logger.info(
        f"Sniper forecast refresh complete: "
        f"GFS={gfs_count}/{len(CITIES)}, "
        f"GraphCast={gc_count}/{len(CITIES)}, "
        f"ECMWF={ecmwf_count}/{len(CITIES)}, "
        f"METAR={metar_count} obs in {elapsed:.1f}s"
    )


def _forecast_refresh_loop() -> None:
    """
    Background thread: refreshes forecasts + METAR every FORECAST_REFRESH_INTERVAL_S.
    First refresh happens immediately so the cache is warm before any events arrive.
    """
    logger.info(
        f"Sniper forecast thread started (refresh every {FORECAST_REFRESH_INTERVAL_S}s)"
    )
    while not _shutdown_flag.is_set():
        try:
            _refresh_all_forecasts()
        except Exception as e:
            logger.error(f"Sniper forecast refresh crashed: {e}", exc_info=True)
        _shutdown_flag.wait(timeout=FORECAST_REFRESH_INTERVAL_S)
    logger.info("Sniper forecast thread stopped")


# ==========================================================================
# Market qualification filter
# ==========================================================================

def _is_qualifying_temperature_market(question: str) -> bool:
    """
    Returns True if the market question is a daily high OR low temperature
    market. Must contain "highest temperature" or "lowest temperature"
    (case-insensitive) AND reference a known city from markets.CITY_ALIASES.
    """
    q_lower = question.lower()
    if "highest temperature" not in q_lower and "lowest temperature" not in q_lower:
        return False
    if _identify_city(question) is None:
        return False
    return True


def _get_cached_forecast(city: str) -> Optional[dict]:
    """Returns the pre-cached forecast dict for a city, or None."""
    with _forecast_cache_lock:
        return _forecast_cache.get(city)


def _get_cached_graphcast(city: str) -> Optional[dict]:
    """Returns the pre-cached GraphCast forecast dict for a city, or None."""
    with _graphcast_cache_lock:
        return _graphcast_cache.get(city)


def _get_cached_ecmwf(city: str) -> Optional[dict]:
    """Returns the pre-cached ECMWF HRES forecast dict for a city, or None."""
    with _ecmwf_cache_lock:
        return _ecmwf_cache.get(city)


def _get_cached_metar(city: str) -> Optional[dict]:
    """Returns the pre-cached METAR observation for a city, or None."""
    with _metar_cache_lock:
        return _metar_cache.get(city)


# ==========================================================================
# Top-2 bracket evaluation (uses pre-cached forecasts)
# ==========================================================================

def get_best_temperature_brackets(market_data: dict) -> list[dict]:
    """
    Evaluates a new temperature market using PRE-CACHED forecast data
    (no HTTP calls at snipe time) and returns up to 2 bracket signals.

    Logic:
    1. Parse market metadata (city, date, bucket bounds, token IDs)
    2. Read forecast from pre-warmed cache (instant, no HTTP)
    3. If METAR data available, use it to refine same-day forecasts
    4. Compute model probability via strategy.forecast_probability()
    5. If prob > MIN_BRACKET_PROB (0.30), generate a signal
    6. Check for sibling brackets in the event; return top 2 by prob

    Returns:
        List of 0-2 signal dicts, each with:
            tokenId, side, price (0.01-0.50), size (USDC), prob, edge, meta
    """
    meta = parse_market_metadata(market_data)
    if meta is None:
        logger.debug("Sniper: could not parse metadata from new market")
        return []

    city = meta["city"]
    date_str = meta["date"]
    bucket_low = meta["bucket_low"]
    bucket_high = meta["bucket_high"]
    unit = meta["unit"]
    yes_token_id = meta["yes_token_id"]
    market_type = meta.get("market_type", "highest")

    if not yes_token_id or len(yes_token_id) < 10:
        logger.debug(f"Sniper: no valid YES token for {market_data.get('slug', '?')}")
        return []

    if is_token_traded_today(yes_token_id):
        logger.info(f"Sniper: already traded {yes_token_id[:16]}... today, skip")
        return []

    if market_type == "lowest":
        # Lowest-temp markets are not pre-warmed in the 3-model blend cache.
        # Do a single blocking get_forecast_low() call (uses weather_v2's own
        # 3-model ensemble + disk cache, so hot path is still cheap).
        try:
            low_data = get_forecast_low(city, days=FORECAST_DAYS)
        except Exception as e:
            logger.error(f"Sniper: get_forecast_low failed for {city}: {e}")
            return []
        if not low_data or date_str not in low_data:
            logger.debug(f"Sniper: no low forecast for {city} on {date_str}")
            return []
        forecast_celsius = low_data[date_str]
        logger.debug(
            f"Sniper LOW {city} {date_str}: get_forecast_low={forecast_celsius:.1f}C"
        )
    else:
        # --- Read from PRE-CACHED forecasts (no HTTP call!) ---
        # 3-way blend: ECMWF HRES (0.40) + GraphCast (0.35) + GFS (0.25).
        # If a source is missing, re-normalize weights across available sources.
        gfs_data = _get_cached_forecast(city)
        gc_data = _get_cached_graphcast(city)
        ecmwf_data = _get_cached_ecmwf(city)

        gfs_temp = gfs_data.get(date_str) if gfs_data else None
        gc_temp = gc_data.get(date_str) if gc_data else None
        ecmwf_temp = ecmwf_data.get(date_str) if ecmwf_data else None

        # Cache miss on all three: fallback blocking GFS fetch
        if gfs_temp is None and gc_temp is None and ecmwf_temp is None:
            logger.warning(f"Sniper: cache miss for {city} {date_str}, blocking fetch")
            try:
                gfs_data = get_forecast(city, days=FORECAST_DAYS)
            except Exception as e:
                logger.error(f"Sniper: blocking forecast fetch failed for {city}: {e}")
                return []
            if gfs_data is None or date_str not in gfs_data:
                logger.debug(f"Sniper: no forecast for {city} on {date_str}")
                return []
            with _forecast_cache_lock:
                _forecast_cache[city] = gfs_data
            gfs_temp = gfs_data[date_str]

        # Weighted blend with dynamic re-normalization for missing sources
        sources = []
        if ecmwf_temp is not None:
            sources.append(("ECMWF", ecmwf_temp, ECMWF_WEIGHT))
        if gc_temp is not None:
            sources.append(("GC", gc_temp, GRAPHCAST_WEIGHT))
        if gfs_temp is not None:
            sources.append(("GFS", gfs_temp, GFS_WEIGHT))

        total_weight = sum(w for _, _, w in sources)
        forecast_celsius = sum(t * (w / total_weight) for _, t, w in sources)

        blend_parts = " + ".join(
            f"{name}={t:.1f}C*{w/total_weight:.2f}" for name, t, w in sources
        )
        logger.debug(
            f"Sniper blend {city} {date_str}: {blend_parts} = {forecast_celsius:.1f}C"
        )

    forecast_in_unit = convert_forecast_to_market_unit(forecast_celsius, unit)

    # --- Bayesian METAR update for same-day markets ---
    # Instead of crude "if METAR > forecast, use METAR", we compute a
    # proper conditional probability: P(max in bucket | observed, time).
    # This is where the real money is on same-day markets.
    # Skip Bayesian METAR for lowest-temp markets — the update model assumes
    # observed temp informs the daily MAX, not MIN. For daily-low markets
    # rely on the ensemble forecast only.
    metar_obs = _get_cached_metar(city) if market_type != "lowest" else None
    use_bayesian = False

    if metar_obs is not None:
        metar_temp_c = metar_obs.get("temp_c")
        if metar_temp_c is not None:
            # Get local hour for the city
            try:
                from zoneinfo import ZoneInfo
                city_tz = ZoneInfo(CITIES[city]["tz"])
                local_now = datetime.now(city_tz)
                local_hour = local_now.hour + local_now.minute / 60.0

                # Only use Bayesian updates for same-day markets
                today_local = local_now.strftime("%Y-%m-%d")
                if date_str == today_local:
                    observed_in_unit = convert_forecast_to_market_unit(metar_temp_c, unit)
                    use_bayesian = True
                    logger.info(
                        f"Sniper: Bayesian METAR update for {city} {date_str} | "
                        f"observed={observed_in_unit:.1f}{unit} @ {local_hour:.1f}h | "
                        f"forecast={forecast_in_unit:.1f}{unit}"
                    )
            except Exception as e:
                logger.debug(f"Sniper: Bayesian METAR failed for {city}: {e}")

    # --- Compute probability ---
    if use_bayesian:
        prob = bayesian_metar_probability(
            forecast_temp=forecast_in_unit,
            observed_temp=observed_in_unit,
            local_hour=local_hour,
            bucket_low=bucket_low,
            bucket_high=bucket_high,
            unit=unit,
            market_date=date_str,
        )
        # Also compute the naive probability for logging comparison
        naive_prob = forecast_probability(
            forecast_temp=forecast_in_unit,
            bucket_low=bucket_low,
            bucket_high=bucket_high,
            unit=unit,
            market_date=date_str,
            city=city,
        )
        logger.info(
            f"Sniper: Bayesian prob={prob:.1%} vs naive={naive_prob:.1%} "
            f"for {city} [{bucket_low},{bucket_high}]{unit}"
        )
    else:
        prob = forecast_probability(
            forecast_temp=forecast_in_unit,
            bucket_low=bucket_low,
            bucket_high=bucket_high,
            unit=unit,
            market_date=date_str,
            city=city,
        )

    # --- Build signal for this bracket if it qualifies ---
    signals = []

    if prob >= MIN_BRACKET_PROB:
        best_ask = _fetch_best_ask(yes_token_id)
        entry_price = _compute_entry_price(prob, best_ask=best_ask)
        if best_ask is not None:
            logger.info(
                f"Sniper peg-to-book: {city} {date_str} "
                f"[{bucket_low},{bucket_high}]{unit} | "
                f"best_ask=${best_ask:.2f} model=${_compute_entry_price(prob):.2f} "
                f"-> entry=${entry_price:.2f}"
            )
        edge = find_edge(prob, entry_price)
        if should_trade(edge, forecast_prob=prob, market_date=date_str):
            size = get_safe_position_size(_get_live_sniper_bankroll(), edge, prob, entry_price)
            size = min(size, MAX_USD_PER_BRACKET, MAX_USD_PER_NEW_MARKET)
            signals.append({
                "tokenId": yes_token_id,
                "side": "BUY",
                "price": entry_price,
                "size": size,
                "prob": prob,
                "edge": edge,
                "meta": meta,
                "forecast_celsius": forecast_celsius,
                "forecast_in_unit": forecast_in_unit,
            })
            logger.info(
                f"Sniper SIGNAL: {city} {date_str} | "
                f"[{bucket_low},{bucket_high}]{unit} | "
                f"forecast={forecast_in_unit:.1f}{unit} | prob={prob:.1%} | "
                f"entry=${entry_price:.2f} | edge={edge:+.1%} | size=${size:.2f}"
            )
            if _notifier:
                _notifier.notify_new_weather_market(
                    city=city,
                    date_str=date_str,
                    forecast_temp_c=forecast_in_unit,
                    brackets=[f"[{bucket_low},{bucket_high}]{unit}"],
                    edge=edge,
                    skip_reason="TRADE PLACED (edge met)",
                    slug=meta.get("slug", meta.get("conditionId", ""))
                )
        else:
            logger.info(
                f"Sniper: insufficient edge for {city} {date_str} "
                f"[{bucket_low},{bucket_high}]{unit} | prob={prob:.1%} "
                f"entry=${_compute_entry_price(prob):.2f} edge={find_edge(prob, _compute_entry_price(prob)):+.1%}"
            )
            if _notifier:
                _notifier.notify_new_weather_market(
                    city=city,
                    date_str=date_str,
                    forecast_temp_c=forecast_in_unit,
                    brackets=[f"[{bucket_low},{bucket_high}]{unit}"],
                    edge=edge,
                    skip_reason=f"INSUFFICIENT EDGE ({edge:+.1%} < threshold)",
                    slug=meta.get("slug", meta.get("conditionId", ""))
                )
    else:
        logger.info(
            f"Sniper: below threshold for {city} {date_str} "
            f"[{bucket_low},{bucket_high}]{unit} | prob={prob:.1%} < {MIN_BRACKET_PROB:.0%}"
        )
        if _notifier:
            _notifier.notify_new_weather_market(
                city=city,
                date_str=date_str,
                forecast_temp_c=forecast_in_unit,
                brackets=[f"[{bucket_low},{bucket_high}]{unit}"],
                edge=0.0,
                skip_reason=f"PROB BELOW MIN ({prob:.1%} < {MIN_BRACKET_PROB:.0%})",
                slug=meta.get("slug", meta.get("conditionId", ""))
            )

    # --- Check for sibling brackets in multi-market events ---
    related_markets = market_data.get("markets", [])
    if related_markets and isinstance(related_markets, list):
        for related in related_markets:
            if related is market_data:
                continue
            r_meta = parse_market_metadata(related)
            if r_meta is None:
                continue
            if r_meta["city"] != city or r_meta["date"] != date_str:
                continue
            r_token = r_meta["yes_token_id"]
            if not r_token or len(r_token) < 10:
                continue
            if is_token_traded_today(r_token):
                continue

            if use_bayesian:
                r_prob = bayesian_metar_probability(
                    forecast_temp=forecast_in_unit,
                    observed_temp=observed_in_unit,
                    local_hour=local_hour,
                    bucket_low=r_meta["bucket_low"],
                    bucket_high=r_meta["bucket_high"],
                    unit=r_meta["unit"],
                    market_date=date_str,
                )
            else:
                r_prob = forecast_probability(
                    forecast_temp=forecast_in_unit,
                    bucket_low=r_meta["bucket_low"],
                    bucket_high=r_meta["bucket_high"],
                    unit=r_meta["unit"],
                    market_date=date_str,
                    city=city,
                )
            if r_prob < MIN_BRACKET_PROB:
                continue
            r_best_ask = _fetch_best_ask(r_token)
            r_entry = _compute_entry_price(r_prob, best_ask=r_best_ask)
            r_edge = find_edge(r_prob, r_entry)
            if not should_trade(r_edge, forecast_prob=r_prob, market_date=date_str):
                continue
            r_size = get_safe_position_size(_get_live_sniper_bankroll(), r_edge, r_prob, r_entry)
            r_size = min(r_size, MAX_USD_PER_BRACKET)
            signals.append({
                "tokenId": r_token,
                "side": "BUY",
                "price": r_entry,
                "size": r_size,
                "prob": r_prob,
                "edge": r_edge,
                "meta": r_meta,
                "forecast_celsius": forecast_celsius,
                "forecast_in_unit": forecast_in_unit,
            })

    # Sort by probability descending, take top 2
    signals.sort(key=lambda s: s["prob"], reverse=True)
    top_signals = signals[:2]

    # Enforce total spend cap across all brackets
    total_allocated = sum(s["size"] for s in top_signals)
    if total_allocated > MAX_USD_PER_NEW_MARKET:
        scale = MAX_USD_PER_NEW_MARKET / total_allocated
        for s in top_signals:
            s["size"] = round(s["size"] * scale, 2)

    return top_signals


# ==========================================================================
# Order execution (reuses executor.py)
# ==========================================================================



# ==========================================================================
# WebSocket message handler
# ==========================================================================

async def _handle_message(raw_msg: str, ws=None) -> None:
    """
    Processes a single WebSocket message from the Polymarket CLOB Market WS.

    Handles two event types:
      new_market    — qualifies market, stores signals in _pending_snipes, kicks off
                      liquidity polling via _poll_for_liquidity().
      price_change  — WS fast-path: if the token is pending, immediately verifies
                      liquidity via REST and fires the ladder without waiting for the
                      next poll interval.

    Orders are NEVER placed here directly. All placement goes through
    _execute_pending_snipe() once REST confirms bids + asks exist.
    """
    try:
        data = json.loads(raw_msg)
    except json.JSONDecodeError:
        logger.debug(f"Sniper: non-JSON message: {raw_msg[:200]}")
        return
    except Exception as e:
        logger.debug(f"Sniper: failed to parse message: {e}")
        return

    if not isinstance(data, dict):
        logger.debug(f"Sniper: non-dict message received (type={type(data)})")
        return

    event_type = data.get("event_type", data.get("type", ""))

    # ---- WS fast-path: price_change or book event on a pending token ----
    if event_type in ("price_change", "book"):
        asset_id = data.get("asset_id", data.get("token_id", ""))
        if asset_id:
            with _pending_lock:
                in_pending = asset_id in _pending_snipes
            if in_pending:
                logger.info(f"Sniper: WS fast-path ({event_type}) for {asset_id[:12]}...")
                asyncio.create_task(_fire_pending_snipe(asset_id))
        return

    # ---- Only process new_market events beyond this point ----
    if event_type != "new_market":
        return

    market_data = data.get("market", data)
    if not isinstance(market_data, dict):
        logger.debug(f"Sniper: market_data is not dict (type={type(market_data)})")
        return

    question = market_data.get("question", "")
    condition_id = market_data.get("conditionId", market_data.get("condition_id", ""))
    slug = market_data.get("slug", "unknown")

    logger.info(f"Sniper: new_market event | slug={slug} | question={question[:120]}")

    # Filter: only daily high temperature markets for tracked cities
    if not _is_qualifying_temperature_market(question):
        logger.info(f"Sniper: not a qualifying temp market, skipping: {slug}")
        return

    # Deduplicate: at most one snipe per conditionId per 24-hour window
    with _sniped_lock:
        _now = time.time()
        expired = [k for k, v in _sniped_markets.items() if _now - v > 86400]
        for k in expired:
            del _sniped_markets[k]
        if condition_id in _sniped_markets:
            logger.info(f"Sniper: already sniped {condition_id[:16]}..., skipping")
            return
        _sniped_markets[condition_id] = _now

    logger.info(f"Sniper: QUALIFYING temperature market! {slug}")

    # ---- Evaluate top 2 brackets (uses pre-cached forecasts, no HTTP) ----
    try:
        signals = get_best_temperature_brackets(market_data)
    except Exception as e:
        logger.error(f"Sniper: bracket evaluation failed for {slug}: {e}")
        _notify_error(f"Bracket eval failed for {slug}: {e}")
        # Roll back dedup so a reconnect can retry
        with _sniped_lock:
            _sniped_markets.pop(condition_id, None)
        return

    if not signals:
        logger.info(f"Sniper: no actionable signal for {slug}")
        return

    logger.info(f"Sniper: {len(signals)} bracket(s) qualifying for {slug}, waiting for liquidity")

    # ---- Pre-cache token metadata (cuts order latency when liquidity arrives) ----
    from executor import precache_token_metadata
    for signal in signals:
        try:
            precache_token_metadata(signal["tokenId"])
        except Exception as e:
            logger.debug(f"Sniper: precache failed for {signal['tokenId'][:16]}: {e}")

    # ---- Queue each bracket token; subscribe to WS orderbook for fast-path ----
    for signal in signals:
        token_id = signal.get("tokenId", "")
        if not token_id:
            continue
        with _pending_lock:
            _pending_snipes[token_id] = {
                "signals": [signal],
                "market_data": market_data,
                "slug": slug,
                "question": question,
                "condition_id": condition_id,
                "queued_at": time.time(),
            }
        await _subscribe_to_order_book(ws, token_id)
        asyncio.create_task(_poll_for_liquidity(token_id))
        logger.info(f"Sniper: queued liquidity poll for {slug} token {token_id[:12]}...")

# ==========================================================================
# ORDER BOOK SUBSCRIPTION (for new temperature markets)
# ==========================================================================

async def _subscribe_to_order_book(ws, token_id: str):
    """Subscribe to order book updates for a specific YES token."""
    if not ws or ws.closed:
        logger.warning(f"Cannot subscribe to order book for {token_id[:12]} - WebSocket not available")
        return

    subscribe_msg = {
        "type": "orderbook",
        "asset_ids": [token_id],
        "custom_feature_enabled": True
    }
    try:
        await ws.send(json.dumps(subscribe_msg))
        logger.info(f"Sniper: subscribed to order book for token {token_id[:12]}...")
    except Exception as e:
        logger.warning(f"Failed to subscribe to order book for {token_id[:12]}: {e}")

# ==========================================================================
# Liquidity detection & deferred order placement
# ==========================================================================

def _check_book_has_liquidity(token_id: str) -> bool:
    """Returns True when the CLOB REST book has at least one bid and one ask."""
    try:
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=5,
        )
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        return len(bids) > 0 and len(asks) > 0
    except Exception as e:
        logger.debug(f"Sniper: REST book check failed for {token_id[:12]}: {e}")
        return False


def _execute_snipe_order(signal: dict, market_data: dict) -> dict:
    """
    Places a single GTC buy order via executor.place_gtc_order().

    GTC is used because brand-new markets have no matching liquidity.
    The order sits on the book waiting for a seller. We wait only 5s
    for a fill to keep latency low, but the order remains active.
    """
    token_id = signal["tokenId"]
    price = signal["price"]
    size_usdc = signal["size"]
    meta = signal["meta"]

    logger.info(
        f"Sniper EXECUTING: BUY {token_id[:16]}... "
        f"@ ${price:.4f} for ${size_usdc:.2f} USDC "
        f"(prob={signal['prob']:.1%} edge={signal['edge']:+.1%})"
    )

    start_ts = time.monotonic()

    result = place_gtc_order(
        token_id=token_id,
        price=price,
        size_usdc=size_usdc,
        wait_seconds=5.0,
    )

    elapsed_ms = (time.monotonic() - start_ts) * 1000
    status = result.get("status", "UNKNOWN")
    logger.info(f"Sniper order done in {elapsed_ms:.0f}ms | status={status}")

    if status != "FAILED":
        try:
            num_shares = size_usdc / price if price > 0 else 0.0
            record_entry(
                token_id=token_id,
                condition_id=meta.get("condition_id", ""),
                slug=market_data.get("slug", "snipe-unknown"),
                city=meta["city"],
                market_date=meta["date"],
                bucket_low=meta["bucket_low"],
                bucket_high=meta["bucket_high"],
                unit=meta["unit"],
                entry_price=price,
                shares=num_shares,
                size_usdc=size_usdc,
                order_id=result.get("order_id", ""),
                question=market_data.get("question", ""),
                forecast_prob=signal["prob"],
                edge=signal["edge"],
            )
            try:
                log_trade(
                    market_slug=market_data.get("slug", "snipe-unknown"),
                    city=meta["city"],
                    date=meta["date"],
                    forecast_temp_c=signal.get("forecast_celsius", 0.0),
                    forecast_temp_market_unit=signal.get("forecast_in_unit", 0.0),
                    market_unit=meta["unit"],
                    bucket_low=meta["bucket_low"],
                    bucket_high=meta["bucket_high"],
                    forecast_prob=signal["prob"],
                    market_price=price,
                    edge=signal["edge"],
                    size_usdc=size_usdc,
                    dry_run=DRY_RUN,
                    order_response=result,
                    question=market_data.get("question", ""),
                )
            except Exception as e:
                logger.warning(f"Sniper: log_trade failed: {e}")
        except Exception as e:
            logger.warning(f"Sniper: failed to record position: {e}")

    return result


def _execute_pending_snipe(token_id: str) -> None:
    """Pops token from pending and places the GTC ladder. No-op if already gone."""
    with _pending_lock:
        entry = _pending_snipes.pop(token_id, None)
    if entry is None:
        return

    slug = entry["slug"]
    question = entry["question"]
    market_data = entry["market_data"]
    signals = entry["signals"]
    elapsed = time.time() - entry["queued_at"]

    logger.info(
        f"Sniper: placing ladder for {slug} token {token_id[:12]}... "
        f"({elapsed:.1f}s after market creation)"
    )

    for i, signal in enumerate(signals):
        bracket_label = f"bracket {i+1}/{len(signals)}"
        try:
            place_gtc_ladder(signal, market_data)
            _notify_snipe(slug, question, signal, "LADDER_PLACED", bracket_label)
        except Exception as e:
            logger.error(f"Sniper: ladder failed for {slug} {bracket_label}: {e}")
            _notify_error(f"Ladder failed for {slug} {bracket_label}: {e}")


async def _fire_pending_snipe(token_id: str) -> None:
    """WS fast-path: verify liquidity via REST then execute. Runs as an asyncio task."""
    loop = asyncio.get_running_loop()
    has_liq = await loop.run_in_executor(None, _check_book_has_liquidity, token_id)
    if has_liq:
        await loop.run_in_executor(None, _execute_pending_snipe, token_id)
    else:
        logger.debug(f"Sniper: fast-path REST check: still no liquidity for {token_id[:12]}")


async def _poll_for_liquidity(token_id: str) -> None:
    """
    Polls CLOB REST until the order book has bids + asks, then places the ladder.

    Schedule: 5s, 15s, 30s, 60s, 2min, 5min, 5min, 5min (cumulative ~24 min).
    Hard cap: LIQUIDITY_TIMEOUT_S (30 min) — gives up and removes from pending.
    The WS fast-path (_fire_pending_snipe) can short-circuit this at any time.
    """
    loop = asyncio.get_running_loop()
    queued_at = time.time()

    for delay in LIQUIDITY_POLL_SCHEDULE_S:
        await asyncio.sleep(delay)

        with _pending_lock:
            if token_id not in _pending_snipes:
                return  # fast-path already executed or cancelled

        elapsed = time.time() - queued_at
        if elapsed > LIQUIDITY_TIMEOUT_S:
            break

        has_liq = await loop.run_in_executor(None, _check_book_has_liquidity, token_id)
        if has_liq:
            logger.info(f"Sniper: poll confirmed liquidity for {token_id[:12]}... after {elapsed:.0f}s")
            await loop.run_in_executor(None, _execute_pending_snipe, token_id)
            return

        logger.debug(f"Sniper: still no liquidity for {token_id[:12]}... ({elapsed:.0f}s elapsed)")

    # 30-minute timeout — abandon this token
    with _pending_lock:
        entry = _pending_snipes.pop(token_id, None)
    if entry:
        logger.warning(
            f"Sniper: 30-min liquidity timeout for {entry['slug']} "
            f"token {token_id[:12]}... — abandoning"
        )
        _notify_error(f"Liquidity timeout (30 min): {entry['slug']} ({token_id[:12]}...)")
        # Remove dedup entry so a future reconnect can retry if market still open
        with _sniped_lock:
            _sniped_markets.pop(entry["condition_id"], None)


# ==========================================================================
# Telegram notifications
# ==========================================================================

def _notify_snipe(slug, question, signal, status, bracket_label):
    """Sends a Telegram alert for a snipe attempt."""
    if _notifier is None:
        return
    try:
        hit = status in ("FILLED", "MATCHED")
        msg = (
            f"SNIPER {'HIT' if hit else 'PLACED'} ({bracket_label})\n"
            f"Market: {question[:80]}\n"
            f"Token: {signal['tokenId'][:16]}...\n"
            f"Prob: {signal['prob']:.1%} | Entry: ${signal['price']:.2f} | "
            f"Size: ${signal['size']:.2f}\n"
            f"Edge: {signal['edge']:+.1%} | Status: {status}"
        )
        _notifier.send_message(msg)
    except Exception as e:
        logger.warning(f"Sniper: Telegram notify failed: {e}")


def _notify_error(error_msg):
    """Sends a Telegram error alert."""
    if _notifier is None:
        return
    try:
        _notifier.notify_error("Weather Sniper", error_msg)
    except Exception:
        pass


# ==========================================================================
# Async WebSocket loop (pure push, no polling)
# ==========================================================================

async def _ws_loop():
    """
    Core async loop: connects to the Polymarket CLOB Market WebSocket,
    subscribes with {"type": "market", "custom_feature_enabled": true},
    and listens for new_market events indefinitely.

    Reconnection uses exponential backoff (1s -> 2s -> 4s -> ... -> 60s max).
    The websockets library handles PING/PONG keepalive automatically at
    WS_PING_INTERVAL_S (50 seconds).

    This is the SOLE detection mechanism for new markets. There is no polling.
    """
    import websockets

    reconnect_delay = INITIAL_RECONNECT_DELAY_S
    _shutdown = _shutdown_event

    while not _shutdown.is_set():
        try:
            logger.info(f"Sniper WS: connecting to {WS_URL}")

            async with websockets.connect(
                WS_URL,
                ping_interval=WS_PING_INTERVAL_S,
                ping_timeout=10,
                close_timeout=5,
                max_size=10 * 1024 * 1024,
            ) as ws:
                # --- Subscribe to market events with custom_feature_enabled ---
                await ws.send(json.dumps(SUBSCRIBE_PAYLOAD))
                logger.info(f"Sniper WS: subscribed with {SUBSCRIBE_PAYLOAD}")

                reconnect_delay = INITIAL_RECONNECT_DELAY_S

                # --- Listen loop ---
                while not _shutdown.is_set():
                    try:
                        raw_msg = await asyncio.wait_for(
                            ws.recv(),
                            timeout=WS_RECV_TIMEOUT_S,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("Sniper WS: recv timeout, reconnecting")
                        break
                    except websockets.ConnectionClosed as e:
                        logger.warning(f"Sniper WS: connection closed: {e}")
                        break

                    try:
                        await _handle_message(raw_msg, ws)   # pass ws for order book subscription
                    except Exception as e:
                        logger.error(f"Sniper WS: handler error: {e}", exc_info=True)

        except asyncio.CancelledError:
            logger.info("Sniper WS: cancelled, exiting")
            return
        except Exception as e:
            logger.error(f"Sniper WS: connection error: {e}")

        if _shutdown.is_set():
            break

        logger.info(f"Sniper WS: reconnecting in {reconnect_delay:.1f}s")
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=reconnect_delay)
            if _shutdown.is_set():
                break
        except asyncio.TimeoutError:
            pass

        reconnect_delay = min(
            reconnect_delay * RECONNECT_BACKOFF_FACTOR,
            MAX_RECONNECT_DELAY_S,
        )

    logger.info("Sniper WS: loop exited cleanly")


def _run_async_loop():
    """Thread entry point for the WebSocket listener."""
    global _shutdown_event
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _shutdown_event = asyncio.Event()
    try:
        loop.run_until_complete(_ws_loop())
    except Exception as e:
        logger.error(f"Sniper WS: async loop crashed: {e}", exc_info=True)
    finally:
        loop.close()
        logger.info("Sniper WS: event loop closed")

# ==========================================================================
# GTC LADDER FOR NEW WEATHER MARKETS
# ==========================================================================

def _try_split_and_sell(signal: dict, market_data: dict) -> dict:
    """
    CTF split-and-sell pre-check, gated by SNIPER_SPLIT_ENABLED.

    Decision:
        If best_yes_bid >= model_prob + SPLIT_MIN_EDGE:
            split $size into YES+NO, sell YES into the bid (FOK), keep NO.
            Net: paid (1 - yes_bid) per NO share, NO model_prob = 1 - prob.
            Edge captured = (1 - prob) - (1 - yes_bid) = yes_bid - prob.

    Returns one of:
        {"action": "skip"}        - nothing to do, fall through to ladder
        {"action": "executed", "remaining_usdc": float}
                                  - split done; reduces remaining ladder budget
        {"action": "failed",  "remaining_usdc": float}
                                  - split or sell failed; ladder still runs

    Mutates signal["size"] is NOT done here; caller handles budget.
    """
    if not SNIPER_SPLIT_ENABLED:
        return {"action": "skip"}

    meta       = signal["meta"]
    cond_id    = meta.get("condition_id", "")
    yes_token  = meta.get("yes_token_id", "")
    neg_risk   = bool(meta.get("neg_risk", False))
    prob       = float(signal.get("prob", 0.0))

    if not cond_id or not yes_token or prob <= 0:
        return {"action": "skip"}

    yes_bid = _fetch_best_bid(yes_token)
    if yes_bid is None:
        return {"action": "skip"}

    edge_on_split = yes_bid - prob
    if edge_on_split < SPLIT_MIN_EDGE:
        logger.info(
            f"Sniper SPLIT skip: yes_bid=${yes_bid:.3f} model_prob={prob:.3f} "
            f"edge={edge_on_split:+.3f} < SPLIT_MIN_EDGE={SPLIT_MIN_EDGE:+.3f}"
        )
        return {"action": "skip"}

    budget_split = round(min(MAX_USD_PER_SPLIT, signal.get("size", 0.0)), 2)
    if budget_split < 5.0:
        return {"action": "skip"}

    logger.info(
        f"Sniper SPLIT OPPORTUNITY: yes_bid=${yes_bid:.3f} model_prob={prob:.3f} "
        f"edge={edge_on_split:+.3f} >= {SPLIT_MIN_EDGE:+.3f} | "
        f"splitting ${budget_split:.2f} on cond={cond_id[:14]}... neg_risk={neg_risk}"
    )

    # Lazy import: ctf.py is optional infra; failing import must not crash sniper.
    try:
        from ctf import split_position
    except Exception as e:
        logger.error(f"Sniper SPLIT: ctf import failed: {e}")
        return {"action": "failed", "remaining_usdc": signal.get("size", 0.0)}

    split_resp = split_position(cond_id, budget_split, neg_risk=neg_risk)
    split_status = split_resp.get("status", "UNKNOWN")
    if split_status not in ("SUCCESS", "SIMULATED"):
        logger.error(f"Sniper SPLIT: splitPosition failed: {split_resp}")
        _notify_error(f"splitPosition failed for {meta.get('slug')}: {split_resp.get('reason', split_status)}")
        return {"action": "failed", "remaining_usdc": signal.get("size", 0.0)}

    # Each $1 split mints 1 YES + 1 NO at par.
    pair_shares = budget_split

    # Record both legs as 'split' entries before attempting the sell, so
    # crashes between split and sell still leave a trail for the merge monitor.
    no_token = meta.get("no_token_id", "")
    try:
        record_entry(
            token_id=yes_token,
            condition_id=cond_id,
            slug=market_data.get("slug", "split-snipe"),
            city=meta["city"],
            market_date=meta["date"],
            bucket_low=meta["bucket_low"],
            bucket_high=meta["bucket_high"],
            unit=meta["unit"],
            entry_price=1.0,             # par cost from splitPosition
            shares=pair_shares,
            size_usdc=budget_split,
            order_id=split_resp.get("transaction_hash", "CTF_SPLIT"),
            neg_risk=neg_risk,
            question=market_data.get("question", ""),
            forecast_prob=prob,
            edge=edge_on_split,
            outcome="YES",
            entry_method="split",
        )
        if no_token:
            record_entry(
                token_id=no_token,
                condition_id=cond_id,
                slug=market_data.get("slug", "split-snipe"),
                city=meta["city"],
                market_date=meta["date"],
                bucket_low=meta["bucket_low"],
                bucket_high=meta["bucket_high"],
                unit=meta["unit"],
                entry_price=1.0,
                shares=pair_shares,
                size_usdc=budget_split,
                order_id=split_resp.get("transaction_hash", "CTF_SPLIT"),
                neg_risk=neg_risk,
                question=market_data.get("question", ""),
                forecast_prob=1.0 - prob,
                edge=edge_on_split,
                outcome="NO",
                entry_method="split",
            )
    except Exception as e:
        logger.warning(f"Sniper SPLIT: record_entry failed (non-fatal): {e}")

    # Now sell YES into the overpriced bid via FOK (immediate-or-cancel).
    from executor import place_sell_order
    sell_resp = place_sell_order(
        token_id=yes_token,
        price=yes_bid,
        num_shares=pair_shares,
    )
    sell_status = sell_resp.get("status", "UNKNOWN")
    logger.info(
        f"Sniper SPLIT-SELL: sold {pair_shares:.2f} YES @ ${yes_bid:.3f} | "
        f"status={sell_status}"
    )

    # Adjust YES leg's recorded entry to reflect the sale outcome:
    #   - On FOK fill: YES leg becomes 'exited' with proceeds = pair_shares * yes_bid.
    #     Net cost basis for the remaining NO leg = budget_split - proceeds = pair_shares * (1 - yes_bid).
    if sell_status in ("MATCHED", "simulated", "FILLED"):
        try:
            # Find the most recent open YES position for this token + condition
            from positions import _get_conn, record_exit
            conn = _get_conn()
            row = conn.execute(
                """SELECT id FROM positions
                   WHERE token_id = ? AND condition_id = ? AND status = 'open'
                     AND outcome = 'YES' AND entry_method = 'split'
                   ORDER BY id DESC LIMIT 1""",
                (yes_token, cond_id),
            ).fetchone()
            if row:
                record_exit(
                    position_id=int(row["id"]),
                    exit_price=yes_bid,
                    exit_reason="split_sell",
                    exit_order_id=sell_resp.get("orderID", "FOK_SELL"),
                )
        except Exception as e:
            logger.warning(f"Sniper SPLIT: YES exit record failed: {e}")
    else:
        logger.warning(
            f"Sniper SPLIT: YES sell did NOT fill — both legs remain open "
            f"and will be merged by inventory_manager on next cycle."
        )

    if _notifier:
        try:
            _notifier.send_message(
                f"SPLIT-SELL: {meta['city']} {meta['date']}\n"
                f"YES bid=${yes_bid:.3f} model_prob={prob:.1%} edge={edge_on_split:+.3f}\n"
                f"Split ${budget_split:.2f} | Sell status: {sell_status}\n"
                f"Implied NO entry: ${1.0 - yes_bid:.3f}"
            )
        except Exception:
            pass

    remaining = max(0.0, round(signal.get("size", 0.0) - budget_split, 2))
    return {"action": "executed", "remaining_usdc": remaining}


def place_gtc_ladder(signal: dict, market_data: dict):
    """
    Places a 4-level GTC ladder on a new temperature market using dynamic pricing.
    Levels step down from signal["price"] (computed by _compute_entry_price) in
    $0.02 increments. Most allocation goes at the signal price (most fill-likely).
    Total spend capped at MAX_USD_PER_NEW_MARKET ($25).
    All orders are GTC (Good-Til-Canceled).
    Note: on brand-new markets with no existing order book there is no market price
    to fall back to, so GTC is the only viable order type.
    """
    token_id = signal["tokenId"]
    meta = signal["meta"]
    total_usdc = min(signal.get("size", 25.0), MAX_USD_PER_NEW_MARKET)

    # CTF split-and-sell pre-check (no-op when SNIPER_SPLIT_ENABLED=false).
    # Runs BEFORE the FOK ask-sweep because it competes for the same budget:
    # if YES bids are overpriced, we'd rather mint at par + sell into the bid
    # than pay an ask. If split executes, we deduct its cost from total_usdc.
    try:
        split_res = _try_split_and_sell(signal, market_data)
        if split_res.get("action") == "executed":
            total_usdc = float(split_res.get("remaining_usdc", total_usdc))
            if total_usdc < 1.0:
                logger.info("Sniper LADDER: budget consumed by split-and-sell, no ladder needed")
                return
            logger.info(f"Sniper LADDER: ${total_usdc:.2f} remaining after split-and-sell")
    except Exception as e:
        logger.error(f"Sniper SPLIT: pre-check crashed (continuing to ladder): {e}", exc_info=True)

    base_price = signal["price"]  # dynamically computed by _compute_entry_price()
    LADDER_STEP = 0.02
    ladder_prices = [
        round(base_price, 2),
        round(max(MIN_ENTRY_PRICE, base_price - LADDER_STEP), 2),
        round(max(MIN_ENTRY_PRICE, base_price - 2 * LADDER_STEP), 2),
        round(max(MIN_ENTRY_PRICE, base_price - 3 * LADDER_STEP), 2),
    ]
    weights = [0.40, 0.30, 0.20, 0.10]

    logger.info(
        f"Sniper LADDER: Starting 4-level GTC for {meta['city']} {meta['date']} | "
        f"token {token_id[:12]}... | total ${total_usdc:.2f}"
    )

    # Pre-ladder book sweep: if a book has formed with qualifying asks,
    # FOK-sweep them first. Cheaper than laying out a stepped-down ladder
    # and praying. Remainder of total_usdc flows through the ladder below.
    try:
        book_asks = get_book_asks(token_id)
    except Exception as e:
        logger.debug(f"Sniper LADDER: book fetch failed ({e}); skipping sweep")
        book_asks = []

    prob = signal.get("prob", 0.0)
    qualifying = [(p, s) for p, s in book_asks if p < prob - ENTRY_THRESHOLD]
    if qualifying and prob > 0:
        max_fill_price = qualifying[-1][0]
        sweep_depth    = sum(p * s for p, s in qualifying)
        sweep_budget   = round(min(total_usdc, sweep_depth), 2)
        if sweep_budget >= 1.0:
            logger.info(
                f"Sniper SWEEP: FOK ${sweep_budget:.2f} up to ${max_fill_price:.3f} "
                f"({len(qualifying)} asks, depth=${sweep_depth:.2f})"
            )
            sweep_resp = place_buy_order(
                token_id=token_id,
                price=max_fill_price,
                size_usdc=sweep_budget,
            )
            if sweep_resp.get("status") == "MATCHED":
                try:
                    num_shares = sweep_budget / max_fill_price if max_fill_price > 0 else 0.0
                    record_entry(
                        token_id=token_id,
                        condition_id=meta.get("condition_id", ""),
                        slug=market_data.get("slug", "ladder-snipe"),
                        city=meta["city"],
                        market_date=meta["date"],
                        bucket_low=meta["bucket_low"],
                        bucket_high=meta["bucket_high"],
                        unit=meta["unit"],
                        entry_price=max_fill_price,
                        shares=num_shares,
                        size_usdc=sweep_budget,
                        order_id=sweep_resp.get("orderID", "SWEEP"),
                        question=market_data.get("question", ""),
                        forecast_prob=prob,
                        edge=signal.get("edge", 0.0),
                    )
                    try:
                        log_trade(
                            market_slug=market_data.get("slug", "ladder-snipe"),
                            city=meta["city"],
                            date=meta["date"],
                            forecast_temp_c=signal.get("forecast_celsius", 0.0),
                            forecast_temp_market_unit=signal.get("forecast_in_unit", 0.0),
                            market_unit=meta["unit"],
                            bucket_low=meta["bucket_low"],
                            bucket_high=meta["bucket_high"],
                            forecast_prob=prob,
                            market_price=max_fill_price,
                            edge=signal.get("edge", 0.0),
                            size_usdc=sweep_budget,
                            dry_run=DRY_RUN,
                            order_response=sweep_resp,
                            question=market_data.get("question", ""),
                        )
                    except Exception as e:
                        logger.warning(f"Sniper sweep: log_trade failed: {e}")
                except Exception as e:
                    logger.warning(f"Failed to record sweep fill: {e}")
                total_usdc = round(total_usdc - sweep_budget, 2)
                if total_usdc < 1.0:
                    logger.info("Sniper LADDER: budget consumed by sweep, no ladder needed")
                    return
                # Re-scale ladder allocation to the remaining budget
                logger.info(f"Sniper LADDER: ${total_usdc:.2f} remaining for ladder")

    for i, price in enumerate(ladder_prices):
        size_usdc = round(total_usdc * weights[i], 2)
        if size_usdc < 1.0:
            continue

        logger.info(f"  Ladder level {i+1}: BUY ${price:.2f} for ${size_usdc:.2f} USDC (GTC)")

        result = place_gtc_order(
            token_id=token_id,
            price=price,
            size_usdc=size_usdc,
            wait_seconds=8.0,
        )

        if result.get("status") != "FAILED":
            try:
                num_shares = size_usdc / price if price > 0 else 0.0
                record_entry(
                    token_id=token_id,
                    condition_id=meta.get("condition_id", ""),
                    slug=market_data.get("slug", "ladder-snipe"),
                    city=meta["city"],
                    market_date=meta["date"],
                    bucket_low=meta["bucket_low"],
                    bucket_high=meta["bucket_high"],
                    unit=meta["unit"],
                    entry_price=price,
                    shares=num_shares,
                    size_usdc=size_usdc,
                    order_id=result.get("order_id", f"DRY_RUN_{price}"),
                    question=market_data.get("question", ""),
                    forecast_prob=signal["prob"],
                    edge=signal.get("edge", 0.0),
                )
                try:
                    log_trade(
                        market_slug=market_data.get("slug", "ladder-snipe"),
                        city=meta["city"],
                        date=meta["date"],
                        forecast_temp_c=signal.get("forecast_celsius", 0.0),
                        forecast_temp_market_unit=signal.get("forecast_in_unit", 0.0),
                        market_unit=meta["unit"],
                        bucket_low=meta["bucket_low"],
                        bucket_high=meta["bucket_high"],
                        forecast_prob=signal["prob"],
                        market_price=price,
                        edge=signal.get("edge", 0.0),
                        size_usdc=size_usdc,
                        dry_run=DRY_RUN,
                        order_response=result,
                        question=market_data.get("question", ""),
                    )
                except Exception as e:
                    logger.warning(f"Sniper ladder: log_trade failed: {e}")
            except Exception as e:
                logger.warning(f"Failed to record ladder order: {e}")


# ==========================================================================
# Public API
# ==========================================================================

def start_weather_sniper(notifier=None):
    """
    Starts the weather market sniper system (2 daemon threads):
      1. Forecast refresh thread: warms cache every 5 min with GFS + GraphCast + METAR
      2. WebSocket listener thread: connects to Polymarket WS, listens for new_market

    Args:
        notifier: Optional TelegramNotifier instance (same as bot.py uses).

    Returns:
        The WebSocket listener thread.
    """
    global _sniper_thread, _forecast_thread, _notifier

    _notifier = notifier
    _shutdown_flag.clear()

    if _sniper_thread is not None and _sniper_thread.is_alive():
        logger.warning("Sniper: already running, not starting again")
        return _sniper_thread

    # --- Start forecast refresh thread first (warms cache before WS connects) ---
    logger.info("Sniper: starting forecast refresh thread")
    _forecast_thread = threading.Thread(
        target=_forecast_refresh_loop,
        daemon=True,
        name="sniper-forecast",
    )
    _forecast_thread.start()

    # Wait up to 150s for initial cache warm-up
    logger.info("Sniper: waiting for initial forecast cache warm-up...")
    for _ in range(150):
        if _last_forecast_refresh > 0:
            break
        time.sleep(1)
    if _last_forecast_refresh > 0:
        logger.info("Sniper: forecast cache warmed, starting WebSocket listener")
    else:
        logger.warning("Sniper: forecast cache not yet warm, starting WS anyway")

    # --- (Optional) CTF strategy startup hooks ---
    if SNIPER_SPLIT_ENABLED:
        logger.info("Sniper: SNIPER_SPLIT_ENABLED=true — initializing CTF infra")
        try:
            from ctf import ensure_collateral_allowances, DRY_RUN as CTF_DRY_RUN
            if not CTF_DRY_RUN:
                logger.info("Sniper: ensuring USDC.e allowances for CTF + NegRiskAdapter")
                approval_res = ensure_collateral_allowances()
                logger.info(f"Sniper: collateral approvals: {approval_res.get('status')}")
            else:
                logger.info("Sniper: CTF_DRY_RUN=true, skipping live approvals")
        except Exception as e:
            logger.error(f"Sniper: CTF startup hook failed (non-fatal): {e}")

        try:
            from inventory_manager import start_inventory_manager
            start_inventory_manager(notifier=notifier)
            logger.info("Sniper: inventory manager thread started")
        except Exception as e:
            logger.error(f"Sniper: inventory_manager startup failed (non-fatal): {e}")

    # --- Start WebSocket listener thread ---
    _sniper_thread = threading.Thread(
        target=_run_async_loop,
        daemon=True,
        name="sniper-ws",
    )
    _sniper_thread.start()
    logger.info("Sniper: all threads started")

    if notifier:
        try:
            with _forecast_cache_lock:
                cached_cities = len(_forecast_cache)
            with _graphcast_cache_lock:
                gc_cities = len(_graphcast_cache)
            with _metar_cache_lock:
                metar_cities = len(_metar_cache)
            with _ecmwf_cache_lock:
                ecmwf_cities = len(_ecmwf_cache)
            notifier.send_message(
                f"Weather Sniper started\n"
                f"WS: {WS_URL}\n"
                f"GFS cache: {cached_cities} cities\n"
                f"GraphCast cache: {gc_cities} cities\n"
                f"ECMWF cache: {ecmwf_cities} cities\n"
                f"METAR cache: {metar_cities} stations\n"
                f"Blend: {ECMWF_WEIGHT:.0%} ECMWF + {GRAPHCAST_WEIGHT:.0%} GraphCast + {GFS_WEIGHT:.0%} GFS\n"
                f"Refresh: every {FORECAST_REFRESH_INTERVAL_S}s\n"
                f"Price range: ${MIN_ENTRY_PRICE:.2f}-${MAX_ENTRY_PRICE:.2f}\n"
                f"Min bracket prob: {MIN_BRACKET_PROB:.0%}"
            )
        except Exception:
            pass

    return _sniper_thread


def stop_weather_sniper():
    """
    Signals all sniper threads to shut down gracefully.
    """
    global _shutdown_event, _sniper_thread, _forecast_thread

    logger.info("Sniper: shutdown requested")
    _shutdown_flag.set()

    if _shutdown_event is not None:
        _shutdown_event.set()

    if SNIPER_SPLIT_ENABLED:
        try:
            from inventory_manager import stop_inventory_manager
            stop_inventory_manager()
        except Exception as e:
            logger.warning(f"Sniper: inventory_manager stop failed: {e}")

    for name, thread in [("forecast", _forecast_thread), ("ws", _sniper_thread)]:
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
            if thread.is_alive():
                logger.warning(f"Sniper: {name} thread did not exit within 10s")
            else:
                logger.info(f"Sniper: {name} thread exited cleanly")

    _sniper_thread = None
    _forecast_thread = None


# ==========================================================================
# Standalone entry point (for testing)
# ==========================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(module)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    print("Weather Market Sniper - Standalone Test")
    print(f"  DRY_RUN:     {DRY_RUN}")
    print(f"  WS URL:      {WS_URL}")
    print(f"  Price range: ${MIN_ENTRY_PRICE:.2f} - ${MAX_ENTRY_PRICE:.2f}")
    print(f"  Min prob:    {MIN_BRACKET_PROB:.0%}")
    print(f"  Forecast refresh: every {FORECAST_REFRESH_INTERVAL_S}s")
    print(f"  GraphCast weight: {GRAPHCAST_WEIGHT:.0%}")
    print(f"  Cities tracked: {len(CITIES)}")
    print("Press Ctrl+C to stop.\n")

    _tg_token = os.environ.get("TELEGRAM_TOKEN")
    _tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    _notifier_inst = TelegramNotifier(_tg_token, int(_tg_chat)) if _tg_token and _tg_chat else None
    thread = start_weather_sniper(notifier=_notifier_inst)
    try:
        while thread.is_alive():
            thread.join(timeout=1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        stop_weather_sniper()
        print("Done.")
