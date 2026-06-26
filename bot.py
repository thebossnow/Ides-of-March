"""
bot.py - Main bot entry point and scheduler loop.
Scans weather markets, calculates edges, places orders, monitors positions,
resolves past markets, and redeems winning positions.

Scheduling:
  - 30 min: standard scan cycle (new trades + position monitoring)
  - 15 min: fast monitor loop (only when holding positions <12h from resolution)
  - Daily 00:05 UTC: resolution checker + redemption
  - Daily 23:55 UTC: daily report

Includes:
  - Position tracking via SQLite (positions.py)
  - Same-day exit signals based on observed temps (position_monitor.py)
  - Profit-taking at 50%+ when forecast prob < 60%
  - Automatic resolution checking via Open-Meteo archive (observed_temps.py)
  - Gasless redemption of winning positions (redeemer.py)
  - Telegram daily reports (balance, trades, wins/losses, in-progress)
  - Heartbeat with stale-ID recovery
  - Auth retry with max attempts and Telegram error alerts

Run with: python3 bot.py
Stop with: Ctrl+C  (or detach screen session: Ctrl+A then D)
"""

# Load .env so OPENMETEO_API_KEY etc. are available regardless of launch method
from dotenv import load_dotenv
load_dotenv()

import schedule
import time
import threading
import logging
import os
import signal
import uuid
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from weather_v2 import get_forecast, get_forecast_low, get_ensemble_forecast, celsius_to_fahrenheit, fahrenheit_to_celsius, CITIES
from weather_v2 import get_city_gfs_ensemble, get_gfs_spread
from markets import (
    get_weather_markets,
    parse_market_metadata,
    get_market_price,
    get_midpoint_price,
    is_book_illiquid,
    get_book_asks,
)
from executor import (
    get_client as get_clob_client,
    place_buy_order,
    place_ladder_bids,
    place_gtc_order,
    DRY_RUN,
)
from strategy import (
    forecast_probability,
    wu_normal_probability,
    wu_empirical_or_normal_probability,
    ensemble_probability,
    empirical_probability,
    empirical_probability_with_bias,
    bayesian_metar_probability,
    find_edge,
    should_trade,
    classify_skip_reason,
    convert_forecast_to_market_unit,
    get_prob_floor,
    is_city_tradable,
    is_city_watch_only,
    is_city_gfs_stable,
    get_entry_threshold,
    get_spread_threshold,
    compute_lead_time_days,
    MAX_LEAD_TIME_DAYS,
    HIGH_GFS_SPREAD_THRESHOLD,
    ORHIGHER_ORBELOW_CITIES,
    ENTRY_THRESHOLD,
    MIN_HOURS_TO_RES,
    SOFT_MIN_PROB,
    LOWEST_SOFT_MIN_PROB,
    UNRESOLVED_EXPOSURE_CAP,
)
from risk_manager_v2 import (
    get_current_bankroll,
    get_safe_position_size,
    check_drawdown,
    print_risk_status,
)
from logger import log_trade, log_scan
from notifier import TelegramNotifier
from positions import record_entry, is_token_traded_today, get_open_positions, get_total_open_exposure, init_db
from position_monitor import (
    monitor_positions,
    resolve_past_positions,
    needs_fast_monitoring,
)
from redeemer import redeem_all_winners
from aviation_weather import get_current_metar_temps, AVIATION_ICAO
from metar_bias import compute_biases_from_metar

# ── Wunderground: resolution source's own forecast (replaces GFS/t-dist) ──
from wunderground_client import (
    fetch_forecasts as wu_fetch_forecasts,
    wunderground_match,
    get_forecast_for_date as wu_get_forecast_for_date,
    AIRPORT_COORDS as WU_AIRPORT_COORDS,
)
from wu_empirical import log_wu_scan

# Lottery cities: HK/Seoul now in BLOCKED_CITIES (never trade).
# LOTTERY_CITIES / LOTTERY_MAX_USDC removed — no lottery cities remain.

# -----------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------
BANKROLL = 0.0  # Set dynamically at startup; overwritten in __main__ via get_current_bankroll()
SCAN_INTERVAL       = 15   # Minutes between standard scans
FAST_MONITOR_INTERVAL = 15 # Minutes between fast monitor checks (positions <12h out)
FORECAST_DAYS       = 10   # Max days ahead to fetch forecasts (calibrated sigma/df range)
LOG_LEVEL           = logging.INFO

# Aviation Weather toggle (METAR/TAF from AviationWeather.gov)
USE_AVIATION_WEATHER = os.getenv("USE_AVIATION_WEATHER", "true").lower() in ("true", "1", "yes")

# Telegram Configuration - must be set in .env (never hardcode)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise EnvironmentError(
        "TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in your .env file. "
        "Do not hardcode credentials."
    )
TELEGRAM_CHAT_ID = int(TELEGRAM_CHAT_ID)

# Daily report time (UTC hour, 0-23). Default: 23:55 UTC
DAILY_REPORT_HOUR   = 23
DAILY_REPORT_MINUTE = 55

# Resolved market checker time (runs once per day)
RESOLVED_CHECK_HOUR   = 0
RESOLVED_CHECK_MINUTE = 5

# -----------------------------------------------------------------------
# Logging setup
# -----------------------------------------------------------------------
logging.basicConfig(
    handlers=[
        logging.FileHandler("bot.log"),
    ],
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(module)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Telegram Notifier (global, thread-safe)
# -----------------------------------------------------------------------
notifier = TelegramNotifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)

def _get_clob_client_safe() -> object:
    """
    Returns the executor module-level singleton ClobClient.
    Auth creds are derived once at import time in executor.py and reused.
    No retry logic needed here since the client is already authenticated.
    """
    return get_clob_client()


# -----------------------------------------------------------------------
# Live bankroll helper
# -----------------------------------------------------------------------
def get_current_bankroll() -> float:
    """Get real available USDC. Tries signature_type=1 first (works for your proxy wallet).
    Returns actual live balance. Returns 0.0 if all attempts fail so Kelly
    sizing produces $0 positions instead of trading on a fake bankroll."""
    import time as _time
    from py_clob_client_v2 import BalanceAllowanceParams, AssetType

    errors = []

    try:
        client = get_clob_client()  # Reuse executor singleton (already authed)

        for sig_type in [1, 2]:   # 1 works for you, 2 is fallback
            try:
                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=sig_type
                )
                client.update_balance_allowance(params)
                _time.sleep(2)

                result = client.get_balance_allowance(params)
                if isinstance(result, dict) and "balance" in result:
                    balance_usdc = int(result["balance"]) / 1_000_000
                    if balance_usdc >= 0:
                        logger.info(f"Live bankroll (sig_type={sig_type}): ${balance_usdc:.2f}")
                        return round(balance_usdc, 2)
                else:
                    msg = f"sig_type={sig_type}: unexpected response format: {result}"
                    errors.append(msg)
                    logger.warning(msg)
            except Exception as inner_e:
                msg = f"sig_type={sig_type} failed: {inner_e}"
                errors.append(msg)
                logger.warning(msg)
                continue

        # All attempts failed -- return 0 so Kelly sizes to $0 (no trades)
        logger.error(
            f"get_current_bankroll: ALL balance attempts failed. "
            f"Returning $0 (no trades will execute). Errors: {errors}"
        )
        return 0.0

    except Exception as e:
        logger.error(f"get_current_bankroll: outer error: {e}. Returning $0 (no trades).")
        return 0.0

# -----------------------------------------------------------------------
# Heartbeat thread (LIVE mode only)
# -----------------------------------------------------------------------
_heartbeat_stop = threading.Event()


def _heartbeat_loop() -> None:
    """
    Background thread: sends a heartbeat to CLOB every 5 seconds.

    CRITICAL FIX (per Polymarket CLOB protocol):
    1. Start with heartbeat_id = "" (empty string)
    2. Server responds with a new heartbeat_id in the response JSON
    3. Use THAT server-provided ID on the next call
    4. Each successful response gives you the next ID to use

    If you use self-generated IDs or fail to update from responses,
    the server rejects every heartbeat with "Invalid Heartbeat ID"
    and cancels all open orders.
    """
    hb_id = ""  # CRITICAL: Start with empty string per Polymarket protocol
    logger.info("Heartbeat thread started with empty ID (will be set by server)")

    consecutive_failures = 0
    max_consecutive_failures = 20  # Raised from 10 — normal restart causes 5-6 failures
    cloudflare_backoff = 0       # seconds, 0 = not in backoff
    cloudflare_max_backoff = 300  # 5 min cap

    def _is_cloudflare_block(error_text: str) -> bool:
        """Detect Cloudflare 403 blocks in API error responses."""
        lower = error_text.lower()
        return any(kw in lower for kw in (
            "cloudflare", "cf-wrapper", "cf-ray", "attention required",
            "403 forbidden", "you have been blocked", "<!doctype",
        ))

    try:
        client = _get_clob_client_safe()
    except Exception as e:
        logger.error(f"Heartbeat: could not create initial CLOB client: {e}")
        notifier.notify_error("Heartbeat", f"Could not start: {e}")
        return

    while not _heartbeat_stop.is_set():
        try:
            resp = client.post_heartbeat(hb_id)

            # CRITICAL: Extract server-provided heartbeat_id from response
            if isinstance(resp, dict) and "heartbeat_id" in resp:
                new_hb_id = resp["heartbeat_id"]
                if new_hb_id != hb_id:
                    logger.debug(f"Heartbeat: updated ID {hb_id} -> {new_hb_id}")
                    hb_id = new_hb_id

            consecutive_failures = 0  # Reset on success

        except Exception as e:
            error_str = str(e)

            # Cloudflare block detection — enter exponential backoff, don't crash
            if _is_cloudflare_block(error_str):
                if cloudflare_backoff == 0:
                    cloudflare_backoff = 30  # Start at 30s
                else:
                    cloudflare_backoff = min(cloudflare_backoff * 2, cloudflare_max_backoff)
                logger.warning(
                    f"Heartbeat: Cloudflare block detected. "
                    f"Backing off {cloudflare_backoff}s (not counting as failure)"
                )
                _heartbeat_stop.wait(cloudflare_backoff)
                continue  # Don't count as failure, don't alert Telegram

            consecutive_failures += 1
            logger.warning(
                f"Heartbeat failed ({consecutive_failures}x): {error_str[:200]}"
            )

            # Try to extract server-provided heartbeat_id from error response
            # Error format: PolyApiException[..., error_message={'heartbeat_id': '...', 'error_msg': '...'}]
            try:
                # Extract the server-provided heartbeat_id from the error response.
                # The CLOB returns the current valid ID in the error body so we
                # can resync on the next call.
                hb_payload = None
                if hasattr(e, 'error_msg') and isinstance(e.error_msg, dict):
                    hb_payload = e.error_msg
                elif hasattr(e, 'error_message') and isinstance(e.error_message, dict):
                    hb_payload = e.error_message
                if hb_payload:
                    new_hb_id = hb_payload.get("heartbeat_id")
                    if new_hb_id:
                        logger.debug(f"Heartbeat: resyncing ID from error response")
                        hb_id = new_hb_id
            except Exception as parse_err:
                logger.debug(f"Heartbeat: could not parse error response: {parse_err}")

            # Re-auth the client on auth errors
            if "401" in error_str or "auth" in error_str.lower():
                try:
                    client = _get_clob_client_safe()
                    logger.info("Heartbeat: re-authenticated client")
                except Exception as auth_err:
                    logger.error(f"Heartbeat re-auth failed: {auth_err}")

            # Log only — no Telegram alert for heartbeat failures.
            # Heartbeat self-recovers within ~30s after restart; the cron watchdog
            # handles true service outages. Alerting here causes spam on every restart.
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    f"Heartbeat: {consecutive_failures} consecutive failures — "
                    f"still trying (last error: {error_str[:100]})"
                )
                consecutive_failures = 0  # Reset counter

        _heartbeat_stop.wait(5)

    logger.info("Heartbeat thread stopped.")


def start_heartbeat() -> threading.Thread:
    """Starts the heartbeat background thread. No-op in DRY_RUN mode."""
    if DRY_RUN:
        logger.info("DRY_RUN: heartbeat thread not started.")
        return None
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    t.start()
    return t


def stop_heartbeat() -> None:
    _heartbeat_stop.set()


# -----------------------------------------------------------------------
# Position tracking
# -----------------------------------------------------------------------
MIN_PRICE_FLOOR = 0.01

_traded_tokens: set = set()
_traded_tokens_date: str = ""


def _reset_position_tracker() -> None:
    """Clears the traded token set at midnight and resets daily Telegram stats."""
    global _traded_tokens, _traded_tokens_date
    today = datetime.now(timezone.utc).date().isoformat()

    if _traded_tokens_date != today:
        _traded_tokens = set()
        _traded_tokens_date = today
        logger.info(f"Position tracker reset for new day: {today}")
        notifier.reset_daily(get_current_bankroll())


# -----------------------------------------------------------------------
# Scan cycle
# -----------------------------------------------------------------------
def run_cycle() -> None:
    """
    One full scan cycle:
    1. Fetch all active weather markets
    2. For each market: parse metadata, get forecast, calculate edge
    3. Collect all qualifying signals
    4. Filter: best-bucket-only per city/date
    5. Place orders for remaining signals
    6. Send cycle summary via Telegram
    """
    global _traded_tokens

    cycle_start = datetime.now()
    mode_label = "DRY RUN" if DRY_RUN else "LIVE"
    logger.info(f"=== Scan cycle started [{mode_label}] ===")

    _reset_position_tracker()

    try:
        clob_client = _get_clob_client_safe()
    except Exception as e:
        logger.error(f"Failed to create CLOB client for cycle: {e}")
        notifier.notify_error("Scan Cycle", f"CLOB client creation failed: {e}")
        return

    forecast_cache: dict = {}
    ensemble_cache: dict = {}  # per-model raw forecasts for snapshot
    gfs_ensemble_cache: dict = {}  # GFS 30-member ensemble {city: {date: [vals]}}
    price_cache: dict = {}       # token_id -> price; avoids redundant CLOB /book calls per cycle
    metar_cache: dict = {}       # city_key -> METAR obs for Bayesian same-day updates
    trades_placed = 0
    total_spent = 0.0
    cycle_bankroll = get_current_bankroll()
    MAX_DAILY_SPEND = cycle_bankroll * 0.5

    # Phase 0: Pre-fetch METAR observations for Bayesian same-day updates
    raw_metar: dict = {}      # Initialised before try so bias block can always reference it
    icao_to_city: dict = {}
    if USE_AVIATION_WEATHER:
        try:
            all_icao = list(AVIATION_ICAO.values())
            icao_to_city = {icao: city_key for city_key, icao in AVIATION_ICAO.items()}
            raw_metar = get_current_metar_temps(all_icao)
            for icao, obs in raw_metar.items():
                city_key = icao_to_city.get(icao)
                if city_key:
                    metar_cache[city_key] = obs
            logger.info(f"METAR cache warm: {len(metar_cache)} stations")
        except Exception as e:
            logger.warning(f"METAR pre-fetch failed (scan continues without Bayesian): {e}")

    # Phase 0b: Compute live METAR bias corrections for forecast adjustment
    metar_bias_cache: dict[str, float] = {}
    if USE_AVIATION_WEATHER and metar_cache:
        try:
            metar_bias_cache = compute_biases_from_metar(raw_metar, icao_to_city)
        except Exception as e:
            logger.warning(f"METAR bias computation failed (scan continues): {e}")

    try:
        markets = get_weather_markets()
        logger.info(f"Scanning {len(markets)} weather markets")
    except Exception as e:
        logger.error(f"Failed to fetch markets: {e}")
        notifier.notify_error("Market Fetch", str(e))
        return

    markets_scanned = len(markets)

    # Phase 0c: Pre-fetch Wunderground forecasts (resolution source — primary signal)
    wunderground_cache: dict = {}
    try:
        # Collect unique cities from all markets
        wu_cities_set: set[str] = set()
        for market in markets:
            meta_raw = parse_market_metadata(market)
            if meta_raw and meta_raw.get("city") in WU_AIRPORT_COORDS:
                wu_cities_set.add(meta_raw["city"])
        wu_cities_list = list(wu_cities_set)
        if wu_cities_list:
            wunderground_cache = wu_fetch_forecasts(wu_cities_list)
            ok = sum(1 for v in wunderground_cache.values() if not v.get("error"))
            # ── Guard: validate forecast_days is non-empty for each city ──
            for city_name, city_data in wunderground_cache.items():
                if not city_data.get("error"):
                    fdays = city_data.get("forecast_days", [])
                    if not fdays:
                        logger.warning(
                            f"WU DATA GUARD: {city_name} returned empty forecast_days "
                            f"— marking as error"
                        )
                        city_data["error"] = "Empty forecast_days after validation"
            # Re-count after validation
            ok = sum(1 for v in wunderground_cache.values() if not v.get("error"))
            logger.info(f"Wunderground: {ok}/{len(wu_cities_list)} cities forecasted (after validation)")
    except Exception as e:
        logger.warning(f"Wunderground pre-fetch failed (all markets skipped): {e}")

    # Phase 1: Collect all qualifying signals
    signals = []

    for market in markets:
        slug = market.get("slug", "unknown")

        meta = parse_market_metadata(market)
        if meta is None:
            logger.debug(f"Skipping unparseable market: {slug}")
            log_scan(slug, "?", "?", 0.0, 0.0, 0.0, "SKIP", "unparseable_metadata")
            continue

        city = meta["city"]
        date_str = meta["date"]
        bucket_low = meta["bucket_low"]
        bucket_high = meta["bucket_high"]
        unit = meta["unit"]
        question = meta["question"]
        yes_token_id = meta["yes_token_id"]
        market_type = meta.get("market_type", "highest")

        # ── City block check ─────────────────────────────────────────────
        # Blocked cities: hard skip, no scan data collected
        if not is_city_tradable(city):
            log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP", "blocked_city",
                     bucket_low=bucket_low, bucket_high=bucket_high)
            continue
        # Watch-only cities: scan and collect data, but flag for no trade
        _city_watch_only = is_city_watch_only(city)

        if not yes_token_id or len(yes_token_id) < 10:
            logger.debug(f"No/invalid YES token ID for {slug}: {yes_token_id!r}")
            log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP", "no_yes_token_id", bucket_low=bucket_low, bucket_high=bucket_high)
            continue

        # Check both in-memory set (this cycle) and SQLite (persistent)
        if yes_token_id in _traded_tokens or is_token_traded_today(yes_token_id):
            logger.info(f"SKIP (already traded): {slug}")
            log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP", "already_traded", bucket_low=bucket_low, bucket_high=bucket_high)
            notifier.record_trade(entered=False)
            continue

        # ── Wunderground Check (resolution source — primary signal) ──
        # If WU has a forecast for this city+date, use it deterministically.
        # No fallback to GFS. No t-distribution. No Bayesian METAR.
        use_wunderground = False
        wu_city_data = wunderground_cache.get(city, {})
        if not wu_city_data or wu_city_data.get("error"):
            log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP", "NO_WU_DATA",
                     bucket_low=bucket_low, bucket_high=bucket_high)
            continue
        wu_result, wu_reason, wu_temp_c = wunderground_match(
            wu_city_data, date_str, market_type, bucket_low, bucket_high, unit=unit
        )
        # Phase 2: when WU has forecast data for this city/date but this bucket
        # isn't the point-match, compute probability anyway. Phase 2 grouping
        # (below) picks the highest-prob bucket per (city, date) from all
        # candidates. The corrected t-distribution sigma naturally assigns low
        # probability to buckets far from the WU forecast.
        _wu_direct_match = (wu_result == "TRADE")
        if wu_result == "SKIP":
            if wu_temp_c is None:
                # WU has no forecast at all for this date — skip entirely
                log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP", f"WU: {wu_reason}",
                         bucket_low=bucket_low, bucket_high=bucket_high)
                continue
            # WU has a forecast but this bucket isn't the point-match.
            # Fall through to probability computation for Phase 2 selection.
        use_wunderground = True

        # ── WU open-ended buffer guard ─────────────────────────────────────
        # For open-ended buckets, the WU 2× sigma stacking inflates probability
        # when the forecast is on the adverse side (e.g. forecast=14°C for a
        # [None,9°C] bucket), producing phantom FOK edge.
        # Require the WU forecast to be at least WU_MIN_BUFFER_F inside the
        # bucket boundary — regardless of market_type (highest or lowest).
        # Bug fixed 2026-06-08: previous ORBELOW guard was gated on
        # market_type=="lowest" so it never fired for "highest" markets with
        # [None,H] buckets (Munich, Moscow, Wellington, HK observed losses).
        WU_MIN_BUFFER_F = 5.0  # ~2.8°C; forecast must be this far inside the boundary
        if bucket_high is None and bucket_low is not None:
            # ORHIGHER: forecast must be >= bucket_low + buffer
            _bl_c = (bucket_low - 32.0) / 1.8 if unit.upper() == "F" else bucket_low
            _gap_f = (wu_temp_c - _bl_c) * 1.8
            if _gap_f < WU_MIN_BUFFER_F:
                log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP",
                         f"WU: ORHIGHER gap {_gap_f:+.1f}°F < {WU_MIN_BUFFER_F}°F required",
                         bucket_low=bucket_low, bucket_high=bucket_high)
                use_wunderground = False
                continue
        if bucket_low is None and bucket_high is not None:
            # ORBELOW: forecast must be <= bucket_high - buffer (any market_type)
            _bh_c = (bucket_high - 32.0) / 1.8 if unit.upper() == "F" else bucket_high
            _gap_f = (_bh_c - wu_temp_c) * 1.8
            if _gap_f < WU_MIN_BUFFER_F:
                log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP",
                         f"WU: ORBELOW gap {_gap_f:+.1f}°F < {WU_MIN_BUFFER_F}°F required",
                         bucket_low=bucket_low, bucket_high=bucket_high)
                use_wunderground = False
                continue
        # ──────────────────────────────────────────────────────────────────

        # Log this WU scan for calibration (every valid WU forecast, not just trades).
        # log_wu_scan is duplicate-safe: first insert per (city, date, type) wins.
        # When the market resolves, record_wu_resolution() will fill in actual_temp_c.
        try:
            _bl_c_scan = (bucket_low - 32.0) / 1.8 if (bucket_low is not None and unit.upper() == "F") else bucket_low
            _bh_c_scan = (bucket_high - 32.0) / 1.8 if (bucket_high is not None and unit.upper() == "F") else bucket_high
            log_wu_scan(
                city=city,
                market_date=date_str,
                bucket_type=market_type,
                wu_forecast_c=wu_temp_c,
                bucket_low_c=_bl_c_scan,
                bucket_high_c=_bh_c_scan,
            )
        except Exception as _e:
            logger.debug(f"log_wu_scan call failed: {_e}")

        # Compute realistic probability from the ensemble/t-distribution, not a
        # hardcoded 85%.  WU match means the deterministic forecast falls in this
        # bucket, but the probability must still reflect forecast uncertainty.
        # Use the same probability engine as the non-WU path so calibration
        # applies uniformly.
        forecast_in_unit_wu = convert_forecast_to_market_unit(wu_temp_c, unit)
        prob = wu_empirical_or_normal_probability(
            forecast_temp=forecast_in_unit_wu,
            bucket_low=bucket_low,
            bucket_high=bucket_high,
            unit=unit,
            market_date=date_str,
            city=city,
            market_type=market_type,
        )
        # Cap WU deterministic probability at a realistic maximum.
        # A single 1°C/2°F bucket should never exceed ~40% even with a
        # deterministic match — the market prices these at 2-15% for a reason.
        WU_MAX_PROB = 0.65  # Phase 3: raised from 0.40 — honest ORHIGHER signals can legitimately exceed 40%
        if prob > WU_MAX_PROB:
            logger.info(
                f"WU PROB CAP: {city} {date_str} raw={prob:.1%} → capped at {WU_MAX_PROB:.0%}"
            )
            prob = WU_MAX_PROB
        forecast_celsius = wu_temp_c
        forecast_in_unit = convert_forecast_to_market_unit(wu_temp_c, unit)
        ensemble_prob_val = None
        model_snapshot = None
        if _wu_direct_match:
            logger.info(f"WU TRADE: {city} {date_str} | {wu_reason}")
        else:
            logger.debug(f"WU P2 candidate: {city} {date_str} [{bucket_low}-{bucket_high}]{unit} (wu={wu_temp_c:.1f}C)")

        # Get weather forecast (cached per (city, market_type) per cycle).
        # Highest-temp markets use daily MAX; lowest-temp markets use daily MIN.
        fc_key = (city, market_type)
        if not use_wunderground and fc_key not in forecast_cache:
            try:
                if market_type == "lowest":
                    forecast_cache[fc_key] = get_forecast_low(city, days=FORECAST_DAYS)
                else:
                    forecast_cache[fc_key] = get_forecast(city, days=FORECAST_DAYS)
                # Also fetch per-model ensemble for post-resolution analysis
                ec_key = (city, market_type, "ensemble")
                if ec_key not in ensemble_cache:
                    try:
                        ensemble_cache[ec_key] = get_ensemble_forecast(city, days=FORECAST_DAYS)
                    except Exception:
                        ensemble_cache[ec_key] = None
                # Fetch GFS 30-member ensemble for empirical probability (highest-temp only)
                if market_type != "lowest" and city not in gfs_ensemble_cache:
                    try:
                        gfs_ensemble_cache[city] = get_city_gfs_ensemble(city, days=FORECAST_DAYS)
                    except Exception:
                        gfs_ensemble_cache[city] = {}
                logger.debug(f"Forecast cached for {city} ({market_type})")
                time.sleep(1.0)  # Avoid bursting Open-Meteo rate limits across many cities
            except ValueError as e:
                logger.debug(f"No forecast for city {city} ({market_type}): {e}")
                forecast_cache[fc_key] = None
            except Exception as e:
                logger.warning(f"Weather API error for {city} ({market_type}): {e}")
                forecast_cache[fc_key] = None

        if not use_wunderground:
            forecast_data = forecast_cache[fc_key]
            if forecast_data is None:
                log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP", "no_forecast_available", bucket_low=bucket_low, bucket_high=bucket_high)
                continue

            if date_str not in forecast_data:
                logger.debug(f"Date {date_str} not in forecast window for {city}")
                log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP", "date_out_of_forecast_window", bucket_low=bucket_low, bucket_high=bucket_high)
                continue

            forecast_celsius = forecast_data[date_str]
            forecast_in_unit = convert_forecast_to_market_unit(forecast_celsius, unit)

        # ── GFS Ensemble Stability Check ───────────────────────────────
        if not use_wunderground:
            # Block trading if GFS ensemble can't agree (std ≥ 1.5°C).
            # "GFS doesn't know what's happening → we don't bet."
            if market_type != "lowest" and not is_city_gfs_stable(city, date_str):
                spread = get_gfs_spread(city, date_str)
                logger.info(
                    "BLOCKED (GFS spread %.1f°C ≥ %.1f°C): %s / %s",
                    spread or 99.9, HIGH_GFS_SPREAD_THRESHOLD, city, date_str,
                )
                log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP",
                         "gfs_spread_too_high",
                         bucket_low=bucket_low, bucket_high=bucket_high)
                continue

        # Pre-fetch GFS ensemble members for this city+date (used by
        # empirical probability and calibration routing below).
        gfs_data = gfs_ensemble_cache.get(city, {})
        gfs_members = gfs_data.get(date_str) if gfs_data else None

        # --- Probabilistic forecast: Bayesian METAR for same-day highest-temp markets ---
        use_bayesian = False
        if not use_wunderground and metar_cache and market_type != "lowest" and city in metar_cache:
            try:
                city_tz_str = CITIES[city]["tz"]
                local_now = datetime.now(ZoneInfo(city_tz_str))
                today_local = local_now.strftime("%Y-%m-%d")
                if date_str == today_local:
                    metar_obs = metar_cache[city]
                    observed_c = metar_obs["temp_c"]
                    observed_in_unit = convert_forecast_to_market_unit(observed_c, unit)
                    local_hour = local_now.hour + local_now.minute / 60.0
                    use_bayesian = True
            except Exception:
                pass

        if not use_wunderground:
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
                logger.info(
                    f"Bayesian METAR: {city} {date_str} | "
                    f"obs={observed_in_unit:.1f}{unit} @ {local_hour:.1f}h | "
                    f"fc={forecast_in_unit:.1f}{unit} | prob={prob:.1%}"
                )
            else:
                # ── Empirical GFS Ensemble (primary for highest-temp) ──────────
                # GFS 30-member ensemble: P(bucket) = count(members in bucket) / 30
                # Zero calibration. Physics-based. GFS's own uncertainty estimate.

                if gfs_members and market_type != "lowest":
                    # Convert bucket bounds to Celsius for empirical count
                    if unit.upper() == "F":
                        bl_c = fahrenheit_to_celsius(bucket_low) if bucket_low is not None else None
                        bh_c = fahrenheit_to_celsius(bucket_high) if bucket_high is not None else None
                    else:
                        bl_c, bh_c = bucket_low, bucket_high

                    # Apply per-city, per-horizon GFS warm-bias correction (2026-05-26)
                    # GFS members are systematically warm in many cities — shift threshold
                    # up by the measured bias before counting members in bucket.
                    from strategy import get_gfs_bias_c
                    _horizon = compute_lead_time_days(date_str)
                    _gfs_bias = get_gfs_bias_c(city, _horizon)
                    _bl_c_adj = (bl_c + _gfs_bias) if bl_c is not None else None
                    _bh_c_adj = (bh_c + _gfs_bias) if bh_c is not None else None
                    if _gfs_bias != 0.0:
                        logger.info(
                            f"GFS bias correction: {city} h={_horizon} "
                            f"bias={_gfs_bias:+.2f}°C → threshold shifted"
                        )
                    prob = empirical_probability_with_bias(gfs_members, _bl_c_adj, _bh_c_adj, city=city)
                    logger.info(
                        f"GFS empirical: {city} {date_str} | "
                        f"mean={forecast_in_unit:.1f}{unit} | "
                        f"n={len(gfs_members)} members | bias={_gfs_bias:+.2f}°C | prob={prob:.1%}"
                    )
                    # Skip calibration — empirical is inherently calibrated
                else:
                    # Fallback: Student's t-distribution (lowest-temp or no GFS data)
                    prob = forecast_probability(
                        forecast_temp=forecast_in_unit,
                        bucket_low=bucket_low,
                        bucket_high=bucket_high,
                        unit=unit,
                        market_date=date_str,
                        city=city,
                        forecast_bias=metar_bias_cache.get(city, 0.0),
                        market_type=market_type,
                    )

            # ── CALIBRATION LAYER (t-dist only — empirical skips this) ──
            if not (gfs_members and market_type != "lowest"):
                from calibration import calibrate_probability
                raw_prob = prob
                prob = calibrate_probability(prob)
                if prob != raw_prob:
                    logger.debug(
                        f"CAL: {city} {date_str} raw={raw_prob:.1%} → cal={prob:.1%}"
                    )

            # Compute per-model ensemble for snapshot (trading decision uses prob above)
            model_snapshot = None
            ensemble_prob_val = None
            ec_key = (city, market_type, "ensemble")
            ensemble_data = ensemble_cache.get(ec_key)
            if ensemble_data and date_str in next(iter(ensemble_data.values()), {}):
                try:
                    ens_prob, ens_details = ensemble_probability(
                        ensemble_data, bucket_low, bucket_high,
                        unit=unit, market_date=date_str, city=city,
                        live_bias=metar_bias_cache.get(city, 0.0)
                    )
                    # Build compact JSON snapshot: {model: {temp_c, temp_market, prob}}
                    snap = {}
                    for label, d in ens_details.get("per_model", {}).items():
                        snap[label] = {
                            "temp_c": round(d["temp_c"], 2),
                            "temp_market": round(d["temp_market"], 1),
                            "prob": round(d["prob"], 4),
                        }
                    model_snapshot = json.dumps(snap)
                    ensemble_prob_val = round(ens_prob, 4)
                except Exception as e:
                    logger.debug(f"Ensemble computation skipped for {city}: {e}")

            # ── Ensemble Consensus Filter (stable-markets mode) ──
            # Only trade when ensemble probability is decisive: ≥85% or ≤15%.
            # Anything in between = GFS can't pick a side with conviction.
            # NOAA EMC verification shows mid-range probs have highest error rates.
            ENSEMBLE_HIGH_CONVICTION = 0.85
            ENSEMBLE_LOW_CONVICTION  = 0.15
            if gfs_members:
                if ENSEMBLE_LOW_CONVICTION < prob < ENSEMBLE_HIGH_CONVICTION:
                    logger.info(
                        "SKIP (ensemble indecisive %.1f%%): %s / %s",
                        prob * 100, city, date_str,
                    )
                    log_scan(slug, city, date_str, prob, 0.0, 0.0, "SKIP",
                             "ensemble_low_conviction",
                             forecast_temp=forecast_in_unit,
                             market_unit=unit,
                             bucket_low=bucket_low,
                             bucket_high=bucket_high)
                    notifier.record_trade(entered=False)
                    continue

        # Fetch market price with one re-auth retry (cached per cycle)
        if yes_token_id in price_cache:
            market_price = price_cache[yes_token_id]
        else:
            # Always fetch live order book asks
            book_asks = get_book_asks(yes_token_id, client=clob_client)
            qualifying_asks = [(p, s) for p, s in book_asks if p < prob - ENTRY_THRESHOLD]
            available_usdc = float("inf")  # default; overridden below if book depth is known
            real_market_ask = float(book_asks[0][0]) if book_asks else None  # lowest real ask, or None if no sellers

            if qualifying_asks:
                # FOK-sweep qualifying asks WITH SPREAD CAP.
                # max_fill_price is capped at 3x the cheapest qualifying ask to
                # prevent sweeping into stale/absurd asks far above the market.
                # Effective price is the weighted average of actually swept asks,
                # not the cheapest one — this gives honest edge/Kelly sizing.
                MAX_SWEEP_SPREAD = 3.0   # never pay more than 3x the best ask
                effective_price = qualifying_asks[0][0]       # best ask (anchor)
                raw_max = qualifying_asks[-1][0]              # highest qualifying ask
                max_fill_price = min(raw_max, effective_price * MAX_SWEEP_SPREAD)
                # Recalculate against only the asks we'll actually sweep
                swept_asks = [(p, s) for p, s in qualifying_asks if p <= max_fill_price]
                swept_usdc = sum(p * s for p, s in swept_asks)
                swept_shares = sum(s for _, s in swept_asks)
                if swept_shares > 0:
                    effective_price = swept_usdc / swept_shares  # true blended cost
                available_usdc = swept_usdc
                size = min(2.0, available_usdc)              # cap to real book depth ($2/trade per Boss)
                use_gtc = False                                # FOK sweep, not resting GTC
                price_source = (
                    f"FOK sweep @ ${effective_price:.3f} "
                    f"(band ${qualifying_asks[0][0]:.3f}-${max_fill_price:.3f}, "
                    f"depth=${available_usdc:.2f}, cap={MAX_SWEEP_SPREAD}x)"
                )
            elif not book_asks:
                # Empty book — no sellers at any price. GTC bid would just sit
                # in an empty orderbook forever. Skip.
                logger.info(f"SKIP (empty ask book): {slug} | prob={prob:.1%}")
                log_scan(slug, city, date_str, prob, 0.0, 0.0, "SKIP", "empty_ask_book", forecast_temp=forecast_in_unit, market_unit=unit,
                         market_ask=None, max_bid=None, bucket_low=bucket_low, bucket_high=bucket_high)
                notifier.record_trade(entered=False)
                continue
            else:
                # Asks exist but none inside edge budget. Rest a GTC at best ask
                # so we're in queue if the book softens.
                effective_price = book_asks[0][0]
                price_source = f"GTC @ best_ask {effective_price:.3f}"
                size = 2.0  # $2/trade per Boss directive 2026-05-04
                use_gtc = True
                available_usdc = float("inf")
                max_fill_price = effective_price

            edge = find_edge(prob, effective_price)

            skip_reason = classify_skip_reason(edge, forecast_prob=prob, market_date=date_str, market_type=market_type)
            if not use_wunderground and skip_reason is not None:
                log_scan(slug, city, date_str, prob, effective_price, edge, "PASS", skip_reason, market_ask=real_market_ask, max_bid=effective_price, forecast_temp=forecast_in_unit, market_unit=unit, bucket_low=bucket_low, bucket_high=bucket_high)
                notifier.record_trade(entered=False)
                continue
            if use_wunderground and (effective_price > 0.90 or edge <= 0):
                wu_reason = "WU_SKIP: resolved" if effective_price > 0.90 else "WU_SKIP: negative edge"
                log_scan(slug, city, date_str, prob, effective_price, edge, "SKIP", wu_reason, market_ask=real_market_ask, max_bid=effective_price, forecast_temp=forecast_in_unit, market_unit=unit, bucket_low=bucket_low, bucket_high=bucket_high)
                notifier.record_trade(entered=False)
                continue

            logger.info(
                f"{city} {date_str} | bucket=[{bucket_low},{bucket_high}]{unit} | "
                f"forecast={forecast_in_unit:.1f}{unit} | prob={prob:.1%} | "
                f"price={price_source} | edge={edge:+.1%} → BUY ${size:.2f}"
            )

            # Enforce CLOB minimum tick size. Polymarket rejects orders below $0.01.
            # Sub-tick prices ($0.001 etc.) come from stale last-trade data or
            # thin book levels and must be floored before edge/Kelly calculation.
            MIN_CLOB_PRICE = 0.01
            if effective_price < MIN_CLOB_PRICE:
                logger.debug(
                    f"Sub-tick price {effective_price:.4f} floored to {MIN_CLOB_PRICE} "
                    f"for {slug}"
                )
                effective_price = MIN_CLOB_PRICE
                max_fill_price  = max(max_fill_price, MIN_CLOB_PRICE)

            # Recalculate edge and Kelly at the real price anchor
            edge = find_edge(prob, effective_price)

            skip_reason = classify_skip_reason(edge, forecast_prob=prob, market_date=date_str, market_type=market_type)
            if not use_wunderground and skip_reason is not None:
                log_scan(slug, city, date_str, prob, effective_price, edge, "PASS", skip_reason, market_ask=real_market_ask, max_bid=effective_price, forecast_temp=forecast_in_unit, market_unit=unit, bucket_low=bucket_low, bucket_high=bucket_high)
                notifier.record_trade(entered=False)
                continue
            if use_wunderground and (effective_price > 0.90 or edge <= 0):
                wu_reason = "WU_SKIP: resolved" if effective_price > 0.90 else "WU_SKIP: negative edge"
                log_scan(slug, city, date_str, prob, effective_price, edge, "SKIP", wu_reason, market_ask=real_market_ask, max_bid=effective_price, forecast_temp=forecast_in_unit, market_unit=unit, bucket_low=bucket_low, bucket_high=bucket_high)
                notifier.record_trade(entered=False)
                continue

            size = get_safe_position_size(cycle_bankroll, edge, prob, effective_price)
            # Cap to actual available book liquidity when known
            if available_usdc != float("inf"):
                size = min(size, available_usdc)

            # Lottery cap: HK/Seoul now in BLOCKED_CITIES (never trade).
            # This code path is kept for any future lottery cities.

            logger.info(
                f"{city} {date_str} | bucket=[{bucket_low},{bucket_high}]{unit} | "
                f"forecast={forecast_in_unit:.1f}{unit} | prob={prob:.1%} | "
            )

            signals.append({
                "slug": slug,
                "city": city,
                "date_str": date_str,
                "bucket_low": bucket_low,
                "bucket_high": bucket_high,
                "unit": unit,
                "question": question,
                "yes_token_id": yes_token_id,
                "condition_id": meta.get("condition_id", ""),
                "forecast_celsius": forecast_celsius,
                "forecast_in_unit": forecast_in_unit,
                "prob": prob,
                "market_price": effective_price,
                "max_fill_price": max_fill_price,
                "edge": edge,
                "size": size,
                "illiquid": True,
                "use_gtc": use_gtc,
                "market_ask": real_market_ask,
                "max_bid": effective_price,
                "model_snapshot": model_snapshot,
                "ensemble_prob": ensemble_prob_val,
                "market_type": market_type,
                "wu_source": use_wunderground,  # True if WU-powered signal
                "is_lottery": False,  # Lottery cities removed — HK/Seoul now in WATCH_ONLY_CITIES
            })
            continue

        # Liquid market: normal path (cached per cycle)
        if market_price is None:
            market_price = price_cache.get(yes_token_id)
        if market_price is None:
            market_price = get_market_price(yes_token_id, client=clob_client)
            if market_price is not None:
                price_cache[yes_token_id] = market_price
        if market_price is None:
            logger.debug(f"Could not fetch price for {slug}")
            log_scan(slug, city, date_str, prob, 0.0, 0.0, "SKIP", "no_price_available", forecast_temp=forecast_in_unit, market_unit=unit, bucket_low=bucket_low, bucket_high=bucket_high)
            continue

        if market_price < MIN_PRICE_FLOOR:
            logger.info(f"SKIP (price floor): {slug} | mkt={market_price:.3f}")
            log_scan(slug, city, date_str, prob, market_price, 0.0, "SKIP", "price_below_floor", forecast_temp=forecast_in_unit, market_unit=unit, bucket_low=bucket_low, bucket_high=bucket_high)
            notifier.record_trade(entered=False)
            continue

        edge = find_edge(prob, market_price)

        logger.info(
            f"{city} {date_str} | bucket=[{bucket_low},{bucket_high}]{unit} | "
            f"forecast={forecast_in_unit:.1f}{unit} | prob={prob:.1%} | "
            f"mkt={market_price:.1%} | edge={edge:+.1%}"
        )

        skip_reason = classify_skip_reason(edge, forecast_prob=prob, market_date=date_str, market_type=market_type)
        if not use_wunderground and skip_reason is not None:
            log_scan(slug, city, date_str, prob, market_price, edge, "PASS", skip_reason, forecast_temp=forecast_in_unit, market_unit=unit, bucket_low=bucket_low, bucket_high=bucket_high)
            notifier.record_trade(entered=False)
            continue
        if use_wunderground and (market_price > 0.90 or edge <= 0):
            wu_reason = "WU_SKIP: resolved" if market_price > 0.90 else "WU_SKIP: negative edge"
            log_scan(slug, city, date_str, prob, market_price, edge, "SKIP", wu_reason, forecast_temp=forecast_in_unit, market_unit=unit, bucket_low=bucket_low, bucket_high=bucket_high)
            notifier.record_trade(entered=False)
            continue

        size = get_safe_position_size(cycle_bankroll, edge, prob, market_price)
        signals.append({
            "slug": slug,
            "city": city,
            "date_str": date_str,
            "bucket_low": bucket_low,
            "bucket_high": bucket_high,
            "unit": unit,
            "question": question,
            "yes_token_id": yes_token_id,
            "condition_id": meta.get("condition_id", ""),
            "forecast_celsius": forecast_celsius,
            "forecast_in_unit": forecast_in_unit,
            "prob": prob,
            "market_price": market_price,
            "edge": edge,
            "size": size,
            "illiquid": False,
            "market_ask": market_price,
            "max_bid": market_price,
            "model_snapshot": model_snapshot,
            "ensemble_prob": ensemble_prob_val,
            "market_type": market_type,
            "wu_source": use_wunderground,  # True if WU-powered signal
            "is_lottery": False,  # Lottery cities removed — HK/Seoul now in WATCH_ONLY_CITIES
            "is_watch_only": _city_watch_only,  # True for watch-only cities (scan but don't trade)
        })
    # Even after edge + prob-floor gates, a single market can produce multiple
    # qualifying buckets (e.g. both "70-72F" and "73-75F" have edge). Buying
    # multiple buckets in the same market is a shotgun spray: they are mutually
    # exclusive outcomes, so at most one can win. Keep ONLY the highest-prob
    # qualifying bucket per (city, date) and log the rest as skipped for
    # auditability.
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for s in signals:
        groups[(s["city"], s["date_str"])].append(s)

    filtered = []
    dropped_lower_prob = 0
    dropped_soft_floor = 0
    for key, group in groups.items():
        group.sort(key=lambda s: s["prob"], reverse=True)
        best = group[0]

        # ── ORHIGHER/ORBELOW SMART FALLBACK ────────────────────────────
        # For cities where Polymarket only offers a subset of possible
        # temperature brackets (US-F ORHIGHER/ORBELOW markets), the model
        # may correctly identify a winning bucket that isn't offered. When
        # the forecast falls outside ALL listed bucket bounds, pick the
        # closest available bucket instead.
        if best["city"] in ORHIGHER_ORBELOW_CITIES:
            fc = best.get("forecast_in_unit")
            if fc is not None:
                # Collect all boundary values from this city-date's buckets
                all_bounds = []
                for s in group:
                    if s["bucket_low"] is not None:
                        all_bounds.append((s["bucket_low"], "low", s))
                    if s["bucket_high"] is not None:
                        all_bounds.append((s["bucket_high"], "high", s))
                if all_bounds:
                    vals = [b[0] for b in all_bounds]
                    lo, hi = min(vals), max(vals)
                    if fc < lo:
                        # Forecast below lowest offered → use lowest
                        closest = min(all_bounds, key=lambda b: abs(b[0] - fc))
                        best = closest[2]
                        logger.info(
                            f"ORBELOW FALLBACK: {best['city']} {best['date_str']} "
                            f"fc={fc:.1f}{best.get('unit','')} < min={lo:.0f} → "
                            f"using lowest bucket {best['slug']}"
                        )
                    elif fc > hi:
                        # Forecast above highest offered → use highest
                        closest = min(all_bounds, key=lambda b: abs(b[0] - fc))
                        best = closest[2]
                        logger.info(
                            f"ORHIGHER FALLBACK: {best['city']} {best['date_str']} "
                            f"fc={fc:.1f}{best.get('unit','')} > max={hi:.0f} → "
                            f"using highest bucket {best['slug']}"
                        )
        # ────────────────────────────────────────────────────────────────

        # Soft absolute backstop: even the highest-prob bucket in this market
        # must clear SOFT_MIN_PROB (or LOWEST_SOFT_MIN_PROB for lowest-temp).
        # If the best candidate in the whole market only reaches e.g. 18%,
        # confidence is too low regardless of edge.
        soft_floor = LOWEST_SOFT_MIN_PROB if best.get("market_type") == "lowest" else SOFT_MIN_PROB
        if best["prob"] < soft_floor:
            dropped_soft_floor += 1
            log_scan(
                best["slug"], best["city"], best["date_str"],
                best["prob"], best["market_price"], best["edge"],
                "SKIP", "best_bucket_below_soft_floor",
                market_ask=best.get("market_ask"),
                max_bid=best.get("max_bid"),
                forecast_temp=best.get("forecast_in_unit"),
                market_unit=best.get("unit", ""),
                bucket_low=best["bucket_low"],
                bucket_high=best["bucket_high"],
            )
            # Discard all siblings too — no point trading any bucket in this market
            for losing in group[1:]:
                dropped_lower_prob += 1
                log_scan(
                    losing["slug"], losing["city"], losing["date_str"],
                    losing["prob"], losing["market_price"], losing["edge"],
                    "SKIP", "lower_prob_bucket_in_same_market",
                    market_ask=losing.get("market_ask"),
                    max_bid=losing.get("max_bid"),
                    forecast_temp=losing.get("forecast_in_unit"),
                    market_unit=losing.get("unit", ""),
                    bucket_low=losing["bucket_low"],
                    bucket_high=losing["bucket_high"],
                )
            continue

        filtered.append(best)
        for losing in group[1:]:
            dropped_lower_prob += 1
            log_scan(
                losing["slug"], losing["city"], losing["date_str"],
                losing["prob"], losing["market_price"], losing["edge"],
                "SKIP", "lower_prob_bucket_in_same_market",
                market_ask=losing.get("market_ask"),
                max_bid=losing.get("max_bid"),
                forecast_temp=losing.get("forecast_in_unit"),
                market_unit=losing.get("unit", ""),
                bucket_low=losing["bucket_low"],
                bucket_high=losing["bucket_high"],
            )

    # Execute highest-probability bets first so daily spend limit doesn't
    # starve high-confidence signals if bankroll fills up mid-cycle.
    filtered.sort(key=lambda s: s["prob"], reverse=True)

    if dropped_lower_prob or dropped_soft_floor:
        logger.info(
            f"Per-market filter: kept {len(filtered)} bets | "
            f"dropped {dropped_soft_floor} markets (best bucket < {SOFT_MIN_PROB:.0%} soft floor) | "
            f"dropped {dropped_lower_prob} lower-prob siblings"
        )

    # Phase 2b: METAR pre-trade block — MANDATORY safety gate
    # WU-generated signals bypass METAR: the resolution source IS the authority.
    # If METAR cache is empty, we CANNOT verify GFS trades. Skip GFS only.
    from metar_block import check_all_blocked
    wu_signals = [b for b in filtered if b.get("wu_source")]
    gfs_signals = [b for b in filtered if not b.get("wu_source")]
    if wu_signals:
        logger.info(f"WU signals pass METAR gate: {len(wu_signals)} (METAR bypass)")
    if not metar_cache:
        logger.warning("METAR cache EMPTY — skipping GFS trades this cycle (uncertified)")
        for b in gfs_signals:
            log_scan(b["slug"], b["city"], b["date_str"], b["prob"],
                     b.get("market_price", 0), b.get("edge", 0),
                     "SKIP", "metar_unavailable",
                     market_ask=b.get("market_ask"), max_bid=b.get("max_bid"),
                     forecast_temp=b.get("forecast_in_unit"),
                     market_unit=b.get("unit", ""),
                     bucket_low=b["bucket_low"], bucket_high=b["bucket_high"])
        gfs_signals = []
    else:
        gfs_signals, blocked_metar = check_all_blocked(gfs_signals, metar_cache)
        if blocked_metar:
            for b in blocked_metar:
                log_scan(b["slug"], b["city"], b["date_str"], b["prob"],
                         b.get("market_price", 0), b.get("edge", 0),
                         "SKIP", "metar_block",
                         market_ask=b.get("market_ask"), max_bid=b.get("max_bid"),
                         forecast_temp=b.get("forecast_in_unit"),
                         market_unit=b.get("unit", ""),
                         bucket_low=b["bucket_low"], bucket_high=b["bucket_high"])
            logger.info(
                f"METAR block: {len(blocked_metar)} GFS trade(s) skipped, "
                f"{len(gfs_signals)} remaining"
            )
    filtered = wu_signals + gfs_signals

    # ── Watch-only city filter ─────────────────────────────────────────
    # Watch-only cities are fully scanned (forecast, prob, edge all computed)
    # but no orders are placed. Log them as WATCH_ONLY_SKIP for audit trail.
    watch_only_signals = [s for s in filtered if s.get("is_watch_only")]
    tradable_signals = [s for s in filtered if not s.get("is_watch_only")]
    for s in watch_only_signals:
        log_scan(s["slug"], s["city"], s["date_str"], s["prob"],
                 s.get("market_price", 0), s.get("edge", 0),
                 "SKIP", "watch_only_city",
                 market_ask=s.get("market_ask"), max_bid=s.get("max_bid"),
                 forecast_temp=s.get("forecast_in_unit"),
                 market_unit=s.get("unit", ""),
                 bucket_low=s["bucket_low"], bucket_high=s["bucket_high"])
    if watch_only_signals:
        logger.info(f"Watch-only filter: {len(watch_only_signals)} signals scanned but not traded")
    filtered = tradable_signals

    # Phase 2c: Unresolved exposure cap — don't commit more than 50% of bankroll
    if filtered:
        open_exposure = get_total_open_exposure()
        exposure_ratio = open_exposure / max(cycle_bankroll, 1.0)
        if exposure_ratio > UNRESOLVED_EXPOSURE_CAP:
            logger.warning(
                f"EXPOSURE CAP BREACH: ${open_exposure:.2f} open / "
                f"${cycle_bankroll:.2f} bankroll = {exposure_ratio:.0%} "
                f"(limit {UNRESOLVED_EXPOSURE_CAP:.0%}). "
                f"SKIPPING all {len(filtered)} remaining signals."
            )
            for sig in filtered:
                log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                         sig.get("market_price", 0), sig.get("edge", 0),
                         "SKIP", "exposure_cap",
                         market_ask=sig.get("market_ask"), max_bid=sig.get("max_bid"),
                         forecast_temp=sig.get("forecast_in_unit"),
                         market_unit=sig.get("unit", ""),
                         bucket_low=sig["bucket_low"], bucket_high=sig["bucket_high"])
            filtered = []
        elif exposure_ratio > UNRESOLVED_EXPOSURE_CAP * 0.5:
            logger.info(
                f"Exposure warning: ${open_exposure:.2f} open / "
                f"${cycle_bankroll:.2f} bankroll = {exposure_ratio:.0%}"
            )

    # Phase 3: Execute orders
    for sig in filtered:
        if total_spent + sig["size"] > MAX_DAILY_SPEND:
            logger.warning(
                f"Daily spend limit reached (${total_spent:.2f} + ${sig['size']:.2f} "
                f"> ${MAX_DAILY_SPEND:.2f}). Stopping."
            )
            log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                     sig["market_price"], sig["edge"], "SKIP", "daily_spend_limit", market_ask=sig.get("market_ask"), max_bid=sig.get("max_bid"), forecast_temp=sig.get("forecast_in_unit"), market_unit=sig.get("unit",""), bucket_low=sig["bucket_low"], bucket_high=sig["bucket_high"])
            break

        # Route illiquid markets via book-sweep (FOK) or last-trade GTC.
        # Liquid markets use the standard FOK path below.
        if sig.get("illiquid"):
            if sig.get("use_gtc"):
                # Path B: resting GTC at best_ask or model_price (no qualifying sweep depth)
                logger.info(
                    f"GTC BID @ ${sig['market_price']:.3f}: {sig['slug']} | "
                    f"prob={sig['prob']:.1%} | size=${sig['size']:.2f}"
                )
                exec_resp = place_gtc_order(
                    token_id=sig["yes_token_id"],
                    price=sig["market_price"],
                    size_usdc=sig["size"],
                    wait_seconds=90.0,
                )
                exec_label = "GTC"
            else:
                # Path A: book sweep - FOK at highest qualifying ask price
                logger.info(
                    f"FOK SWEEP: {sig['slug']} | prob={sig['prob']:.1%} | "
                    f"price={sig['max_fill_price']:.3f} | size=${sig['size']:.2f}"
                )
                raw_resp = place_buy_order(
                    token_id=sig["yes_token_id"],
                    price=sig["max_fill_price"],
                    size_usdc=sig["size"],
                )
                # Normalise FOK response to the shared exec_resp shape
                fok_status = (raw_resp.get("status") or "FAILED").lower()
                if fok_status == "matched":
                    # V2 returns amounts as strings — parse to float
                    raw_taking = raw_resp.get("takingAmount", "")
                    raw_making = raw_resp.get("makingAmount", "")
                    try:
                        actual_shares = float(raw_taking) if raw_taking else 0.0
                        actual_spent  = float(raw_making) if raw_making else 0.0
                    except (ValueError, TypeError):
                        actual_shares = 0.0
                        actual_spent  = 0.0
                    if actual_shares > 0 and actual_spent > 0:
                        exec_resp = {
                            "status": "FILLED",
                            "total_spent": actual_spent,
                            "total_shares": actual_shares,
                            "avg_price": actual_spent / actual_shares,
                            "order_id": raw_resp.get("orderID", ""),
                        }
                    else:
                        exec_resp = {
                            "status": "NONE",
                            "total_shares": 0.0,
                            "total_spent": 0.0,
                            "avg_price": 0.0,
                            "order_id": raw_resp.get("orderID", ""),
                            "reason": "matched_but_empty_amounts",
                        }
                elif fok_status == "delayed":
                    # V2 FOK accepted but awaiting on-chain settlement.
                    # When success=True, order WILL settle — record with estimated
                    # amounts so position tracking isn't lost (Cape Town bug).
                    order_success = raw_resp.get("success", False)
                    if order_success:
                        # Use API-provided amounts if available; otherwise estimate
                        taking_raw = raw_resp.get("takingAmount", "")
                        making_raw = raw_resp.get("makingAmount", "")
                        try:
                            est_shares = (
                                float(taking_raw)
                                if taking_raw and taking_raw != ""
                                else round(sig["size"] / sig["max_fill_price"], 2)
                            )
                            est_spent = (
                                float(making_raw)
                                if making_raw and making_raw != ""
                                else sig["size"]
                            )
                        except (ValueError, TypeError):
                            est_shares = round(sig["size"] / sig["max_fill_price"], 2)
                            est_spent = sig["size"]
                        exec_resp = {
                            "status": "DELAYED",
                            "total_spent": est_spent,
                            "total_shares": est_shares,
                            "avg_price": (
                                round(est_spent / est_shares, 4)
                                if est_shares > 0
                                else sig["max_fill_price"]
                            ),
                            "order_id": raw_resp.get("orderID", ""),
                            "reason": "delayed_settling",
                        }
                    else:
                        exec_resp = {
                            "status": "DELAYED",
                            "total_spent": 0.0,
                            "total_shares": 0.0,
                            "avg_price": 0.0,
                            "order_id": raw_resp.get("orderID", ""),
                            "reason": "fok_delayed_pending_settlement",
                        }
                else:
                    exec_resp = {
                        "status": fok_status,
                        "total_spent": 0.0,
                        "total_shares": 0.0,
                        "avg_price": 0.0,
                        "order_id": raw_resp.get("orderID", ""),
                        "reason": raw_resp.get("reason", "fok_not_matched"),
                    }
                exec_label = "FOK_SWEEP"

            exec_status = exec_resp.get("status", "FAILED")

            if exec_status == "FAILED":
                # API-level rejection (e.g. CLOB V2 decimal precision,
                # malformed payload) — NOT a liquidity issue.
                reason_str = exec_resp.get("reason", "unknown")
                logger.error(
                    f"ORDER REJECTED (FAILED): {sig['slug']} | "
                    f"{reason_str}"
                )
                log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                         sig["market_price"], sig["edge"], "SKIP", "order_rejected",
                         market_ask=sig.get("market_ask"), max_bid=sig.get("max_bid"),
                         forecast_temp=sig.get("forecast_in_unit"),
                         market_unit=sig.get("unit", ""),
                         bucket_low=sig["bucket_low"],
                         bucket_high=sig["bucket_high"])
                notifier.record_trade(entered=False)
                continue

            # DELAYED with estimated fill → record as trade, not skip
            if exec_status == "DELAYED" and exec_resp.get("total_shares", 0) > 0:
                logger.info(
                    f"DELAYED ORDER RECORDING: {sig['slug']} | "
                    f"est_shares={exec_resp['total_shares']:.2f} | "
                    f"est_spent=${exec_resp['total_spent']:.2f}"
                )
                # Fall through to normal recording below
            elif exec_status in ("NONE", "DELAYED") or exec_resp.get("total_shares", 0) <= 0:
                skip_label = "illiquid_delayed" if exec_status == "DELAYED" else "illiquid_no_fill"
                logger.info(f"ILLIQUID SKIP ({skip_label}): {sig['slug']}")
                log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                         sig["market_price"], sig["edge"], "SKIP", skip_label, market_ask=sig.get("market_ask"), max_bid=sig.get("max_bid"), forecast_temp=sig.get("forecast_in_unit"), market_unit=sig.get("unit",""), bucket_low=sig["bucket_low"], bucket_high=sig["bucket_high"])
                notifier.record_trade(entered=False)
                continue

            # Build unified order_response for downstream logging/DB
            actual_spent  = exec_resp["total_spent"]
            actual_shares = exec_resp["total_shares"]
            actual_price  = exec_resp["avg_price"]

            order_response = {
                "orderID": exec_resp.get("order_id", ""),
                "status": "MATCHED",
                "price": actual_price,
                "size": actual_shares,
                "size_usdc": actual_spent,
                "exec_type": exec_label,
            }

            log_trade(
                market_slug=sig["slug"],
                city=sig["city"],
                date=sig["date_str"],
                forecast_temp_c=sig["forecast_celsius"],
                forecast_temp_market_unit=sig["forecast_in_unit"],
                market_unit=sig["unit"],
                bucket_low=sig["bucket_low"],
                bucket_high=sig["bucket_high"],
                forecast_prob=sig["prob"],
                market_price=actual_price,
                edge=sig["edge"],
                size_usdc=actual_spent,
                dry_run=DRY_RUN,
                order_response=order_response,
                question=sig["question"],
            )

            log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                     actual_price, sig["edge"],
                     "TRADE", f"{exec_label} ${actual_spent:.2f} @ avg ${actual_price:.4f}", market_ask=sig.get("market_ask"), max_bid=sig.get("max_bid"), forecast_temp=sig.get("forecast_in_unit"), market_unit=sig.get("unit",""), bucket_low=sig["bucket_low"], bucket_high=sig["bucket_high"])

            _traded_tokens.add(sig["yes_token_id"])

            # Record position with actual fill data
            try:
                record_entry(
                    token_id=sig["yes_token_id"],
                    condition_id=sig["condition_id"],
                    slug=sig["slug"],
                    city=sig["city"],
                    market_date=sig["date_str"],
                    bucket_low=sig["bucket_low"],
                    bucket_high=sig["bucket_high"],
                    unit=sig["unit"],
                    entry_price=actual_price,
                    shares=actual_shares,
                    size_usdc=actual_spent,
                    order_id=order_response["orderID"],
                    neg_risk=False,
                    question=sig["question"],
                    forecast_prob=sig["prob"],
                    market_prob=actual_price,
                    edge=sig["edge"],
                    entry_method=exec_label.lower(),
                    model_snapshot=sig.get("model_snapshot"),
                    ensemble_prob=sig.get("ensemble_prob"),
                    market_type=sig.get("market_type", "highest"),
                    wu_source=sig.get("wu_source", False),
                    wu_forecast_c=(sig.get("forecast_celsius") if sig.get("wu_source") else None),
                    forecast_temp_c=sig.get("forecast_celsius"),
                )
            except Exception as e:
                logger.error(f"Failed to record illiquid position in DB: {e}")

            notifier.record_trade(entered=True, size_usdc=actual_spent)
            notifier.notify_trade(
                slug=sig["slug"],
                city=sig["city"],
                date_str=sig["date_str"],
                edge=sig["edge"],
                size_usdc=actual_spent,
                price=actual_price,
                prob=sig["prob"],
                order_status=exec_label,
            )

            trades_placed += 1
            total_spent += actual_spent
            continue

        # ---- Normal (liquid) market path ----
        logger.info(
            f"EDGE FOUND: {sig['slug']} | edge={sig['edge']:.1%} | "
            f"size=${sig['size']:.2f} | price={sig['market_price']:.3f}"
        )

        order_response = place_buy_order(
            token_id=sig["yes_token_id"],
            price=sig["market_price"],
            size_usdc=sig["size"],
        )

        order_status = order_response.get("status", "UNKNOWN") if order_response else "FAILED"
        exec_label = "FOK"

        # FOK killed (thin book near close) — fall back to GTC at live ask price
        if order_status not in ("simulated", "MATCHED", "LIVE"):
            # Fetch live best ask so GTC rests at the market's offer, not stale midpoint
            _ask_book = get_book_asks(sig["yes_token_id"], client=clob_client)
            _gtc_price = _ask_book[0][0] if _ask_book else sig["market_price"]
            logger.info(
                f"FOK killed for {sig['slug']} — falling back to GTC "
                f"@ ask {_gtc_price:.3f} (midpoint was {sig['market_price']:.3f})"
            )
            gtc_resp = place_gtc_order(
                token_id=sig["yes_token_id"],
                price=_gtc_price,
                size_usdc=sig["size"],
            )
            gtc_status = gtc_resp.get("status", "FAILED") if gtc_resp else "FAILED"
            if gtc_status not in ("FAILED", "UNKNOWN"):
                order_response = gtc_resp
                order_status = gtc_status
                exec_label = "GTC_FALLBACK"
                logger.info(f"GTC fallback placed for {sig['slug']} | status={gtc_status}")
            else:
                logger.warning(f"GTC fallback also failed for {sig['slug']}: {gtc_resp}")
                log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                         sig["market_price"], sig["edge"], "SKIP", "fok_and_gtc_failed", market_ask=sig.get("market_ask"), max_bid=sig.get("max_bid"), forecast_temp=sig.get("forecast_in_unit"), market_unit=sig.get("unit",""), bucket_low=sig["bucket_low"], bucket_high=sig["bucket_high"])
                notifier.record_trade(entered=False)
                continue

        actual_size  = sig["size"] if order_status in ("simulated", "MATCHED", "LIVE") else 0.0
        actual_price = sig["market_price"]

        log_trade(
            market_slug=sig["slug"],
            city=sig["city"],
            date=sig["date_str"],
            forecast_temp_c=sig["forecast_celsius"],
            forecast_temp_market_unit=sig["forecast_in_unit"],
            market_unit=sig["unit"],
            bucket_low=sig["bucket_low"],
            bucket_high=sig["bucket_high"],
            forecast_prob=sig["prob"],
            market_price=actual_price,
            edge=sig["edge"],
            size_usdc=actual_size,
            dry_run=DRY_RUN,
            order_response=order_response,
            question=sig["question"],
        )

        log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                 actual_price, sig["edge"],
                 "TRADE", f"{exec_label} size=${actual_size:.2f}", market_ask=sig.get("market_ask"), max_bid=sig.get("max_bid"), forecast_temp=sig.get("forecast_in_unit"), market_unit=sig.get("unit",""), bucket_low=sig["bucket_low"], bucket_high=sig["bucket_high"])

        _traded_tokens.add(sig["yes_token_id"])

        # Record position in SQLite for monitoring/resolution/redemption
        if order_status in ("simulated", "MATCHED", "LIVE"):
            try:
                num_shares = actual_size / actual_price if actual_price > 0 else 0
                record_entry(
                    token_id=sig["yes_token_id"],
                    condition_id=sig["condition_id"],
                    slug=sig["slug"],
                    city=sig["city"],
                    market_date=sig["date_str"],
                    bucket_low=sig["bucket_low"],
                    bucket_high=sig["bucket_high"],
                    unit=sig["unit"],
                    entry_price=actual_price,
                    shares=num_shares,
                    size_usdc=actual_size,
                    order_id=order_response.get("orderID", ""),
                    neg_risk=False,
                    question=sig["question"],
                    forecast_prob=sig["prob"],
                    market_prob=actual_price,
                    edge=sig["edge"],
                    entry_method=exec_label.lower(),
                    model_snapshot=sig.get("model_snapshot"),
                    ensemble_prob=sig.get("ensemble_prob"),
                    market_type=sig.get("market_type", "highest"),
                    wu_source=sig.get("wu_source", False),
                    wu_forecast_c=(sig.get("forecast_celsius") if sig.get("wu_source") else None),
                    forecast_temp_c=sig.get("forecast_celsius"),
                )
            except Exception as e:
                logger.error(f"Failed to record position in DB: {e}")

        # Record trade in notifier and send alert
        notifier.record_trade(entered=True, size_usdc=actual_size)
        notifier.notify_trade(
            slug=sig["slug"],
            city=sig["city"],
            date_str=sig["date_str"],
            edge=sig["edge"],
            size_usdc=actual_size,
            price=actual_price,
            prob=sig["prob"],
            order_status=exec_label,
        )

        trades_placed += 1
        total_spent += actual_size

    elapsed = (datetime.now() - cycle_start).total_seconds()
    logger.info(
        f"=== Scan cycle complete: {trades_placed} trade(s), "
        f"${total_spent:.2f} deployed, {elapsed:.1f}s elapsed ==="
    )

    # Send cycle summary to Telegram (only if something happened)
    if trades_placed > 0 or markets_scanned > 0:
        notifier.notify_cycle_summary(trades_placed, total_spent, markets_scanned, elapsed)

    # Phase 4: Monitor open positions for exits and profit-taking
    try:
        monitor_result = monitor_positions(notifier=notifier)
        if monitor_result["exits_triggered"] > 0:
            logger.info(
                f"Position monitor: {monitor_result['exits_executed']} exit(s) "
                f"executed out of {monitor_result['exits_triggered']} triggered"
            )
    except Exception as e:
        logger.error(f"Position monitor error: {e}", exc_info=True)


# -----------------------------------------------------------------------
# Daily report job
# -----------------------------------------------------------------------
def _send_daily_report() -> None:
    """Scheduled job: sends the daily summary report via Telegram."""
    logger.info("Sending daily Telegram report...")
    try:
        balance = get_current_bankroll()
        notifier.send_daily_report(balance)
    except Exception as e:
        logger.error(f"Failed to send daily report: {e}")


# -----------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------
def main() -> None:
    # Ignore SIGHUP so logrotate (or any future cron) can't kill us.
    # Fixed 2026-06-11: /etc/logrotate.d/weatherbot had `postrotate {
    # systemctl kill -s HUP weatherbot.service }`, and Python's default
    # SIGHUP handler is to terminate. logrotate now uses copytruncate,
    # but this is belt-and-suspenders — if any future config change
    # re-introduces a SIGHUP, we ignore it instead of dying.
    # hasattr guard for portability (SIGHUP is not defined on Windows).
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        logger.info("SIGHUP ignored (logrotate/detach-safe)")

    logger.info("=" * 60)
    logger.info("Polymarket Weather Bot starting")
    mode_str = "DRY RUN (paper trading)" if DRY_RUN else "*** LIVE TRADING ***"
    logger.info(f"Mode: {mode_str}")
    logger.info(f"Bankroll: ${BANKROLL:.2f} USDC")
    logger.info(f"Scan interval: every {SCAN_INTERVAL} minutes")
    logger.info(f"Fast monitor: every {FAST_MONITOR_INTERVAL} minutes (when positions <12h out)")
    logger.info(f"Forecast horizon: {FORECAST_DAYS} days")
    logger.info(f"Entry threshold: edge > {ENTRY_THRESHOLD:.0%}")
    logger.info("=" * 60)

    # Initialize positions database
    init_db()

    # Send startup notification
    notifier.notify_startup(
        get_current_bankroll(),
        "LIVE" if not DRY_RUN else "DRY RUN",
        SCAN_INTERVAL,
    )

    # Log open positions on startup
    open_pos = get_open_positions()
    if open_pos:
        logger.info(f"Startup: {len(open_pos)} open position(s) in database")
    else:
        logger.info("Startup: no open positions")

    # Initialize daily stats with a fresh balance (not the startup BANKROLL
    # which may have used the $200 fallback if the API was slow on boot)
    fresh_balance = get_current_bankroll()
    notifier.reset_daily(fresh_balance)

    # Start heartbeat thread (no-op in DRY_RUN)
    start_heartbeat()

    # Run one cycle immediately on start
    run_cycle()

    # Schedule recurring scan cycles (30 min)
    schedule.every(SCAN_INTERVAL).minutes.do(run_cycle)

    # Schedule fast position monitor (15 min, only runs when needed)
    schedule.every(FAST_MONITOR_INTERVAL).minutes.do(_fast_monitor_cycle)
    logger.info(f"Fast monitor: every {FAST_MONITOR_INTERVAL} min (active when positions <12h out)")

    # Schedule daily report at configured time
    report_time = f"{DAILY_REPORT_HOUR:02d}:{DAILY_REPORT_MINUTE:02d}"
    schedule.every().day.at(report_time).do(_send_daily_report)
    logger.info(f"Daily report scheduled at {report_time} UTC")

    # Schedule resolution checker + redemption daily
    resolved_time = f"{RESOLVED_CHECK_HOUR:02d}:{RESOLVED_CHECK_MINUTE:02d}"
    schedule.every().day.at(resolved_time).do(_daily_resolve_and_redeem)
    logger.info(f"Resolution + redemption scheduled daily at {resolved_time} UTC")

    logger.info(f"Scheduler running. Next scan in {SCAN_INTERVAL} minutes.")
    logger.info("Press Ctrl+C to stop. In screen session: Ctrl+A then D to detach.")

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
        notifier.notify_error("Main Loop", str(e))
    finally:
        stop_heartbeat()
        notifier.notify_error("Shutdown", "Bot is shutting down")
        notifier.shutdown()
        logger.info("Shutdown complete.")

# -----------------------------------------------------------------------
# Resolution checker + Redemption (daily job)
# -----------------------------------------------------------------------
def _daily_resolve_and_redeem() -> None:
    """
    Daily job: resolve past positions and redeem winners.
    1. Check all open positions with past market dates against actual temps
    2. Mark as resolved_won or resolved_lost
    3. Redeem winning positions for USDC
    """
    logger.info("=== Daily resolution + redemption cycle ===")

    # Step 1: Resolve past positions
    try:
        resolve_result = resolve_past_positions(notifier=notifier)
        logger.info(
            f"Resolution: {resolve_result['won']} won, {resolve_result['lost']} lost, "
            f"{resolve_result['boundary_flags']} boundary flag(s)"
        )
    except Exception as e:
        logger.error(f"Resolution checker error: {e}", exc_info=True)
        notifier.notify_error("Resolution", str(e))

    # Step 2: Redeem winning positions
    try:
        redeem_result = redeem_all_winners(notifier=notifier)
        if redeem_result["redeemed"] > 0:
            logger.info(
                f"Redemption: {redeem_result['redeemed']}/{redeem_result['total']} "
                f"redeemed, ${redeem_result['total_payout']:.2f} collected"
            )
    except Exception as e:
        logger.error(f"Redemption error: {e}", exc_info=True)
        notifier.notify_error("Redemption", str(e))


# -----------------------------------------------------------------------
# Fast position monitor (15-min loop for positions near resolution)
# -----------------------------------------------------------------------
def _fast_monitor_cycle() -> None:
    """
    Runs every 15 minutes ONLY when positions are within 12 hours of
    resolution. Checks for same-day exit signals and profit-taking
    with higher urgency.
    """
    if not needs_fast_monitoring():
        return  # No positions close to resolution, skip

    logger.info("Fast monitor cycle (positions within 12h of resolution)")
    try:
        result = monitor_positions(notifier=notifier)
        if result["exits_triggered"] > 0:
            logger.info(
                f"Fast monitor: {result['exits_executed']} exit(s) "
                f"from {result['positions_checked']} position(s)"
            )
    except Exception as e:
        logger.error(f"Fast monitor error: {e}", exc_info=True)


if __name__ == "__main__":
    # Set real bankroll dynamically
    BANKROLL = get_current_bankroll()
    main()
