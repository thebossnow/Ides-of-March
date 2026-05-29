"""
position_monitor.py - Monitors open positions for exit signals.

Two exit strategies:
1. Same-day exit: If the observed daily max temperature has moved 2+ degrees
   beyond our bucket boundary, sell to cut losses. The market hasn't resolved
   yet, but the position is very likely to lose.

2. Profit-taking: If the current market price of our YES shares gives 50%+
   profit over entry price AND our updated forecast probability has dropped
   below 60%, sell and lock in profits rather than risk resolution.

Monitoring frequency:
  - 30 min for positions 12-24 hours from resolution
  - 15 min for positions within 12 hours of resolution

Called from bot.py's main loop.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from positions import (
    get_open_positions,
    record_exit,
    get_position_by_id,
)
from observed_temps import get_current_day_max
from executor import place_sell_order, DRY_RUN
from markets import get_midpoint_price, get_market_price, get_client as get_clob_client, get_bid_price
from strategy import forecast_probability, convert_forecast_to_market_unit
from weather import get_forecast

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Exit thresholds
# -----------------------------------------------------------------------
# Same-day exit: sell if observed max is this many degrees outside bucket
TEMP_EXIT_MARGIN_DEG = 2.0

# Profit-taking: sell if unrealized profit >= this fraction
PROFIT_TAKE_THRESHOLD = 0.50  # 50% profit

# Profit-taking: only sell if updated forecast prob dropped below this
PROFIT_TAKE_PROB_CEILING = 0.60  # 60%

# How close to resolution before we use the fast (15-min) check
FAST_MONITOR_HOURS = 12.0


def get_hours_to_resolution(market_date: str, city_tz: str = None) -> float:
    """
    Estimates hours remaining until a market resolves.
    Polymarket weather markets typically resolve at end of day (local time).
    Returns negative if already past resolution.
    """
    try:
        from weather import CITIES
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
    from weather import CITIES

    positions = get_open_positions()
    for pos in positions:
        city_info = CITIES.get(pos["city"])
        tz = city_info["tz"] if city_info else None
        hours = get_hours_to_resolution(pos["market_date"], tz)
        if 0 < hours <= FAST_MONITOR_HOURS:
            return True
    return False


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

    current = get_current_day_max(city)
    if current is None:
        logger.debug(f"No current temp data for {city}, skipping exit check")
        return None

    # Convert to market unit
    unit = pos.get("unit", "F")
    if unit.upper() == "F":
        current_max = current["temp_f"]
    else:
        current_max = current["temp_c"]

    bucket_low = pos["bucket_low"]
    bucket_high = pos["bucket_high"]

    # Check if current max has blown past our bucket
    exit_reason = None

    # Case 1: temp already exceeded bucket ceiling (applies to range buckets
    # AND open-ended "X or lower" buckets where bucket_high is set).
    # The daily max never decreases, so exceeding bucket_high means certain loss.
    if bucket_high is not None and current_max >= bucket_high + TEMP_EXIT_MARGIN_DEG:
        exit_reason = (
            f"same_day_exit: observed max {current_max:.1f}{unit} already "
            f"{current_max - bucket_high:.1f} above bucket ceiling {bucket_high}{unit}"
        )

    # Case 2: open-ended HIGH market ("X or higher", bucket_low=X, bucket_high=None).
    # If it's late in the day and the max is still well below the floor,
    # the position is a near-certain loser — cut it.
    if exit_reason is None and bucket_low is not None and bucket_high is None:
        from weather import CITIES as _CITIES_PM
        city_tz = _CITIES_PM.get(city, {}).get("tz")
        hours_left = get_hours_to_resolution(market_date, city_tz)
        if hours_left < 6.0 and current_max < bucket_low - TEMP_EXIT_MARGIN_DEG:
            exit_reason = (
                f"same_day_exit: observed max {current_max:.1f}{unit} is "
                f"{bucket_low - current_max:.1f} below floor {bucket_low}{unit} "
                f"with only {hours_left:.1f}h remaining"
            )

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

    # Use bid price for profit calculation: this is what a FOK sell will
    # actually receive. Using midpoint would overstate realizable profit.
    current_price = get_bid_price(token_id, client=clob_client)
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
    unit = pos.get("unit", "F")

    try:
        forecast_data = get_forecast(city, days=6)
        if market_date not in forecast_data:
            # Can't update forecast, assume risk exists
            updated_prob = 0.50
        else:
            forecast_celsius = forecast_data[market_date]
            forecast_in_unit = convert_forecast_to_market_unit(forecast_celsius, unit)
            updated_prob = forecast_probability(
                forecast_temp=forecast_in_unit,
                bucket_low=pos["bucket_low"],
                bucket_high=pos["bucket_high"],
                unit=unit,
                market_date=market_date,
            )
    except Exception as e:
        logger.debug(f"Could not update forecast for profit check: {e}")
        updated_prob = 0.50  # Assume moderate risk if forecast fails

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
        # Check same-day exit first (higher priority)
        exit_action = _check_same_day_exit(pos, clob_client)

        # Then check profit-taking
        if exit_action is None:
            exit_action = _check_profit_take(pos, clob_client)

        if exit_action is None:
            continue

        exits_triggered += 1

        # Determine sell price.
        # FOK sell orders must be placed at or below the best bid to guarantee a fill.
        # Using midpoint would place the limit above the best buyer's price, causing
        # the FOK to be killed instantly (no one buys above bid).
        sell_price = get_bid_price(exit_action["token_id"], client=clob_client)
        if sell_price is None:
            # Fallback: last trade price (acceptable for non-zero liquidity)
            sell_price = get_market_price(exit_action["token_id"], client=clob_client)
        if sell_price is None:
            logger.warning(
                f"Cannot determine sell price for position {pos['id']}, skipping exit"
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
        # Use actual_shares from response (may differ if balance was adjusted)
        actual_shares_sold = resp.get("actual_shares", exit_action["shares"])

        if order_status in ("simulated", "MATCHED", "LIVE"):
            record_exit(
                position_id=exit_action["position_id"],
                exit_price=sell_price,
                exit_reason=exit_action["reason"],
                exit_order_id=order_id,
                actual_shares=actual_shares_sold,
            )
            exits_executed += 1

            # Send Telegram notification
            if notifier:
                pos_data = get_position_by_id(exit_action["position_id"])
                pnl = pos_data["pnl_usdc"] if pos_data else 0.0
                notifier.send_message(
                    f"<b>Position Exit</b>\n"
                    f"Market: {pos['city']} {pos['market_date']}\n"
                    f"Reason: {exit_action['reason'][:100]}\n"
                    f"Sell: {actual_shares_sold:.1f} shares @ {sell_price:.3f}\n"
                    f"P&L: ${pnl:+.2f}\n"
                    f"Order: {order_status} ({order_id})"
                )
        else:
            logger.error(
                f"Exit order failed for position {pos['id']}: {resp}"
            )
            errors += 1
            if notifier:
                notifier.notify_error(
                    "Position Exit",
                    f"Failed to exit position {pos['id']} ({pos['city']} "
                    f"{pos['market_date']}): {resp.get('reason', 'unknown')}"
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
