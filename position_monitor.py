"""
position_monitor.py - Monitors open positions for exit signals.

Exit strategies (in priority order):
0. Market closed/resolved: If Gamma API reports closed=True or CLOB last-trade-price
   is 0.0/1.0, the market is done. Record resolution immediately — orders are void.
1. Same-day exit: If the observed daily max temperature has moved 3+ degrees
   beyond our bucket boundary, sell to cut losses. The market hasn't resolved
   yet, but the position is very likely to lose.
2. Profit-taking: If the current market price of our YES shares gives 500%+
   profit over entry price AND our updated forecast probability has dropped
   below 49%, sell and lock in profits rather than risk resolution.

Monitoring frequency:
  - 30 min for positions 12-24 hours from resolution
  - 15 min for positions within 12 hours of resolution

Called from bot.py's main loop.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import requests as _requests

from positions import (
    get_open_positions,
    record_exit,
    get_position_by_id,
    record_resolution,
)
from observed_temps import get_current_day_max
from executor import place_sell_order, DRY_RUN, get_client as get_clob_client
from settlement_verifier import verify_delayed_settlements
import os

# Aviation weather toggle (default: enabled)
USE_AVIATION_WEATHER = os.getenv("USE_AVIATION_WEATHER", "true").lower() in ("true", "1", "yes")
from markets import get_midpoint_price, get_market_price
from strategy import forecast_probability, bayesian_metar_probability, find_edge, convert_forecast_to_market_unit
from weather_v2 import get_forecast, get_forecast_low

logger = logging.getLogger(__name__)


def _get_position_forecast(pos: dict, days: int = 6) -> dict:
    """Return forecast data for a position, routing to the correct endpoint
    based on market_type stored at entry time.

    get_forecast()       → daily MAX  (highest-temperature markets)
    get_forecast_low()   → daily MIN  (lowest-temperature markets)
    """
    city = pos["city"]
    market_type = pos.get("market_type", "highest")
    if market_type == "lowest":
        return get_forecast_low(city, days=days)
    return get_forecast(city, days=days)

# -----------------------------------------------------------------------
# Exit retry tracking (in-memory, resets on restart)
# Prevents the same failing position from spamming error logs every cycle.
# -----------------------------------------------------------------------
MAX_EXIT_RETRIES = 5
_exit_retry_counts: dict[int, int] = {}


def _record_exit_failure(position_id: int) -> None:
    """Increments the failure counter for a position exit attempt."""
    _exit_retry_counts[position_id] = _exit_retry_counts.get(position_id, 0) + 1


def _should_skip_exit(position_id: int) -> bool:
    """Returns True if this position has exceeded MAX_EXIT_RETRIES."""
    return _exit_retry_counts.get(position_id, 0) >= MAX_EXIT_RETRIES


def _clear_exit_retries(position_id: int) -> None:
    """Clears retry counter after a successful exit."""
    _exit_retry_counts.pop(position_id, None)


# -----------------------------------------------------------------------
# Exit thresholds
# -----------------------------------------------------------------------
# Same-day exit: sell if observed max is this many degrees outside bucket
TEMP_EXIT_MARGIN_DEG = 3.0

# Profit-taking: sell if unrealized profit >= this fraction
PROFIT_TAKE_THRESHOLD = 5.00  # 500% profit

# Profit-taking: only sell if updated forecast prob dropped below this
PROFIT_TAKE_PROB_CEILING = 0.49  # 49%

# Edge-collapse exit: sell when our edge over market price drops below this
# AND position is in profit. Recycles capital into higher-edge opportunities.
EDGE_COLLAPSE_THRESHOLD = 0.05  # 5% edge remaining
# Minimum profit to allow edge-collapse exit (don't sell at break-even/loss)
MIN_PROFIT_FOR_EDGE_EXIT = 0.10  # 10% profit minimum

# Stop-loss: exit when position is down this fraction from entry
STOP_LOSS_THRESHOLD = 0.25   # 25% loss triggers exit

# Stop-loss: also exit when updated model probability falls to this fraction
# of the original forecast_prob stored at entry time
PROB_DROP_THRESHOLD = 0.50   # model is only 50% as confident as at entry

# How close to resolution before we use the fast (15-min) check
FAST_MONITOR_HOURS = 12.0


def get_hours_to_resolution(market_date: str, city_tz: str = None) -> float:
    """
    Estimates hours remaining until a market resolves.
    Polymarket weather markets typically resolve at end of day (local time).
    Returns negative if already past resolution.
    """
    try:
        from weather_v2 import CITIES
        import pytz

        mdate = datetime.strptime(market_date, "%Y-%m-%d")

        # Estimate resolution as 23:59 local time on market date
        if city_tz:
            tz = pytz.timezone(city_tz)
            local_end = tz.localize(mdate.replace(hour=23, minute=59))
            utc_end = local_end.astimezone(pytz.UTC)
        else:
            # Fallback: assume resolution at 23:59 UTC on market date
            utc_end = mdate.replace(hour=23, minute=59, tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        delta = utc_end - now
        return delta.total_seconds() / 3600.0

    except Exception as e:
        logger.debug(f"Could not compute hours to resolution: {e}")
        return 24.0  # Safe default


def needs_fast_monitoring() -> bool:
    """
    Returns True if any open position is within FAST_MONITOR_HOURS of resolution.
    Used by bot.py to decide whether to run the 15-minute fast loop.
    """
    from weather_v2 import CITIES

    positions = get_open_positions()
    for pos in positions:
        city_info = CITIES.get(pos["city"])
        tz = city_info["tz"] if city_info else None
        hours = get_hours_to_resolution(pos["market_date"], tz)
        if 0 < hours <= FAST_MONITOR_HOURS:
            return True
    return False


def _check_market_closed(pos: dict, clob_client=None) -> Optional[dict]:
    """
    Check if a market is closed/resolved via Gamma API and CLOB.

    This is the HIGHEST PRIORITY check — if the market is closed, all other
    exit checks are meaningless because orders are null and void.

    Detection methods (in order of reliability):
      1. Gamma API: closed=True + outcomePrices=["1","0"] or ["0","1"] → resolved
      2. CLOB last-trade-price: 0.0 or 1.0 → resolved
      3. Gamma API: closed=True (even without outcomePrices) → closed, likely resolved

    Returns:
        dict with resolution info if market is closed/resolved, None otherwise.
        The dict includes: position_id, won (bool), source, outcome_prices
    """
    token_id = pos.get("token_id")
    condition_id = pos.get("condition_id")
    city = pos.get("city", "?")
    market_date = pos.get("market_date", "?")

    # ── Method 1: Gamma API ──────────────────────────────────────────
    # Search by series_id (most reliable for weather markets)
    # Weather markets are in series like "nyc-daily-weather" (id=10005)
    # We try multiple approaches to find the market in Gamma

    gamma_resolved = None  # None=unknown, True=won, False=lost

    # Approach A: Try Gamma /events with the condition_id
    # Note: Gamma's condition_id param is unreliable (returns unrelated markets),
    # but we try it anyway as a fallback
    if condition_id:
        try:
            r = _requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"condition_id": condition_id, "closed": "true", "limit": 5},
                timeout=10,
            )
            if r.status_code == 200:
                markets = r.json()
                if isinstance(markets, list):
                    for m in markets:
                        m_cond = m.get("conditionId", "")
                        # Only trust if condition_id actually matches
                        if m_cond.lower() == condition_id.lower():
                            outcome_prices = m.get("outcomePrices", [])
                            if outcome_prices:
                                yes_price = float(outcome_prices[0]) if len(outcome_prices) > 0 else 0
                                if yes_price >= 0.99:
                                    gamma_resolved = True
                                elif yes_price <= 0.01:
                                    gamma_resolved = False
                            elif m.get("closed"):
                                # Closed but no outcomePrices yet — still closed
                                gamma_resolved = None  # Will fall through to CLOB check
                            break
        except Exception as e:
            logger.debug(f"Gamma API check failed for {city} {market_date}: {e}")

    # Approach B: Try CLOB last-trade-price (works for ALL markets including CLOB-only)
    if gamma_resolved is None and token_id:
        try:
            r = _requests.get(
                f"https://clob.polymarket.com/last-trade-price?token_id={token_id}",
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                price = float(data.get("price", 0.5))
                if price >= 0.99:
                    gamma_resolved = True
                elif price <= 0.01:
                    gamma_resolved = False
        except Exception as e:
            logger.debug(f"CLOB price check failed for {city} {market_date}: {e}")

    # ── If resolved, return the result ───────────────────────────────
    if gamma_resolved is not None:
        won = gamma_resolved
        logger.info(
            f"MARKET CLOSED/RESOLVED: {city} {market_date} → "
            f"{'WON' if won else 'LOST'} (source=gamma+clob)"
        )
        return {
            "position_id": pos["id"],
            "token_id": token_id,
            "shares": pos["shares"],
            "reason": f"market_resolved: {'WON' if won else 'LOST'} via API",
            "won": won,
            "source": "gamma_clob",
            "action": "record_resolution",
        }

    return None


def _check_same_day_exit(pos: dict, clob_client=None) -> Optional[dict]:
    """
    Checks if a same-day position should be exited based on observed temps.

    Logic: If the current observed daily max is already 2+ degrees outside
    our bucket boundary, the position is very likely to lose. Sell now
    rather than hold a near-certain loser.

    Returns exit action dict or None if no exit needed.
    """
    city = pos["city"]
    market_date = pos["market_date"]
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Only applies to same-day positions
    if market_date != today_str:
        return None

    current = get_current_day_max(city, prefer_aviation=USE_AVIATION_WEATHER)
    if current is None:
        logger.debug(f"No current temp data for {city}, skipping exit check")
        return None

    # Convert to market unit
    market_type = pos.get("market_type", "highest")
    unit = pos.get("unit", "F")
    if unit.upper() == "F":
        current_max = current["temp_f"]
    else:
        current_max = current["temp_c"]

    bucket_low = pos["bucket_low"]
    bucket_high = pos["bucket_high"]

    # Check if current max has blown past our bucket
    exit_reason = None
    temp_source = current.get("source", "unknown")

    if bucket_high is not None and current_max >= bucket_high + TEMP_EXIT_MARGIN_DEG:
        exit_reason = (
            f"same_day_exit: observed max {current_max:.1f}{unit} >= "
            f"bucket_high {bucket_high}{unit} + {TEMP_EXIT_MARGIN_DEG} margin "
            f"(source={temp_source})"
        )

    if bucket_low is not None and bucket_high is not None:
        # For range buckets: if current max is already well above the range
        # high, the final max will almost certainly be above our bucket
        if current_max >= bucket_high + TEMP_EXIT_MARGIN_DEG:
            exit_reason = (
                f"same_day_exit: observed max {current_max:.1f}{unit} already "
                f"{current_max - bucket_high:.1f} above bucket ceiling {bucket_high}{unit} "
                f"(source={temp_source})"
            )

    # Note: we don't exit on "current max below bucket_low" because the
    # temp could still rise later in the day. Only exit when temp has
    # exceeded our bucket's HIGH side (can't go back down).

    if exit_reason is None:
        return None

    logger.info(f"SAME-DAY EXIT SIGNAL: {city} {market_date} | {exit_reason}")

    return {
        "position_id": pos["id"],
        "token_id": pos["token_id"],
        "shares": pos["shares"],
        "reason": exit_reason,
        "current_temp": current_max,
    }


def _check_stop_loss(pos: dict, clob_client=None) -> Optional[dict]:
    """
    Checks if a position should be exited via stop-loss.

    Two triggers (either one fires):
      1. Price stop-loss: current market price <= entry_price * (1 - STOP_LOSS_THRESHOLD)
         i.e. position is down 25% or more.
      2. Probability collapse: updated model prob < original forecast_prob * PROB_DROP_THRESHOLD
         i.e. our model now thinks this bucket is at most 50% as likely as when we entered.

    Returns exit action dict or None if no exit needed.
    """
    token_id    = pos["token_id"]
    entry_price = pos["entry_price"]
    orig_prob   = pos.get("forecast_prob")   # stored at entry time (may be None on old rows)

    if entry_price <= 0:
        return None

    # Get current market price
    current_price = get_midpoint_price(token_id, client=clob_client)
    if current_price is None:
        current_price = get_market_price(token_id, client=clob_client)
    if current_price is None:
        return None

    loss_pct = (entry_price - current_price) / entry_price  # positive = loss

    # --- Trigger 1: price down 25%+ ---
    if loss_pct >= STOP_LOSS_THRESHOLD:
        exit_reason = (
            f"stop_loss: price down {loss_pct:.0%} "
            f"(entry={entry_price:.3f}, current={current_price:.3f})"
        )
        logger.info(f"STOP-LOSS SIGNAL: {pos['city']} {pos['market_date']} | {exit_reason}")
        return {
            "position_id":   pos["id"],
            "token_id":      token_id,
            "shares":        pos["shares"],
            "reason":        exit_reason,
            "current_price": current_price,
            "profit_pct":    -loss_pct,
        }

    # --- Trigger 2: model probability collapsed ---
    if orig_prob and orig_prob > 0:
        city        = pos["city"]
        market_date = pos["market_date"]
        unit        = pos.get("unit", "F")
        try:
            forecast_data = _get_position_forecast(pos, days=6)
            if market_date in forecast_data:
                forecast_celsius  = forecast_data[market_date]
                forecast_in_unit  = convert_forecast_to_market_unit(forecast_celsius, unit)
                today_str_check   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if market_date == today_str_check:
                    current_obs = get_current_day_max(city, prefer_aviation=USE_AVIATION_WEATHER)
                    if current_obs is not None:
                        observed_in_unit = current_obs["temp_f"] if unit.upper() == "F" else current_obs["temp_c"]
                        try:
                            from zoneinfo import ZoneInfo
                            from weather_v2 import CITIES as _CITIES
                            city_tz   = ZoneInfo(_CITIES[city]["tz"])
                            local_now = datetime.now(city_tz)
                            local_hour = local_now.hour + local_now.minute / 60.0
                            updated_prob = bayesian_metar_probability(
                                forecast_temp=forecast_in_unit,
                                observed_temp=observed_in_unit,
                                local_hour=local_hour,
                                bucket_low=pos["bucket_low"],
                                bucket_high=pos["bucket_high"],
                                unit=unit,
                                market_date=market_date,
                            )
                        except Exception:
                            updated_prob = forecast_probability(
                                forecast_temp=forecast_in_unit,
                                bucket_low=pos["bucket_low"],
                                bucket_high=pos["bucket_high"],
                                unit=unit,
                                market_date=market_date,
                                city=city,
                            market_type=market_type,
                            )
                    else:
                        updated_prob = forecast_probability(
                            forecast_temp=forecast_in_unit,
                            bucket_low=pos["bucket_low"],
                            bucket_high=pos["bucket_high"],
                            unit=unit,
                            market_date=market_date,
                            city=city,
                        market_type=market_type,
                        )
                else:
                    updated_prob = forecast_probability(
                        forecast_temp=forecast_in_unit,
                        bucket_low=pos["bucket_low"],
                        bucket_high=pos["bucket_high"],
                        unit=unit,
                        market_date=market_date,
                        city=city,
                    market_type=market_type,
                    )

                prob_ratio = updated_prob / orig_prob if orig_prob > 0 else 1.0
                if prob_ratio < PROB_DROP_THRESHOLD:
                    exit_reason = (
                        f"prob_collapse: model prob dropped from {orig_prob:.1%} → {updated_prob:.1%} "
                        f"({prob_ratio:.0%} of original, threshold={PROB_DROP_THRESHOLD:.0%}) | "
                        f"price={current_price:.3f}"
                    )
                    logger.info(f"PROB COLLAPSE EXIT: {pos['city']} {pos['market_date']} | {exit_reason}")
                    return {
                        "position_id":   pos["id"],
                        "token_id":      token_id,
                        "shares":        pos["shares"],
                        "reason":        exit_reason,
                        "current_price": current_price,
                        "profit_pct":    -loss_pct,
                        "updated_prob":  updated_prob,
                    }
        except Exception as e:
            logger.debug(f"Could not compute prob collapse check: {e}")

    return None


def _check_profit_take(pos: dict, clob_client=None) -> Optional[dict]:
    """
    Checks if a position should be sold for profit-taking.

    Logic: If current market price gives 50%+ profit over entry AND
    the updated forecast probability has dropped below 60%, sell.
    This locks in gains when the model is no longer confident.

    Returns exit action dict or None if no exit needed.
    """
    token_id = pos["token_id"]
    entry_price = pos["entry_price"]

    # Get current market price
    current_price = get_midpoint_price(token_id, client=clob_client)
    if current_price is None:
        current_price = get_market_price(token_id, client=clob_client)
    if current_price is None:
        return None

    # Check profit threshold
    if entry_price <= 0:
        return None

    profit_pct = (current_price - entry_price) / entry_price
    if profit_pct < PROFIT_TAKE_THRESHOLD:
        return None

    # Profit threshold met. Now check if there's risk (prob < 60%).
    # Get updated forecast to recalculate probability.
    city = pos["city"]
    market_date = pos["market_date"]
    market_type = pos.get("market_type", "highest")
    unit = pos.get("unit", "F")

    try:
        forecast_data = _get_position_forecast(pos, days=6)
        if market_date not in forecast_data:
            updated_prob = 0.50
        else:
            forecast_celsius = forecast_data[market_date]
            forecast_in_unit = convert_forecast_to_market_unit(forecast_celsius, unit)

            # Use Bayesian METAR update for same-day positions
            today_str_check = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if market_date == today_str_check:
                current = get_current_day_max(city, prefer_aviation=USE_AVIATION_WEATHER)
                if current is not None:
                    observed_in_unit = current["temp_f"] if unit.upper() == "F" else current["temp_c"]
                    try:
                        from zoneinfo import ZoneInfo
                        from weather_v2 import CITIES as _CITIES
                        city_tz = ZoneInfo(_CITIES[city]["tz"])
                        local_now = datetime.now(city_tz)
                        local_hour = local_now.hour + local_now.minute / 60.0
                        updated_prob = bayesian_metar_probability(
                            forecast_temp=forecast_in_unit,
                            observed_temp=observed_in_unit,
                            local_hour=local_hour,
                            bucket_low=pos["bucket_low"],
                            bucket_high=pos["bucket_high"],
                            unit=unit,
                            market_date=market_date,
                        )
                        logger.debug(
                            f"Bayesian prob update for {city}: {updated_prob:.1%} "
                            f"(observed={observed_in_unit:.1f}{unit} @ {local_hour:.1f}h)"
                        )
                    except Exception as e:
                        logger.debug(f"Bayesian update failed, using naive: {e}")
                        updated_prob = forecast_probability(
                            forecast_temp=forecast_in_unit,
                            bucket_low=pos["bucket_low"],
                            bucket_high=pos["bucket_high"],
                            unit=unit,
                            market_date=market_date,
                                    city=city,
                        market_type=market_type,
                        )
                else:
                    updated_prob = forecast_probability(
                        forecast_temp=forecast_in_unit,
                        bucket_low=pos["bucket_low"],
                        bucket_high=pos["bucket_high"],
                        unit=unit,
                        market_date=market_date,
                                city=city,
                    market_type=market_type,
                    )
            else:
                updated_prob = forecast_probability(
                    forecast_temp=forecast_in_unit,
                    bucket_low=pos["bucket_low"],
                    bucket_high=pos["bucket_high"],
                    unit=unit,
                    market_date=market_date,
                            city=city,
                market_type=market_type,
                )
    except Exception as e:
        logger.debug(f"Could not update forecast for profit check: {e}")
        updated_prob = 0.50

    if updated_prob >= PROFIT_TAKE_PROB_CEILING:
        # Model is still very confident. Hold through to resolution.
        logger.debug(
            f"Profit {profit_pct:.0%} on {pos['slug'][:40]} but prob={updated_prob:.1%} "
            f">= {PROFIT_TAKE_PROB_CEILING:.0%}, holding."
        )
        return None

    exit_reason = (
        f"profit_take: {profit_pct:.0%} profit (entry={entry_price:.3f}, "
        f"current={current_price:.3f}) | updated_prob={updated_prob:.1%} "
        f"< {PROFIT_TAKE_PROB_CEILING:.0%} ceiling"
    )

    logger.info(f"PROFIT TAKE SIGNAL: {pos['city']} {pos['market_date']} | {exit_reason}")

    return {
        "position_id": pos["id"],
        "token_id": pos["token_id"],
        "shares": pos["shares"],
        "reason": exit_reason,
        "current_price": current_price,
        "profit_pct": profit_pct,
        "updated_prob": updated_prob,
    }



def _check_edge_collapse(pos: dict, clob_client=None) -> Optional[dict]:
    """
    Checks if a position's edge has collapsed, making it worth selling
    to recycle capital into higher-edge opportunities.

    Logic: If the market price has risen to near our model probability,
    edge < 5%, and we're in profit (>10%), sell. The position is no longer
    a high-edge bet — the market has caught up to our model.

    This is distinct from profit-taking (which requires 500% gain).
    Edge-collapse triggers much earlier, enabling capital recycling.

    Example: Bought at $0.05 with model prob 40%. Market now $0.36.
    Edge = 40% - 36% = 4% < 5%. Position is up 620%. SELL to redeploy.

    Returns exit action dict or None if no exit needed.
    """
    token_id = pos["token_id"]
    entry_price = pos["entry_price"]

    # Get current market price
    current_price = get_midpoint_price(token_id, client=clob_client)
    if current_price is None:
        current_price = get_market_price(token_id, client=clob_client)
    if current_price is None:
        return None

    # Must be in profit to trigger edge-collapse exit
    if entry_price <= 0:
        return None
    profit_pct = (current_price - entry_price) / entry_price
    if profit_pct < MIN_PROFIT_FOR_EDGE_EXIT:
        return None

    # Get updated model probability
    city = pos["city"]
    market_date = pos["market_date"]
    market_type = pos.get("market_type", "highest")
    unit = pos.get("unit", "F")

    try:
        forecast_data = _get_position_forecast(pos, days=6)
        if market_date not in forecast_data:
            return None

        forecast_celsius = forecast_data[market_date]
        forecast_in_unit = convert_forecast_to_market_unit(forecast_celsius, unit)

        # Use Bayesian update for same-day positions
        today_str_check = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if market_date == today_str_check:
            current_obs = get_current_day_max(city, prefer_aviation=USE_AVIATION_WEATHER)
            if current_obs is not None:
                observed_in_unit = current_obs["temp_f"] if unit.upper() == "F" else current_obs["temp_c"]
                try:
                    from zoneinfo import ZoneInfo
                    from weather_v2 import CITIES as _CITIES
                    city_tz = ZoneInfo(_CITIES[city]["tz"])
                    local_now = datetime.now(city_tz)
                    local_hour = local_now.hour + local_now.minute / 60.0
                    updated_prob = bayesian_metar_probability(
                        forecast_temp=forecast_in_unit,
                        observed_temp=observed_in_unit,
                        local_hour=local_hour,
                        bucket_low=pos["bucket_low"],
                        bucket_high=pos["bucket_high"],
                        unit=unit,
                        market_date=market_date,
                    )
                except Exception:
                    updated_prob = forecast_probability(
                        forecast_temp=forecast_in_unit,
                        bucket_low=pos["bucket_low"],
                        bucket_high=pos["bucket_high"],
                        unit=unit,
                        market_date=market_date,
                                city=city,
                    market_type=market_type,
                    )
            else:
                updated_prob = forecast_probability(
                    forecast_temp=forecast_in_unit,
                    bucket_low=pos["bucket_low"],
                    bucket_high=pos["bucket_high"],
                    unit=unit,
                    market_date=market_date,
                            city=city,
                market_type=market_type,
                )
        else:
            updated_prob = forecast_probability(
                forecast_temp=forecast_in_unit,
                bucket_low=pos["bucket_low"],
                bucket_high=pos["bucket_high"],
                unit=unit,
                market_date=market_date,
                        city=city,
            market_type=market_type,
            )
    except Exception as e:
        logger.debug(f"Could not update forecast for edge-collapse check: {e}")
        return None

    # Check if edge has collapsed
    edge = find_edge(updated_prob, current_price)
    if edge >= EDGE_COLLAPSE_THRESHOLD:
        logger.debug(
            f"Edge still healthy for {pos['slug'][:40]}: edge={edge:+.1%} "
            f"(prob={updated_prob:.1%} vs market={current_price:.1%})"
        )
        return None

    exit_reason = (
        f"edge_collapse: edge={edge:+.1%} < {EDGE_COLLAPSE_THRESHOLD:.0%} | "
        f"model_prob={updated_prob:.1%} vs market={current_price:.3f} | "
        f"profit={profit_pct:.0%} (entry={entry_price:.3f})"
    )

    logger.info(f"EDGE COLLAPSE EXIT: {pos['city']} {pos['market_date']} | {exit_reason}")

    return {
        "position_id": pos["id"],
        "token_id": pos["token_id"],
        "shares": pos["shares"],
        "reason": exit_reason,
        "current_price": current_price,
        "profit_pct": profit_pct,
        "updated_prob": updated_prob,
        "edge": edge,
    }


# ---------------------------------------------------------------------------
# Dynamic Exit: Probability Shift Detection
# ---------------------------------------------------------------------------
# Recomputes probability for held bucket using latest forecast + METAR data.
# If the model now says we're in the wrong bucket, cut the position loose
# rather than riding a loser to resolution.
# ---------------------------------------------------------------------------

# ── Wunderground forecast-revision auto-exit (Item 2 fix) ──────────────────
# When WU revises its forecast away from the purchased bucket:
# 1. Market-sell the old position (FOK, accept any price)
# 2. Find the new correct bucket from revised WU forecast
# 3. Re-enter new bucket if exposure cap allows
# 4. For same-day markets: keep alert-only (METAR still shifting actual temp)
#
# Cooldown prevents flip-flopping when WU revises repeatedly.
WU_MISMATCH_COOLDOWN_HOURS = 2  # minimum hours between repeat actions per position
WU_REVISION_REENTRY_DELAY_MIN = 30  # minutes after exit before re-entry

_wu_alert_last: dict[int, float] = {}  # position_id -> last alert timestamp
_wu_exit_last: dict[int, float] = {}  # position_id -> last exit+reentry timestamp


# Trigger exit when an alternative bucket's probability beats the held bucket
# by at least this margin (absolute percentage points). Applied after scanning
# all adjacent buckets — the held bucket doesn't need to be "collapsed," it
# just needs to no longer be the distribution mode.
SHIFT_ALT_LEAD_MARGIN = 0.10  # alternative must beat held bucket by 10+ pp

# Fallback absolute floor: exit even with no clear alternative if the held
# bucket's probability is this low (model has abandoned it entirely).
SHIFT_EXIT_PROB_FLOOR = 0.15

# Number of adjacent buckets to scan when finding the new max-prob alternative
SHIFT_SCAN_RANGE = 5  # ±5 steps (1°C each for Celsius, 2°F each for F)


def _check_probability_shift(pos: dict, clob_client=None) -> Optional[dict]:
    """
    Recomputes probability for the held bucket. If the distribution has
    shifted significantly (held bucket no longer mode, probability collapsed),
    exit the position and recommend the new max-prob bucket.

    Only applies to same-day markets where METAR data provides real-time
    information about whether the forecast is on track.

    Returns:
        dict with exit action if position should be cut, or None
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from weather_v2 import CITIES, celsius_to_fahrenheit, fahrenheit_to_celsius
    from aviation_weather import get_current_metar_temps, AVIATION_ICAO

    city = pos.get("city", "")
    market_date = pos.get("market_date", "")
    unit = pos.get("unit", "F")
    market_type = pos.get("market_type", "highest")
    entry_price = pos.get("entry_price", 0.5)
    shares = pos.get("shares", 0)
    entry_prob = pos.get("forecast_prob") or pos.get("market_prob") or 0.5

    # Only reassess same-day markets
    tz_str = CITIES.get(city, {}).get("tz", "UTC")
    try:
        tz = ZoneInfo(tz_str)
        today_local = datetime.now(tz).strftime("%Y-%m-%d")
    except Exception:
        today_local = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")

    if market_date != today_local:
        return None

    # Get current forecast (respects market_type — lowest vs highest)
    try:
        forecast_data = _get_position_forecast(pos, days=6)
        if market_date not in forecast_data:
            return None
        forecast_c = forecast_data[market_date]
        forecast_in_unit = (
            celsius_to_fahrenheit(forecast_c) if unit.upper() == "F"
            else forecast_c
        )
    except Exception as e:
        logger.debug(f"Probability shift check: no forecast for {city}: {e}")
        return None

    # Try Bayesian METAR update for same-day precision
    use_bayesian = False
    observed_temp_c = None
    local_hour = 0.0
    observed_in_unit = None

    icao = AVIATION_ICAO.get(city)
    if icao:
        try:
            metar_temps = get_current_metar_temps([icao])
            if metar_temps and icao in metar_temps:
                observed_temp_c = metar_temps[icao]
                if observed_temp_c is not None:
                    observed_in_unit = (
                        celsius_to_fahrenheit(observed_temp_c) if unit.upper() == "F"
                        else observed_temp_c
                    )
                    local_now = datetime.now(tz)
                    local_hour = local_now.hour + local_now.minute / 60.0
                    use_bayesian = True
        except Exception:
            pass

    # Compute current probability for held bucket
    bucket_low = pos.get("bucket_low")
    bucket_high = pos.get("bucket_high")

    try:
        if use_bayesian and observed_in_unit is not None:
            current_prob = bayesian_metar_probability(
                forecast_temp=forecast_in_unit,
                observed_temp=observed_in_unit,
                local_hour=local_hour,
                bucket_low=bucket_low,
                bucket_high=bucket_high,
                unit=unit,
                market_date=market_date,
            )
        else:
            current_prob = forecast_probability(
                forecast_temp=forecast_in_unit,
                bucket_low=bucket_low,
                bucket_high=bucket_high,
                unit=unit,
                market_date=market_date,
                city=city,
            market_type=market_type,
            )
    except Exception as e:
        logger.debug(f"Probability shift: computation failed for {city}: {e}")
        return None

    # Always scan adjacent buckets — we don't gate on the held bucket looking
    # "bad enough." If a different bucket is now the clear probability leader,
    # that's grounds to exit, even if the held bucket still has moderate prob.
    prob_drop = entry_prob - current_prob

    # --- Scan adjacent buckets for the new mode ---
    alternative_buckets = []
    forecast_rounded = round(forecast_in_unit)

    # Scan ±SHIFT_SCAN_RANGE buckets around the forecast
    step = 2.0 if unit.upper() == "F" else 1.0
    for offset in range(-SHIFT_SCAN_RANGE, SHIFT_SCAN_RANGE + 1):
        candidate_low = forecast_rounded + offset * step
        candidate_high = candidate_low + step

        # Skip the held bucket itself
        if (candidate_low == bucket_low and candidate_high == bucket_high):
            continue
        # Skip nonsensical temperature ranges
        if candidate_low < -60 or candidate_high > 60:
            continue

        try:
            if use_bayesian and observed_in_unit is not None:
                alt_prob = bayesian_metar_probability(
                    forecast_temp=forecast_in_unit,
                    observed_temp=observed_in_unit,
                    local_hour=local_hour,
                    bucket_low=candidate_low,
                    bucket_high=candidate_high,
                    unit=unit,
                    market_date=market_date,
                )
            else:
                alt_prob = forecast_probability(
                    forecast_temp=forecast_in_unit,
                    bucket_low=candidate_low,
                    bucket_high=candidate_high,
                    unit=unit,
                    market_date=market_date,
                    city=city,
                market_type=market_type,
                )
        except Exception:
            continue

        alternative_buckets.append({
            "low": candidate_low,
            "high": candidate_high,
            "prob": alt_prob,
        })

    # Find the best alternative
    best_alt = None
    if alternative_buckets:
        alternative_buckets.sort(key=lambda b: b["prob"], reverse=True)
        best_alt = alternative_buckets[0]

    # Compute current market price for PnL estimate
    try:
        current_price = get_midpoint_price(pos["token_id"], client=clob_client)
        if current_price is None:
            current_price = get_market_price(pos["token_id"], client=clob_client)
    except Exception:
        current_price = None

    profit_pct = ((current_price - entry_price) / entry_price) if current_price and entry_price > 0 else 0.0

    # --- Decide whether to exit ---
    # Two triggers (either is sufficient):
    #   (a) Held bucket probability fell below absolute floor (model abandoned it)
    #   (b) A different bucket is the new mode and leads by ≥SHIFT_ALT_LEAD_MARGIN
    alt_beats_held = best_alt["prob"] > current_prob if best_alt else False
    alt_lead_margin = (best_alt["prob"] - current_prob) if alt_beats_held else 0.0
    held_collapsed = current_prob < SHIFT_EXIT_PROB_FLOOR

    if not held_collapsed and not (alt_beats_held and alt_lead_margin >= SHIFT_ALT_LEAD_MARGIN):
        return None  # Held bucket still plausible; no alternative clearly beats it

    # Build exit reason
    alt_desc = ""
    if best_alt and best_alt["prob"] > current_prob:
        alt_desc = (
            f" | new_mode=[{best_alt['low']},{best_alt['high']}]{unit} "
            f"prob={best_alt['prob']:.1%}"
        )

    exit_reason = (
        f"probability_shift: prob={current_prob:.1%} (was {entry_prob:.1%}) | "
        f"drop={prob_drop:+.1%} | "
        f"profit={profit_pct:.0%}{alt_desc}"
    )

    logger.info(
        f"PROBABILITY SHIFT EXIT: {city} {market_date} | "
        f"[{bucket_low},{bucket_high}]{unit} | {exit_reason}"
    )

    if notifier:
        alt_msg = ""
        if best_alt and best_alt["prob"] > current_prob:
            alt_msg = (
                f"\nNew best: [{best_alt['low']},{best_alt['high']}]{unit} "
                f"at {best_alt['prob']:.1%} prob"
            )
        notifier.send_message(
            f"<b>[!] Probability Shift — Exiting</b>\n"
            f"{city} {market_date} [{bucket_low},{bucket_high}]{unit}\n"
            f"Entry prob: {entry_prob:.1%} → Now: {current_prob:.1%}\n"
            f"Shares: {shares:.1f} @ entry ${entry_price:.3f}{alt_msg}"
        )

    return {
        "position_id": pos["id"],
        "token_id": pos["token_id"],
        "shares": shares,
        "reason": exit_reason,
        "current_price": current_price if current_price else 0.01,
        "profit_pct": profit_pct,
        "updated_prob": current_prob,
        "edge": current_prob - (current_price or 0.01),
        "switch_bucket": best_alt,  # Phase 2: auto-switch candidate
        "city": city,
        "market_date": market_date,
        "unit": unit,
    }


# ---------------------------------------------------------------------------
# Phase 2: Auto-switch — buy new bucket after probability shift exit
# ---------------------------------------------------------------------------

# Don't switch the same (city, date) more than once per window
SWITCH_COOLDOWN_HOURS = 3

# Minimum edge required on the new bucket to justify switching
SWITCH_ENTRY_THRESHOLD = 0.20

# In-memory switch tracker (resets on restart — acceptable for anti-flap)
_switch_tracker: dict[str, float] = {}  # key=(city, date) → timestamp


def _build_candidate_slug(city: str, date_str: str, bucket_low: float,
                          bucket_high: float, unit: str) -> str:
    """Construct a Polymarket weather market slug for a candidate bucket."""
    # Convert date format: 2026-05-01 → may-1-2026
    from datetime import datetime
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    month_names = ["january", "february", "march", "april", "may", "june",
                   "july", "august", "september", "october", "november", "december"]
    month_str = month_names[dt.month - 1]
    day_str = str(dt.day)
    year_str = str(dt.year)

    # City slug: lowercase, spaces → hyphens
    city_slug = city.lower().replace(" ", "-")

    # Bucket suffix
    if bucket_low is None:
        # orbelow
        bucket_slug = f"{int(bucket_high)}{unit.lower()}orbelow"
    elif bucket_high is None:
        # orhigher
        bucket_slug = f"{int(bucket_low)}{unit.lower()}orhigher"
    else:
        # Standard bucket: 17c or 52-53f
        if unit.upper() == "F":
            bucket_slug = f"{int(bucket_low)}-{int(bucket_high)}{unit.lower()}"
        else:
            bucket_slug = f"{int(bucket_low)}{unit.lower()}"

    return f"highest-temperature-in-{city_slug}-on-{month_str}-{day_str}-{year_str}-{bucket_slug}"


def _lookup_market_by_slug(slug: str) -> Optional[dict]:
    """Query Gamma API for a single market by slug. Returns market dict or None."""
    import requests

    url = "https://gamma-api.polymarket.com/events"
    try:
        r = requests.get(
            url,
            params={"slug": slug, "limit": 1},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug(f"Market lookup failed for slug {slug[:60]}: {e}")
        return None

    if not isinstance(data, list) or not data:
        return None

    event = data[0]
    markets = event.get("markets", [])
    if not markets:
        return None

    # Return the first market (there should be exactly one for a specific slug)
    market = markets[0]
    return {
        "condition_id": market.get("conditionId", ""),
        "token_ids": market.get("tokenIds", []),
        "yes_token_id": market.get("tokenIds", ["", ""])[0] if len(market.get("tokenIds", [])) >= 1 else "",
        "slug": market.get("slug", slug),
        "question": market.get("question", ""),
        "outcomes": market.get("outcomes", []),
    }


def _execute_switch_entry(exit_action: dict, clob_client=None,
                          notifier=None) -> Optional[dict]:
    """
    After a probability shift exit, buy the new max-prob bucket.

    Args:
        exit_action: dict from _check_probability_shift with switch_bucket data
        clob_client: CLOB client instance
        notifier: TelegramNotifier instance

    Returns:
        dict with buy order response, or None
    """
    switch = exit_action.get("switch_bucket")
    if not switch:
        return None

    city = exit_action.get("city", "")
    market_date = exit_action.get("market_date", "")
    unit = exit_action.get("unit", "F")
    bucket_low = switch["low"]
    bucket_high = switch["high"]
    new_prob = switch["prob"]

    # Anti-flap: don't switch same city/date twice in cooldown window
    key = (city, market_date)
    now = datetime.now(timezone.utc).timestamp()
    if key in _switch_tracker:
        last = _switch_tracker[key]
        if (now - last) < SWITCH_COOLDOWN_HOURS * 3600:
            logger.info(
                f"Switch cooldown: {city} {market_date} — "
                f"last switch {(now - last) / 3600:.1f}h ago"
            )
            return None

    # Look up the market for the new bucket
    slug = _build_candidate_slug(city, market_date, bucket_low,
                                  bucket_high, unit)
    market = _lookup_market_by_slug(slug)
    if not market or not market.get("yes_token_id"):
        logger.warning(
            f"Switch entry: market not found for {slug[:60]}"
        )
        return None

    yes_token_id = market["yes_token_id"]

    # Compute entry price and edge
    # Simple model-discount pricing: model_prob * 0.65, capped at 0.50
    entry_price = min(new_prob * 0.65, 0.50)
    edge = find_edge(new_prob, entry_price)

    if not should_trade(edge, forecast_prob=new_prob, market_date=market_date):
        logger.info(
            f"Switch entry: edge insufficient for {city} {market_date} "
            f"[{bucket_low},{bucket_high}]{unit} | edge={edge:+.1%}"
        )
        return None

    # Size the position — simple fixed sizing for switch entries
    size = min(3.0, 3.0)  # Fixed $3 for switch entries

    # Place the buy order
    from executor import place_buy_order
    logger.info(
        f"SWITCH ENTRY: {city} {market_date} "
        f"[{bucket_low},{bucket_high}]{unit} | prob={new_prob:.1%} | "
        f"entry=${entry_price:.3f} | edge={edge:+.1%} | size=${size:.2f}"
    )

    resp = place_buy_order(
        token_id=yes_token_id,
        price=entry_price,
        size_usdc=size,
    )

    _switch_tracker[key] = now

    if notifier and resp.get("status") not in ("FAILED", "REJECTED"):
        notifier.send_message(
            f"<b>[🔄] Switched Position</b>\n"
            f"{city} {market_date} → [{bucket_low},{bucket_high}]{unit}\n"
            f"Entry: ${entry_price:.3f} | Size: ${size:.2f}\n"
            f"Prob: {new_prob:.1%} | Edge: {edge:+.1%}"
        )

    return resp


def _format_bucket(bucket_low, bucket_high, unit) -> str:
    """Format bucket bounds as a human-readable string."""
    if bucket_low is not None and bucket_high is not None:
        return f"[{bucket_low}-{bucket_high}){unit}"
    elif bucket_low is not None:
        return f"[{bucket_low}+){unit}"
    elif bucket_high is not None:
        return f"(≤{bucket_high}]{unit}"
    else:
        return f"any{unit}"


def _check_wu_mismatch(pos: dict, clob_client=None) -> Optional[dict]:
    """Check if Wunderground forecast has revised away from the purchased bucket.

    For markets resolving tomorrow or later:
    - Auto-exit the old position (market sell, FOK)
    - Find the new correct bucket from revised WU forecast
    - Re-enter if exposure cap allows

    For same-day markets:
    - Alert only (METAR may still shift the actual resolution temp)

    Returns:
        dict with action details, or None if WU still agrees with position.
        Keys:
            alert_only: bool — True for same-day (no auto-action)
            action: "exit_only" | "exit_and_reenter" | "alert"
            reason: str — human-readable explanation
            wu_temp: float — revised WU temperature
            wu_temp_c: float — revised WU temperature in Celsius
            new_bucket_low: float | None — new bucket lower bound (if re-entering)
            new_bucket_high: float | None — new bucket upper bound (if re-entering)
            position_id, city, market_date, bucket_low, bucket_high, unit, token_id, shares
    """
    import time as _time
    from wunderground_client import fetch_forecasts, get_forecast_for_date, wunderground_match
    from datetime import datetime, date, timedelta

    city = pos.get("city", "")
    market_date = pos.get("market_date", "")
    bucket_low = pos.get("bucket_low")
    bucket_high = pos.get("bucket_high")
    unit = pos.get("unit", "C")
    pos_id = pos.get("id")

    if not city or not market_date:
        return None

    # Rate-limit per position
    now_ts = _time.time()
    last_action = _wu_alert_last.get(pos_id, 0)
    if now_ts - last_action < WU_MISMATCH_COOLDOWN_HOURS * 3600:
        return None

    # Determine if this is a same-day market (alert-only mode)
    try:
        market_dt = datetime.strptime(market_date, "%Y-%m-%d").date()
        today = date.today()
        is_same_day = (market_dt == today)
    except ValueError:
        is_same_day = False

    # Fetch fresh WU forecast for this city
    try:
        results = fetch_forecasts([city])
        city_data = results.get(city)
        if not city_data or city_data.get("error"):
            logger.debug(f"WU mismatch check: no data for {city}")
            return None

        day = get_forecast_for_date(city_data, market_date)
        if not day:
            logger.debug(f"WU mismatch check: no forecast for {city} on {market_date}")
            return None

        # Determine which WU temperature to compare
        market_type = pos.get("market_type", "highest")
        if market_type == "lowest":
            wu_temp_c = day.get("low_c")
        else:
            wu_temp_c = day.get("high_c")

        if wu_temp_c is None:
            logger.debug(f"WU mismatch check: no temp for {city} {market_date}")
            return None

        # Convert WU Celsius to market unit
        if unit.upper() == "F":
            wu_temp = round(wu_temp_c * 9 / 5 + 32)
        else:
            wu_temp = wu_temp_c

        # Check: does the WU forecast point still fall inside the bucket?
        in_bucket = False
        if bucket_low is not None and bucket_high is not None:
            in_bucket = bucket_low <= wu_temp < bucket_high
        elif bucket_low is not None and bucket_high is None:
            in_bucket = wu_temp >= bucket_low
        elif bucket_low is None and bucket_high is not None:
            in_bucket = wu_temp <= bucket_high

        if in_bucket:
            # WU still agrees — position is valid
            return None

        # ── Mismatch detected ────────────────────────────────────────────
        bucket_str = _format_bucket(bucket_low, bucket_high, unit)
        alert_reason = (
            f"WU FORECAST REVISED: {city} {market_date} | "
            f"WU now says {wu_temp}°{unit} — position is in {bucket_str}"
        )
        logger.info(f"WU MISMATCH: {alert_reason}")

        # Record action timestamp for cooldown
        _wu_alert_last[pos_id] = now_ts

        # ── Same-day: alert only (METAR may still shift actual temp) ────
        if is_same_day:
            return {
                "position_id": pos_id,
                "alert_only": True,
                "action": "alert",
                "reason": alert_reason + " | SAME-DAY: alert only, no auto-exit",
                "wu_temp": wu_temp,
                "wu_temp_c": wu_temp_c,
                "city": city,
                "market_date": market_date,
                "bucket_low": bucket_low,
                "bucket_high": bucket_high,
                "unit": unit,
                "token_id": pos.get("token_id"),
                "shares": pos.get("shares", 0),
            }

        # ── Future date: find new correct bucket ────────────────────────
        # Use wunderground_match to find which bucket WU now points to
        new_bucket_low = None
        new_bucket_high = None
        reenter_result = "no_reenter"

        # We need to find the new bucket from the market's available buckets
        # For now, compute the bucket that contains the WU temp
        # This is a simplified approach — the actual market may not have this exact bucket
        if wu_temp_c is not None:
            # Compute the 1°C bucket that contains the WU temp
            if unit.upper() == "F":
                # 2°F buckets for US markets
                bucket_size = 2
                new_bucket_low = (wu_temp // bucket_size) * bucket_size
                new_bucket_high = new_bucket_low + bucket_size
            else:
                # 1°C buckets for non-US markets
                new_bucket_low = int(wu_temp_c)
                new_bucket_high = new_bucket_low + 1

            reenter_result = f"new_bucket=[{new_bucket_low}-{new_bucket_high})"

        return {
            "position_id": pos_id,
            "alert_only": False,
            "action": "exit_and_reenter",
            "reason": alert_reason + f" | AUTO-EXIT + REENTER {reenter_result}",
            "wu_temp": wu_temp,
            "wu_temp_c": wu_temp_c,
            "new_bucket_low": new_bucket_low,
            "new_bucket_high": new_bucket_high,
            "city": city,
            "market_date": market_date,
            "bucket_low": bucket_low,
            "bucket_high": bucket_high,
            "unit": unit,
            "token_id": pos.get("token_id"),
            "shares": pos.get("shares", 0),
        }

    except Exception as e:
        logger.warning(f"WU mismatch check failed for {city} {market_date}: {e}")
        return None


def monitor_positions(notifier=None) -> dict:
    """
    Main monitoring function. Checks all open positions for exit signals
    and executes sells where warranted.

    Args:
        notifier: TelegramNotifier instance for alerts (optional)

    Returns:
        dict with monitoring summary:
            positions_checked, exits_triggered, exits_executed, errors
    """
    positions = get_open_positions()
    if not positions:
        return {"positions_checked": 0, "exits_triggered": 0, "exits_executed": 0, "errors": 0}

    logger.info(f"Monitoring {len(positions)} open position(s)")

    try:
        clob_client = get_clob_client()
    except Exception as e:
        logger.error(f"Could not create CLOB client for monitoring: {e}")
        return {"positions_checked": len(positions), "exits_triggered": 0, "exits_executed": 0, "errors": 1}

    exits_triggered = 0
    exits_executed = 0
    errors = 0

    for pos in positions:
        # Skip positions that have exceeded max exit retries
        if _should_skip_exit(pos["id"]):
            logger.debug(
                f"Skipping exit for position {pos['id']} ({pos['city']} "
                f"{pos['market_date']}): exceeded {MAX_EXIT_RETRIES} retries"
            )
            continue

        # Skip delayed-settlement positions — their shares haven't been
        # delivered on-chain yet. The settlement_verifier handles these.
        if pos.get("exit_reason") == "delayed_settling":
            logger.debug(
                f"Skipping exit for position {pos['id']} ({pos['city']} "
                f"{pos['market_date']}): pending delayed settlement"
            )
            continue

        # Check 0: Market closed/resolved (HIGHEST PRIORITY)
        # If Gamma API or CLOB says the market is done, record resolution immediately.
        # All other checks are meaningless — orders are null and void.
        closed_action = _check_market_closed(pos, clob_client)
        if closed_action is not None:
            won = closed_action["won"]
            try:
                record_resolution(
                    position_id=pos["id"],
                    won=won,
                    actual_temp=None,
                    actual_temp_source=closed_action["source"],
                )
                exits_triggered += 1
                exits_executed += 1
                logger.info(
                    f"RESOLVED via API: #{pos['id']} {pos['city']} {pos['market_date']} → "
                    f"{'WON' if won else 'LOST'}"
                )
                if notifier:
                    notifier.send_message(
                        f"<b>[{'✓' if won else '✗'}] Market Resolved (API)</b>\n"
                        f"{pos['city']} {pos['market_date']}\n"
                        f"Bucket: [{pos.get('bucket_low')},{pos.get('bucket_high')}]\n"
                        f"Result: <b>{'WON' if won else 'LOST'}</b>\n"
                        f"Entry: ${pos['entry_price']:.3f} × {pos['shares']:.1f}sh = ${pos['size_usdc']:.2f}"
                    )
            except Exception as e:
                logger.error(f"Failed to record resolution for #{pos['id']}: {e}")
                errors += 1
            continue

        # Check 1: Same-day exit (observed temp has moved past bucket)
        exit_action = _check_same_day_exit(pos, clob_client)

        # Stop-loss: price down 25%+ OR model probability collapsed
        if exit_action is None:
            exit_action = _check_stop_loss(pos, clob_client)

        # Then check profit-taking
        if exit_action is None:
            exit_action = _check_profit_take(pos, clob_client)

        # Then check edge-collapse (capital recycling)
        if exit_action is None:
            exit_action = _check_edge_collapse(pos, clob_client)

        # Then check probability shift (same-day adaptive exit)
        if exit_action is None:
            exit_action = _check_probability_shift(pos, clob_client)

        # Then check Wunderground forecast revision (auto-exit for future dates)
        if exit_action is None:
            exit_action = _check_wu_mismatch(pos, clob_client)

        if exit_action is None:
            continue

        # ── Handle WU mismatch ──────────────────────────────────────────
        if exit_action.get("action") == "alert":
            # Same-day market: alert only, no auto-action
            if notifier:
                notifier.send_message(
                    f"<b>⚠️ WU Forecast Revised (Same-Day)</b>\n\n"
                    f"<b>{exit_action['city']} {exit_action['market_date']}</b>\n"
                    f"Position: {_format_bucket(exit_action['bucket_low'], exit_action['bucket_high'], exit_action['unit'])}\n"
                    f"WU now says: <b>{exit_action['wu_temp']}°{exit_action['unit']}</b>\n\n"
                    f"<i>Same-day market — bot will NOT auto-exit. "
                    f"Monitor manually.</i>"
                )
            exits_triggered += 1
            continue

        if exit_action.get("action") == "exit_and_reenter":
            # Future date: auto-exit old position, then re-enter new bucket
            logger.info(f"WU REVISION AUTO-EXIT: {exit_action['reason']}")

            # Check re-entry cooldown
            import time as _time
            now_ts = _time.time()
            last_exit = _wu_exit_last.get(pos.get("id"), 0)
            if now_ts - last_exit < WU_REVISION_REENTRY_DELAY_MIN * 60:
                logger.info(
                    f"WU REVISION: cooldown active for position {pos.get('id')}, "
                    f"skipping re-entry"
                )
                # Still exit, just don't re-enter
                exit_action["action"] = "exit_only"
                exit_action["reason"] += " | COOLDOWN: no re-entry"

            # Execute the sell
            sell_price = get_midpoint_price(exit_action["token_id"], client=clob_client)
            if sell_price is None:
                sell_price = get_market_price(exit_action["token_id"], client=clob_client)
            if sell_price is None:
                logger.warning(
                    f"WU REVISION: cannot determine sell price for position "
                    f"{pos.get('id')}, skipping exit"
                )
                errors += 1
                continue

            sell_price = max(sell_price, 0.01)

            # Execute sell order
            try:
                from executor import place_sell_order
                sell_result = place_sell_order(
                    exit_action["token_id"],
                    sell_price,
                    exit_action["shares"],
                )
                logger.info(
                    f"WU REVISION SELL: position {pos.get('id')} | "
                    f"sold {exit_action['shares']} shares @ ${sell_price:.3f} | "
                    f"result={sell_result}"
                )
                exits_executed += 1

                # Record exit timestamp
                _wu_exit_last[pos.get("id")] = now_ts

                # Send Telegram alert
                if notifier:
                    msg = (
                        f"<b>🔄 WU Revision Auto-Exit</b>\n\n"
                        f"<b>{exit_action['city']} {exit_action['market_date']}</b>\n"
                        f"Old: {_format_bucket(exit_action['bucket_low'], exit_action['bucket_high'], exit_action['unit'])}\n"
                        f"WU revised to: <b>{exit_action['wu_temp']}°{exit_action['unit']}</b>\n"
                        f"Sold {exit_action['shares']} shares @ ${sell_price:.3f}\n"
                    )
                    if exit_action.get("new_bucket_low") is not None:
                        msg += f"New bucket: [{exit_action['new_bucket_low']}-{exit_action['new_bucket_high']})\n"
                    notifier.send_message(msg)

            except Exception as e:
                logger.error(
                    f"WU REVISION SELL FAILED: position {pos.get('id')}: {e}",
                    exc_info=True,
                )
                errors += 1
                continue

            # Note: Re-entry into new bucket would require finding the new token_id
            # and placing a buy order. This is complex because we need to:
            # 1. Find the market for the new bucket
            # 2. Get the token_id for that bucket
            # 3. Check exposure cap
            # 4. Place buy order
            # For now, we exit and free up capital. The next scan cycle will
            # naturally pick up the new bucket if it has edge.
            logger.info(
                f"WU REVISION: exited position {pos.get('id')}, "
                f"new bucket [{exit_action.get('new_bucket_low')}-{exit_action.get('new_bucket_high')}) "
                f"will be picked up by next scan if edge exists"
            )
            exits_triggered += 1
            continue

        # ── Standard exit handling (non-WU-revision exits) ──────────────
        exits_triggered += 1

        # Determine sell price
        if "current_price" in exit_action:
            # Profit-taking: sell at current market price
            sell_price = exit_action["current_price"]
        else:
            # Same-day exit: sell at best available price (use midpoint or lower)
            sell_price = get_midpoint_price(exit_action["token_id"], client=clob_client)
            if sell_price is None:
                sell_price = get_market_price(exit_action["token_id"], client=clob_client)
            if sell_price is None:
                logger.warning(
                    f"Cannot determine sell price for position {pos['id']}, skipping exit"
                )
                errors += 1
                continue

        # Clamp to minimum tick size (0.01) — avoids "Invalid sell price" errors
        # when books are near-zero (e.g. 0.001 on a near-certain loser)
        sell_price = max(sell_price, 0.01)

        # Validate before selling (thin books can produce bad prices/sizes)
        if sell_price is None or sell_price <= 0 or exit_action["shares"] < 5:
            logger.warning(
                f"Skipping exit for position {pos['id']}: "
                f"price={sell_price}, shares={exit_action['shares']} (validation failed)"
            )
            errors += 1
            continue

        # Place the sell order
        resp = place_sell_order(
            token_id=exit_action["token_id"],
            price=sell_price,
            num_shares=exit_action["shares"],
        )

        order_status = resp.get("status", "FAILED")
        order_id = resp.get("orderID", "N/A")

        if order_status in ("simulated", "MATCHED", "LIVE"):
            _clear_exit_retries(exit_action["position_id"])
            record_exit(
                position_id=exit_action["position_id"],
                exit_price=sell_price,
                exit_reason=exit_action["reason"],
                exit_order_id=order_id,
            )
            exits_executed += 1

            # Phase 2: If this was a probability shift exit, switch to new bucket
            if exit_action.get("switch_bucket") and exit_action.get("reason", "").startswith("probability_shift"):
                logger.info(
                    f"Attempting switch entry after probability shift exit "
                    f"for {pos.get('city', '?')} {pos.get('market_date', '?')}"
                )
                switch_resp = _execute_switch_entry(
                    exit_action, clob_client, notifier
                )
                if switch_resp:
                    switch_status = switch_resp.get("status", "?")
                    logger.info(
                        f"Switch entry result: {switch_status} "
                        f"for {pos.get('city', '?')} {pos.get('market_date', '?')}"
                    )

            # Send Telegram notification
            if notifier:
                pos_data = get_position_by_id(exit_action["position_id"])
                pnl = pos_data["pnl_usdc"] if pos_data else 0.0
                notifier.send_message(
                    f"<b>Position Exit</b>\n"
                    f"Market: {pos['city']} {pos['market_date']}\n"
                    f"Reason: {exit_action['reason'][:100]}\n"
                    f"Sell: {exit_action['shares']:.1f} shares @ {sell_price:.3f}\n"
                    f"P&L: ${pnl:+.2f}\n"
                    f"Order: {order_status} ({order_id})"
                )
        elif order_status == "delayed":
            # Order accepted by CLOB, awaiting settlement.
            # Record provisionally — keep status=open so settlement_verifier picks it up.
            _clear_exit_retries(exit_action["position_id"])
            delayed_reason = "delayed_exit: " + exit_action["reason"]
            import sqlite3
            conn = sqlite3.connect("/root/weatherbot/positions.db")
            conn.execute(
                """UPDATE positions SET exit_reason = ?, exit_order_id = ?,
                   exit_price = ? WHERE id = ?""",
                (delayed_reason, order_id, sell_price, exit_action["position_id"]),
            )
            conn.commit()
            conn.close()
            exits_executed += 1
            logger.info(
                f"Exit order DELAYED (awaiting settlement): id={order_id} | "
                f"token={exit_action['token_id'][:16]}... | "
                f"{exit_action['shares']:.1f} shares @ ${sell_price:.3f} | "
                f"reason={delayed_reason[:60]}"
            )
            if notifier:
                pos_data = get_position_by_id(exit_action["position_id"])
                pnl = pos_data["pnl_usdc"] if pos_data else 0.0
                notifier.send_message(
                    f"<b>Position Exit (Delayed)</b>\n"
                    f"Market: {pos['city']} {pos['market_date']}\n"
                    f"Reason: {exit_action['reason'][:100]}\n"
                    f"Sell: {exit_action['shares']:.1f} shares @ {sell_price:.3f}\n"
                    f"Est P&L: ${pnl:+.2f}\n"
                    f"Order: delayed \u2014 {order_id}"
                )

        else:
            _record_exit_failure(pos["id"])
            retry_count = _exit_retry_counts.get(pos["id"], 0)
            logger.error(
                f"Exit order failed for position {pos['id']} "
                f"(attempt {retry_count}/{MAX_EXIT_RETRIES}): {resp}"
            )
            errors += 1
            if retry_count >= MAX_EXIT_RETRIES and notifier:
                notifier.notify_error(
                    "Position Exit",
                    f"Giving up on position {pos['id']} ({pos['city']} "
                    f"{pos['market_date']}) after {MAX_EXIT_RETRIES} attempts: "
                    f"{resp.get('reason', 'unknown')}"
                )

    summary = {
        "positions_checked": len(positions),
        "exits_triggered": exits_triggered,
        "exits_executed": exits_executed,
        "errors": errors,
    }

    if exits_triggered > 0:
        logger.info(
            f"Monitor complete: {exits_triggered} exit(s) triggered, "
            f"{exits_executed} executed, {errors} error(s)"
        )

    # Layer 2: Verify delayed orders that were provisionally recorded
    settlement = verify_delayed_settlements(notifier)
    if settlement["checked"] > 0:
        summary["settlement_checked"] = settlement["checked"]
        summary["settlement_updated"] = settlement["updated"]
        summary["settlement_pending"] = settlement["still_pending"]
        summary["settlement_abandoned"] = settlement["abandoned"]
        summary["settlement_errors"] = settlement["errors"]

    return summary


def resolve_past_positions(notifier=None) -> dict:
    """
    Checks positions for past market dates and resolves them as won/lost
    based on actual observed temperatures.

    Returns:
        dict with resolution summary:
            checked, resolved, won, lost, boundary_flags, errors
    """
    from positions import get_unresolved_past_positions, record_resolution
    from observed_temps import resolve_positions

    past_positions = get_unresolved_past_positions()
    if not past_positions:
        return {"checked": 0, "resolved": 0, "won": 0, "lost": 0, "boundary_flags": 0, "errors": 0}

    logger.info(f"Checking {len(past_positions)} past position(s) for resolution")

    results = resolve_positions(past_positions)

    won = 0
    lost = 0
    boundary_flags = 0

    for result in results:
        record_resolution(
            position_id=result["position_id"],
            won=result["won"],
            actual_temp=result["actual_temp_unit"],
            actual_temp_source=result["source"],
        )

        if result["won"]:
            won += 1
        else:
            lost += 1

        if result["boundary_flag"]:
            boundary_flags += 1
            if notifier:
                notifier.send_message(
                    f"<b>[!] Boundary Resolution</b>\n"
                    f"City: {result['city']} {result['market_date']}\n"
                    f"Actual: {result['actual_temp_unit']:.1f}{result['unit']}\n"
                    f"Bucket: [{result['bucket_low']},{result['bucket_high']}]{result['unit']}\n"
                    f"Margin: {result['margin']:.1f} deg\n"
                    f"Result: {'WON' if result['won'] else 'LOST'}\n"
                    f"<i>Within 1 deg of boundary. Verify against official source.</i>"
                )

        if notifier:
            pos = get_position_by_id(result["position_id"])
            pnl = pos["pnl_usdc"] if pos else 0.0
            icon = "+" if result["won"] else "x"
            notifier.send_message(
                f"<b>[{icon}] Market Resolved</b>\n"
                f"{result['city']} {result['market_date']}\n"
                f"Actual: {result['actual_temp_unit']:.1f}{result['unit']} | "
                f"Bucket: [{result['bucket_low']},{result['bucket_high']}]\n"
                f"{'WON' if result['won'] else 'LOST'} | P&L: ${pnl:+.2f}"
            )

            # Update notifier stats
            notifier.record_settlement(won=result["won"])

    summary = {
        "checked": len(past_positions),
        "resolved": len(results),
        "won": won,
        "lost": lost,
        "boundary_flags": boundary_flags,
        "errors": len(past_positions) - len(results),
    }

    logger.info(
        f"Resolution complete: {won} won, {lost} lost, "
        f"{boundary_flags} boundary flag(s), "
        f"{summary['errors']} error(s)"
    )

    return summary


if __name__ == "__main__":
    print("Position Monitor Module")
    print("=" * 50)
    print(f"DRY_RUN: {DRY_RUN}")
    print(f"Temp exit margin: {TEMP_EXIT_MARGIN_DEG} degrees")
    print(f"Profit take threshold: {PROFIT_TAKE_THRESHOLD:.0%}")
    print(f"Profit take prob ceiling: {PROFIT_TAKE_PROB_CEILING:.0%}")
    print(f"Fast monitor threshold: {FAST_MONITOR_HOURS} hours")
    print()
    print("Functions available:")
    print("  monitor_positions(notifier) - Check exits and profit-taking")
    print("  resolve_past_positions(notifier) - Resolve past-date positions")
    print("  needs_fast_monitoring() - Check if 15-min loop needed")
