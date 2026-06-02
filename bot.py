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

import schedule
import time
import threading
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta

import pytz
from weather import get_forecast, celsius_to_fahrenheit, get_ensemble_spread
from markets import (
    get_weather_markets,
    parse_market_metadata,
    get_market_price,
    get_midpoint_price,
    get_book_snapshot,
    is_book_illiquid,
    get_client as get_clob_client,
)
from strategy import (
    forecast_probability,
    find_edge,
    should_trade,
    kelly_position_size,
    convert_forecast_to_market_unit,
    get_prob_floor,
    ENTRY_THRESHOLD,
    MIN_HOURS_TO_RES,
)
from executor import place_buy_order, place_ladder_bids, DRY_RUN
from logger import log_trade, log_scan
from notifier import TelegramNotifier
from positions import record_entry, is_token_traded_today, get_open_positions, init_db
from position_monitor import (
    monitor_positions,
    resolve_past_positions,
    needs_fast_monitoring,
    get_hours_to_resolution as _get_hours_to_res,
)
from weather import CITIES as _CITIES_REF
from observed_temps import get_current_day_max
from calibration import run_calibration, get_city_bias
from redeemer import redeem_all_winners

# -----------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------
# BANKROLL        = get_current_bankroll()   # Total USDC available - update to match your balance
SCAN_INTERVAL       = 30   # Minutes between standard scans
FAST_MONITOR_INTERVAL = 15 # Minutes between fast monitor checks (positions <12h out)
FORECAST_DAYS       = 6    # Max days ahead to fetch forecasts (calibrated sigma/df range)
LOG_LEVEL           = logging.INFO

# Telegram Configuration
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
if not TELEGRAM_TOKEN:
    raise EnvironmentError("TELEGRAM_TOKEN must be set in .env (no default allowed)")

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
        logging.StreamHandler(),
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

# -----------------------------------------------------------------------
# Auth retry configuration
# -----------------------------------------------------------------------
MAX_AUTH_RETRIES      = 3
AUTH_RETRY_DELAY_S    = 5.0
_consecutive_auth_fails = 0
_MAX_CONSECUTIVE_AUTH_FAILS = 10  # Alert via Telegram after this many


def _get_clob_client_safe() -> object:
    """
    Wraps get_clob_client() with retry logic and Telegram error alerting.
    Returns a client on success, raises on total failure.
    """
    global _consecutive_auth_fails

    last_exc = None
    for attempt in range(1, MAX_AUTH_RETRIES + 1):
        try:
            client = get_clob_client()
            _consecutive_auth_fails = 0  # Reset on success
            return client
        except Exception as e:
            last_exc = e
            _consecutive_auth_fails += 1
            logger.warning(
                f"Auth attempt {attempt}/{MAX_AUTH_RETRIES} failed: {e}"
            )
            if attempt < MAX_AUTH_RETRIES:
                time.sleep(AUTH_RETRY_DELAY_S * attempt)

    # All retries exhausted
    if _consecutive_auth_fails >= _MAX_CONSECUTIVE_AUTH_FAILS:
        notifier.notify_error(
            "Authentication",
            f"Failed {_consecutive_auth_fails} consecutive auth attempts. "
            f"Last error: {last_exc}"
        )
    raise last_exc


# -----------------------------------------------------------------------
# Live bankroll helper
# -----------------------------------------------------------------------
def get_current_bankroll() -> float:
    """Get real available USDC. Tries signature_type=1 first (works for your proxy wallet)."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        import os
        import time

        client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.getenv("POLYMARKET_PRIVATE_KEY"),
            chain_id=137,
            signature_type=int(os.getenv("POLYMARKET_SIG_TYPE", "2")),
            funder=os.getenv("POLYMARKET_FUNDER").strip().lower()
        )

        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        for sig_type in [1, 2]:   # 1 works for you, 2 is fallback
            try:
                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=sig_type
                )
                client.update_balance_allowance(params)
                time.sleep(2)

                result = client.get_balance_allowance(params)
                if isinstance(result, dict) and "balance" in result:
                    balance_usdc = int(result["balance"]) / 1_000_000
                    if balance_usdc > 10:
                        print(f"✅ Real bankroll loaded (sig_type={sig_type}): ${balance_usdc:.2f}")
                        return round(balance_usdc, 2)
            except Exception as inner_e:
                print(f"sig_type={sig_type} failed: {inner_e}")
                continue

        fallback = float(os.getenv("FALLBACK_BANKROLL", "200.0"))
        msg = f"All balance attempts failed — using fallback ${fallback:.2f}"
        print(f"⚠️ {msg}")
        logger.warning(msg)
        try:
            notifier.notify_error("Bankroll", msg)
        except Exception:
            pass
        return fallback

    except Exception as e:
        fallback = float(os.getenv("FALLBACK_BANKROLL", "200.0"))
        msg = f"get_current_bankroll error: {e} — using fallback ${fallback:.2f}"
        print(f"❌ {msg}")
        logger.error(msg)
        try:
            notifier.notify_error("Bankroll", msg)
        except Exception:
            pass
        return fallback

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
    max_consecutive_failures = 10

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
            consecutive_failures += 1
            error_str = str(e)
            logger.warning(
                f"Heartbeat failed ({consecutive_failures}x): {error_str}"
            )

            # Try to extract server-provided heartbeat_id from error response.
            # PolyApiException stores the response body in .error_msg (not .error_message).
            try:
                err_body = getattr(e, 'error_msg', None)
                if isinstance(err_body, dict):
                    new_hb_id = err_body.get("heartbeat_id")
                    if new_hb_id:
                        logger.info(f"Heartbeat: extracted ID from error response: {new_hb_id}")
                        hb_id = new_hb_id
            except Exception as parse_err:
                logger.debug(f"Heartbeat: could not parse error response: {parse_err}")

            # Re-auth the client on auth errors (check status_code attr or string repr)
            status_code = getattr(e, 'status_code', None)
            if status_code == 401 or "401" in error_str or "auth" in error_str.lower():
                try:
                    client = _get_clob_client_safe()
                    logger.info("Heartbeat: re-authenticated client")
                except Exception as auth_err:
                    logger.error(f"Heartbeat re-auth failed: {auth_err}")

            # Alert if too many consecutive failures
            if consecutive_failures >= max_consecutive_failures:
                notifier.notify_error(
                    "Heartbeat",
                    f"{consecutive_failures} consecutive heartbeat failures. "
                    f"Last: {error_str}"
                )
                consecutive_failures = 0  # Reset counter after alerting

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
MIN_PRICE_FLOOR = 0.005

_traded_tokens: set = set()
_traded_tokens_date: str = ""

# Daily spend tracking — persists across cycles within a UTC day.
# Prevents the 20% cap from resetting every 30-minute cycle.
_daily_spent: float = 0.0
_daily_spent_date: str = ""


def _get_remaining_daily_budget(cycle_bankroll: float) -> float:
    """Returns remaining USDC budget for today, accounting for all prior cycles."""
    global _daily_spent, _daily_spent_date
    today = datetime.now(timezone.utc).date().isoformat()
    if _daily_spent_date != today:
        _daily_spent = 0.0
        _daily_spent_date = today
    max_daily = cycle_bankroll * 0.2
    return max(0.0, max_daily - _daily_spent)


def _record_daily_spend(amount: float) -> None:
    global _daily_spent
    _daily_spent += amount


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
    ensemble_cache: dict = {}    # {(city, date): spread_c | None}
    observed_max_cache: dict = {}  # {city: dict | None}
    trades_placed = 0
    total_spent = 0.0
    cycle_bankroll = get_current_bankroll()
    remaining_budget = _get_remaining_daily_budget(cycle_bankroll)
    if remaining_budget <= 0:
        logger.info("Daily spend limit already reached, skipping cycle.")
        return

    # Load calibration data once per cycle (fast — reads from in-memory cache)
    try:
        run_calibration()
    except Exception as _e:
        logger.debug(f"Calibration load failed (non-fatal): {_e}")

    try:
        markets = get_weather_markets()
        logger.info(f"Scanning {len(markets)} weather markets")
    except Exception as e:
        logger.error(f"Failed to fetch markets: {e}")
        notifier.notify_error("Market Fetch", str(e))
        return

    markets_scanned = len(markets)

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

        if not yes_token_id:
            logger.debug(f"No YES token ID for {slug}")
            log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP", "no_yes_token_id")
            continue

        # Skip markets too close to resolution (no time to exit a losing trade)
        _city_tz = _CITIES_REF.get(city, {}).get("tz")
        hours_left = _get_hours_to_res(date_str, _city_tz)
        if hours_left < MIN_HOURS_TO_RES:
            logger.info(
                f"SKIP (too close to resolution): {slug} | {hours_left:.1f}h remaining"
            )
            log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP",
                     f"resolves_in_{hours_left:.1f}h_<_{MIN_HOURS_TO_RES}h")
            continue

        # Check both in-memory set (this cycle) and SQLite (persistent)
        if yes_token_id in _traded_tokens or is_token_traded_today(yes_token_id):
            logger.info(f"SKIP (already traded): {slug}")
            log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP", "already_traded")
            notifier.record_trade(entered=False)
            continue

        # Get weather forecast (cached per city per cycle)
        if city not in forecast_cache:
            try:
                forecast_cache[city] = get_forecast(city, days=FORECAST_DAYS)
                logger.debug(f"Forecast cached for {city}")
            except ValueError as e:
                logger.debug(f"No forecast for city {city}: {e}")
                forecast_cache[city] = None
            except Exception as e:
                logger.warning(f"Weather API error for {city}: {e}")
                forecast_cache[city] = None

        forecast_data = forecast_cache[city]
        if forecast_data is None:
            log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP", "no_forecast_available")
            continue

        if date_str not in forecast_data:
            logger.debug(f"Date {date_str} not in forecast window for {city}")
            log_scan(slug, city, date_str, 0.0, 0.0, 0.0, "SKIP", "date_out_of_forecast_window")
            continue

        forecast_celsius = forecast_data[date_str]
        forecast_in_unit = convert_forecast_to_market_unit(forecast_celsius, unit)

        # ---------------------------------------------------------------
        # Ensemble spread: use per-forecast sigma when available.
        # Falls back to the static horizon table if ensemble API fails.
        # Cached per (city, date) to avoid redundant API calls.
        # ---------------------------------------------------------------
        cache_key = (city, date_str)
        if cache_key not in ensemble_cache:
            try:
                ensemble_cache[cache_key] = get_ensemble_spread(city, date_str)
            except Exception:
                ensemble_cache[cache_key] = None
        ensemble_spread_c = ensemble_cache[cache_key]

        sigma_override = None
        if ensemble_spread_c is not None:
            sigma_override = ensemble_spread_c * 1.8 if unit.upper() == "F" else ensemble_spread_c

        # ---------------------------------------------------------------
        # Bayesian intraday update: for same-day markets, the running
        # daily max acts as a hard lower floor on the final daily high.
        # Shift the distribution center up if observed > forecast.
        # Cached per city to avoid duplicate API calls.
        # ---------------------------------------------------------------
        observed_max = None
        city_tz_str = _CITIES_REF.get(city, {}).get("tz")
        if city_tz_str:
            try:
                _tz = pytz.timezone(city_tz_str)
                today_city = datetime.now(_tz).strftime("%Y-%m-%d")
                if date_str == today_city:
                    if city not in observed_max_cache:
                        observed_max_cache[city] = get_current_day_max(city)
                    current = observed_max_cache.get(city)
                    if current:
                        observed_max = (
                            current["temp_f"] if unit.upper() == "F"
                            else current["temp_c"]
                        )
            except Exception:
                pass

        # Per-city calibration bias offset (0.0 until MIN_SAMPLES_FOR_BIAS met)
        city_bias = get_city_bias(city, unit)

        prob = forecast_probability(
            forecast_temp=forecast_in_unit,
            bucket_low=bucket_low,
            bucket_high=bucket_high,
            unit=unit,
            market_date=date_str,
            model_uncertainty_deg=sigma_override,
            city_bias=city_bias,
            observed_max=observed_max,
        )

        # ---------------------------------------------------------------
        # Fetch order book in one call: get bid, ask, mid, and illiquid
        # flag without separate API round-trips.
        # Edge is computed against the ASK (the price we actually pay),
        # not the midpoint.  Midpoint-based edge overstates profitability
        # since we never buy at the midpoint.
        # ---------------------------------------------------------------
        book = get_book_snapshot(yes_token_id, client=clob_client)
        if book["bid"] is None and book["ask"] is None:
            # Total book failure — retry with a fresh client
            try:
                clob_client = _get_clob_client_safe()
                book = get_book_snapshot(yes_token_id, client=clob_client)
            except Exception:
                pass

        # ---------------------------------------------------------------
        # Illiquid path: no liquid ask → route to ladder bidding.
        # Use $0.03 (mid-rung of default ladder) as the assumed price
        # for edge and Kelly sizing.
        # ---------------------------------------------------------------
        if book["illiquid"] or book["ask"] is None:
            LADDER_ASSUMED_PRICE = 0.03
            edge = find_edge(prob, LADDER_ASSUMED_PRICE)
            prob_floor = get_prob_floor(date_str)

            if prob < prob_floor:
                logger.debug(
                    f"Illiquid SKIP (prob floor): {slug} | prob={prob:.1%} < {prob_floor:.0%}"
                )
                log_scan(slug, city, date_str, prob, 0.0, edge, "SKIP",
                         f"illiquid + prob {prob:.1%} < floor {prob_floor:.0%}")
                notifier.record_trade(entered=False)
                continue

            logger.info(
                f"{city} {date_str} | bucket=[{bucket_low},{bucket_high}]{unit} | "
                f"forecast={forecast_in_unit:.1f}{unit} | prob={prob:.1%} | "
                f"mkt=ILLIQUID | edge={edge:+.1%} (vs ${LADDER_ASSUMED_PRICE:.2f})"
            )

            size = kelly_position_size(cycle_bankroll, edge, prob, LADDER_ASSUMED_PRICE)
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
                "market_price": LADDER_ASSUMED_PRICE,
                "edge": edge,
                "size": size,
                "illiquid": True,
            })
            continue

        # Liquid market: use ask price for edge (what we actually pay)
        ask_price = book["ask"]
        if ask_price < MIN_PRICE_FLOOR:
            logger.info(f"SKIP (price floor): {slug} | ask={ask_price:.3f}")
            log_scan(slug, city, date_str, prob, ask_price, 0.0, "SKIP", "price_below_floor")
            notifier.record_trade(entered=False)
            continue

        edge = find_edge(prob, ask_price)

        logger.info(
            f"{city} {date_str} | bucket=[{bucket_low},{bucket_high}]{unit} | "
            f"forecast={forecast_in_unit:.1f}{unit} | prob={prob:.1%} | "
            f"ask={ask_price:.1%} | edge={edge:+.1%}"
            + (f" | σ_ens={ensemble_spread_c:.2f}°C" if ensemble_spread_c else "")
        )

        if not should_trade(edge, forecast_prob=prob, market_date=date_str):
            prob_floor = get_prob_floor(date_str)
            if prob < prob_floor:
                reason = f"prob {prob:.1%} < floor {prob_floor:.0%} for horizon (edge {edge:.1%})"
            else:
                reason = f"edge {edge:.1%} < threshold {ENTRY_THRESHOLD:.1%}"
            log_scan(slug, city, date_str, prob, ask_price, edge, "PASS", reason)
            notifier.record_trade(entered=False)
            continue

        size = kelly_position_size(cycle_bankroll, edge, prob, ask_price)
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
            "market_price": ask_price,  # ask = what we pay for a FOK buy
            "edge": edge,
            "size": size,
            "illiquid": False,
        })

    # Phase 2: All qualifying signals pass through.
    # The probability floor (MIN_FORECAST_PROB) in should_trade() already
    # filters out low-probability buckets. Multiple buckets per city/date
    # are allowed when each independently meets both the edge threshold
    # AND the absolute probability floor. Sort by probability descending
    # so the most likely outcome gets filled first (matters if daily
    # spend limit is reached mid-cycle).
    filtered = sorted(signals, key=lambda s: s["prob"], reverse=True)

    if filtered:
        # Log how many city/date groups have multiple bets
        from collections import Counter
        group_counts = Counter((s["city"], s["date_str"]) for s in filtered)
        multi = sum(1 for c in group_counts.values() if c > 1)
        if multi:
            logger.info(
                f"Multi-bucket: {multi} city/date group(s) have >1 qualifying bet "
                f"({len(filtered)} total signals)"
            )

    # Phase 3: Execute orders
    for sig in filtered:
        # Use remaining_budget (persists across cycles) not a per-cycle counter
        remaining_budget = _get_remaining_daily_budget(cycle_bankroll)
        if sig["size"] > remaining_budget:
            logger.warning(
                f"Daily spend limit reached (remaining ${remaining_budget:.2f} < "
                f"${sig['size']:.2f}). Stopping."
            )
            log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                     sig["market_price"], sig["edge"], "SKIP", "daily_spend_limit")
            break

        # Route illiquid markets to ladder bidding, liquid to FOK
        if sig.get("illiquid"):
            logger.info(
                f"LADDER BID: {sig['slug']} | prob={sig['prob']:.1%} | "
                f"size=${sig['size']:.2f} | rungs=$0.01-$0.05"
            )

            ladder_resp = place_ladder_bids(
                token_id=sig["yes_token_id"],
                size_usdc=sig["size"],
            )

            ladder_status = ladder_resp.get("status", "FAILED")

            if ladder_status == "NONE":
                # No fills at all. Skip this market per user spec.
                logger.info(f"LADDER SKIP (no fills): {sig['slug']}")
                log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                         sig["market_price"], sig["edge"], "SKIP", "ladder_no_fills")
                notifier.record_trade(entered=False)
                continue

            if ladder_status == "FAILED":
                logger.error(f"LADDER FAILED: {sig['slug']} | {ladder_resp.get('reason', 'unknown')}")
                log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                         sig["market_price"], sig["edge"], "SKIP", "ladder_failed")
                notifier.record_trade(entered=False)
                continue

            # We got fills. Build a synthetic order_response for downstream logging.
            actual_spent = ladder_resp["total_spent"]
            actual_shares = ladder_resp["total_shares"]
            actual_price = ladder_resp["avg_price"]

            order_response = {
                "orderID": "LADDER_" + (ladder_resp["fills"][0]["orderID"] if ladder_resp["fills"] else ""),
                "status": "MATCHED" if ladder_status == "FILLED" else "PARTIAL",
                "price": actual_price,
                "size": actual_shares,
                "size_usdc": actual_spent,
                "ladder_fills": len(ladder_resp["fills"]),
                "ladder_cancelled": ladder_resp["cancelled"],
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
                     "TRADE", f"ladder ${actual_spent:.2f} @ avg ${actual_price:.4f}")

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
                    forecast_temp_c=sig["forecast_celsius"],
                )
            except Exception as e:
                logger.error(f"Failed to record ladder position in DB: {e}")

            notifier.record_trade(entered=True, size_usdc=actual_spent)
            notifier.notify_trade(
                slug=sig["slug"],
                city=sig["city"],
                date_str=sig["date_str"],
                edge=sig["edge"],
                size_usdc=actual_spent,
                price=actual_price,
                prob=sig["prob"],
                order_status=f"LADDER_{ladder_status}",
            )

            trades_placed += 1
            total_spent += actual_spent
            _record_daily_spend(actual_spent)
            continue

        # ---- Normal (liquid) market path ----
        # Refresh the ask price immediately before placing the order.
        # Signal prices were fetched during scanning (possibly minutes ago).
        # If the ask has moved enough to erase the edge, skip.
        fresh_book = get_book_snapshot(sig["yes_token_id"], client=clob_client)
        fresh_ask = fresh_book.get("ask")
        if fresh_ask is None:
            fresh_ask = get_market_price(sig["yes_token_id"], client=clob_client)
        if fresh_ask is not None and abs(fresh_ask - sig["market_price"]) > 0.03:
            fresh_edge = find_edge(sig["prob"], fresh_ask)
            if not should_trade(fresh_edge, forecast_prob=sig["prob"], market_date=sig["date_str"]):
                logger.info(
                    f"SKIP (price moved): {sig['slug']} | "
                    f"signal_ask={sig['market_price']:.3f} -> now={fresh_ask:.3f} | "
                    f"fresh_edge={fresh_edge:+.1%} below threshold"
                )
                log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                         fresh_ask, fresh_edge, "SKIP", "price_moved_pre_order")
                continue
            sig = dict(sig)
            sig["market_price"] = fresh_ask
            sig["edge"] = fresh_edge

        logger.info(
            f"EDGE FOUND: {sig['slug']} | edge={sig['edge']:.1%} | "
            f"size=${sig['size']:.2f} | price={sig['market_price']:.3f}"
        )

        order_response = place_buy_order(
            token_id=sig["yes_token_id"],
            price=sig["market_price"],
            size_usdc=sig["size"],
        )

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
            market_price=sig["market_price"],
            edge=sig["edge"],
            size_usdc=sig["size"],
            dry_run=DRY_RUN,
            order_response=order_response,
            question=sig["question"],
        )

        log_scan(sig["slug"], sig["city"], sig["date_str"], sig["prob"],
                 sig["market_price"], sig["edge"],
                 "TRADE", f"size=${sig['size']:.2f}")

        _traded_tokens.add(sig["yes_token_id"])

        # Record position in SQLite for monitoring/resolution/redemption.
        # Only "MATCHED" (filled FOK) or "simulated" (dry-run) are accepted.
        # "LIVE" means the order is resting unfilled — a true FOK cannot be LIVE.
        order_status = order_response.get("status", "UNKNOWN") if order_response else "FAILED"
        order_filled = order_status in ("simulated", "MATCHED")
        if order_status == "LIVE":
            logger.warning(
                f"FOK order returned LIVE (resting, not filled) for {sig['slug']} — "
                f"cancelling and skipping position record. id={order_response.get('orderID')}"
            )
            from executor import cancel_order
            cancel_order(order_response.get("orderID", ""))
        elif order_filled:
            try:
                num_shares = sig["size"] / sig["market_price"] if sig["market_price"] > 0 else 0
                record_entry(
                    token_id=sig["yes_token_id"],
                    condition_id=sig["condition_id"],
                    slug=sig["slug"],
                    city=sig["city"],
                    market_date=sig["date_str"],
                    bucket_low=sig["bucket_low"],
                    bucket_high=sig["bucket_high"],
                    unit=sig["unit"],
                    entry_price=sig["market_price"],
                    shares=num_shares,
                    size_usdc=sig["size"],
                    order_id=order_response.get("orderID", ""),
                    neg_risk=False,
                    question=sig["question"],
                    forecast_prob=sig["prob"],
                    market_prob=sig["market_price"],
                    edge=sig["edge"],
                    forecast_temp_c=sig["forecast_celsius"],
                )
            except Exception as e:
                logger.error(f"Failed to record position in DB: {e}")

        # Record trade in notifier and send alert
        notifier.record_trade(entered=order_filled, size_usdc=sig["size"] if order_filled else 0.0)
        notifier.notify_trade(
            slug=sig["slug"],
            city=sig["city"],
            date_str=sig["date_str"],
            edge=sig["edge"],
            size_usdc=sig["size"],
            price=sig["market_price"],
            prob=sig["prob"],
            order_status=order_status,
        )

        if order_filled:
            trades_placed += 1
            total_spent += sig["size"]
            _record_daily_spend(sig["size"])

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

    # Initialize daily stats
    notifier.reset_daily(BANKROLL)

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
