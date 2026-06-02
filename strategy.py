"""
strategy.py - Edge calculation, probability estimation, and position sizing.
Uses fractional Kelly criterion for conservative position sizing.

Probability estimation uses a Student's t-distribution with dynamic degrees
of freedom (df) that scale with forecast horizon. This produces 'fat tails'
at longer horizons, reducing overconfidence on extreme outcomes and
preserving capital during weather anomalies. At short horizons (df=20),
behavior is near-identical to the previous Normal distribution model.

Sigma (forecast uncertainty) scales with forecast horizon and adjusts for
temperature unit so that Celsius and Fahrenheit markets are treated
equivalently in probability space.
"""

import logging
from datetime import datetime, date
from scipy.stats import t as t_dist
from weather import celsius_to_fahrenheit, fahrenheit_to_celsius

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Tunable parameters - adjust based on your paper-trading results
# -----------------------------------------------------------------------
ENTRY_THRESHOLD      = 0.15   # Only trade if forecast edge > 15%
EXIT_THRESHOLD       = 0.05   # Exit/cancel if edge drops below 5%
MAX_POSITION_USDC    = 25.0   # Hard cap: max USDC per single trade
MIN_POSITION_USDC    = 5.0    # Minimum meaningful trade size
KELLY_FRACTION       = 0.15   # 15% fractional Kelly (conservative)
MIN_HOURS_TO_RES     = 2.0    # Skip markets resolving in < 2 hours
# Dynamic probability floor by forecast horizon (days ahead).
# Tighter when forecast is more reliable (short horizon), looser
# when uncertainty is high (long horizon). Prevents buying buckets
# the model itself considers unlikely to resolve YES.
PROB_FLOOR_BY_HORIZON = {
    0: 0.30,   # Same-day: forecast is tight, demand high confidence
    1: 0.20,   # Tomorrow: still reliable, moderate floor
    2: 0.18,   # 2 days: growing uncertainty
    3: 0.15,   # 3+ days: wide sigma, lower floor
    4: 0.15,
    5: 0.15,
    6: 0.15,
}
PROB_FLOOR_DEFAULT = 0.15  # Fallback for 7+ days

# -----------------------------------------------------------------------
# Dynamic sigma (forecast uncertainty in Fahrenheit)
# Based on NWS/ECMWF verification: 1-day MAE ~2-3F, 3-day ~4-5F.
# Keys are days-ahead (0 = today, 1 = tomorrow, etc.)
# -----------------------------------------------------------------------
SIGMA_BY_HORIZON_F = {
    0: 2.5,    # Same-day: NWS MAE ~2°F × 1.253 (MAE→sigma) → effective std ~2.64°F
    1: 3.0,    # Tomorrow: NWS MAE ~2.5°F × 1.253 → effective std ~3.29°F
    2: 3.5,    # 2 days out
    3: 5.0,    # 3 days out
    4: 6.0,    # 4 days out: growing uncertainty
    5: 7.0,    # 5 days out
    6: 8.0,    # 6 days out
}
SIGMA_DEFAULT_F = 8.0  # Fallback for 7+ days

# -----------------------------------------------------------------------
# Dynamic degrees of freedom for Student's t-distribution.
# Lower df = fatter tails = more probability assigned to extreme outcomes.
# Same-day (df=20) is near-Normal; 3-day (df=5) has meaningful fat tails.
# -----------------------------------------------------------------------
DF_BY_HORIZON = {
    0: 20.0,   # Same-day: near-Normal (95th pctile multiplier ~2.09 vs Normal 1.96)
    1: 12.0,   # Tomorrow: slight fat tails (multiplier ~2.18)
    2:  7.0,   # 2 days out: moderate fat tails (multiplier ~2.37)
    3:  5.0,   # 3 days out: pronounced fat tails (multiplier ~2.57)
    4:  4.0,   # 4 days out: heavier tails
    5:  3.5,   # 5 days out
    6:  3.0,   # 6 days out: very fat tails
}
DF_DEFAULT = 3.0  # Fallback for 7+ days (heavy tails for distant horizons)


def get_prob_floor(market_date_str: str = None) -> float:
    """
    Returns the minimum forecast probability required to trade,
    based on forecast horizon. Shorter horizons require higher
    confidence since the forecast is more reliable.
    """
    if market_date_str is None:
        return PROB_FLOOR_DEFAULT
    try:
        market_date = datetime.strptime(market_date_str, "%Y-%m-%d").date()
        days_ahead = max(0, (market_date - date.today()).days)
    except (ValueError, TypeError):
        return PROB_FLOOR_DEFAULT
    return PROB_FLOOR_BY_HORIZON.get(days_ahead, PROB_FLOOR_DEFAULT)


def _get_params(market_date_str: str, unit: str = "F") -> tuple:
    """
    Returns (sigma, df) for a market based on:
    1. Forecast horizon (days until market date)
    2. Temperature unit (C markets get sigma scaled by 1/1.8)

    sigma controls spread; df controls tail fatness of the t-distribution.

    Args:
        market_date_str: ISO date string (YYYY-MM-DD) of the market
        unit: "F" or "C"

    Returns:
        (sigma, df) tuple
    """
    try:
        market_date = datetime.strptime(market_date_str, "%Y-%m-%d").date()
        days_ahead = (market_date - date.today()).days
        days_ahead = max(0, days_ahead)  # Clamp: same-day or past = 0
    except (ValueError, TypeError):
        days_ahead = 2  # Conservative default if date parse fails

    sigma_f = SIGMA_BY_HORIZON_F.get(days_ahead, SIGMA_DEFAULT_F)
    df = DF_BY_HORIZON.get(days_ahead, DF_DEFAULT)

    if unit.upper() == "C":
        # Convert Fahrenheit sigma to Celsius: divide by 1.8
        return sigma_f / 1.8, df

    return sigma_f, df


def forecast_probability(forecast_temp: float, bucket_low: float | None,
                          bucket_high: float | None, unit: str = "F",
                          model_uncertainty_deg: float = None,
                          market_date: str = None,
                          city_bias: float = 0.0,
                          observed_max: float | None = None) -> float:
    """
    Estimates the probability that the actual temperature falls within the
    bucket [bucket_low, bucket_high] given a point forecast.

    Uses a Student's t-distribution centered on forecast_temp. The degrees
    of freedom (df) scale with forecast horizon: short horizons use high df
    (near-Normal), long horizons use low df (fat tails that assign more
    probability to extreme outcomes, reducing overconfident bets).

    Args:
        forecast_temp:         forecast temperature (in the same unit as bucket)
        bucket_low:            lower bound of bucket (None = -infinity)
        bucket_high:           upper bound of bucket (None = +infinity)
        unit:                  "F" or "C"
        model_uncertainty_deg: override sigma (degrees). If None, computed
                               dynamically from market_date and unit.
        market_date:           ISO date string for dynamic sigma/df calculation
        city_bias:             measured forecast residual (forecast - actual) in
                               the market's unit. Positive = forecast runs warm.
                               Subtracted from mu so a warm-biased forecast is
                               corrected downward toward the true mean.
        observed_max:          running daily max observed so far (same unit as bucket).
                               When provided and > mu, shifts mu up via Bayesian update:
                               the final daily high cannot be below what's already been
                               observed, so we condition on max_final >= observed_max.

    Returns:
        probability float in [0.001, 0.999]
    """
    if model_uncertainty_deg is not None:
        sigma = model_uncertainty_deg
        df = DF_BY_HORIZON.get(2, DF_DEFAULT)
    elif market_date is not None:
        sigma, df = _get_params(market_date, unit)
    else:
        sigma = 3.5 if unit.upper() == "F" else 3.5 / 1.8
        df = DF_BY_HORIZON.get(2, DF_DEFAULT)

    # Apply calibration bias: city_bias = mean(forecast - actual). Subtract to
    # correct: if forecast historically runs warm (+bias), shift mu cooler.
    mu = forecast_temp - city_bias

    # Bayesian intraday update: once we know the running daily max is observed_max,
    # the final daily high is guaranteed to be >= observed_max.  If that floor
    # exceeds the current forecast center, raise mu accordingly.  This prevents
    # the model from assigning probability to outcomes already ruled out by observation.
    if observed_max is not None and observed_max > mu:
        mu = observed_max

    low_p  = t_dist.cdf(bucket_low,  df, loc=mu, scale=sigma) if bucket_low  is not None else 0.0
    high_p = t_dist.cdf(bucket_high, df, loc=mu, scale=sigma) if bucket_high is not None else 1.0

    prob = high_p - low_p
    return max(0.001, min(0.999, prob))


def find_edge(forecast_prob: float, market_price: float) -> float:
    """
    Edge = forecast probability minus market-implied probability.
    Positive edge means market is underpricing our forecast outcome.
    """
    return forecast_prob - market_price


def should_trade(edge: float, forecast_prob: float = None,
                  market_date: str = None) -> bool:
    """
    Returns True if edge exceeds the entry threshold AND forecast
    probability meets the dynamic floor for the given horizon.

    The probability floor prevents the bot from buying buckets that
    the model itself considers unlikely (e.g. 28% forecast prob vs
    12% market price = +16% edge, but still a losing bet most of
    the time). This forces the bot to only bet on outcomes it
    genuinely expects to happen.
    """
    if edge < ENTRY_THRESHOLD:
        return False
    if forecast_prob is not None:
        floor = get_prob_floor(market_date)
        if forecast_prob < floor:
            return False
    return True


def should_exit(edge: float) -> bool:
    """Returns True if edge has dropped below the exit threshold."""
    return edge < EXIT_THRESHOLD


def kelly_position_size(bankroll: float, edge: float, win_prob: float,
                        market_price: float = 0.5) -> float:
    """
    Calculates position size using fractional Kelly criterion.

    Kelly fraction = (b*p - q) / b
      where b = (1/market_price - 1)  <- payout odds from market price
            p = win_prob              <- our forecast probability
            q = 1 - win_prob

    market_price must be passed for correct odds calculation.
    Using win_prob for b is a common mistake that collapses kelly to ~0.

    Returns USDC amount to spend, clamped to [MIN_POSITION_USDC, MAX_POSITION_USDC].
    Returns 0 if kelly is negative (bet has no edge) or inputs are degenerate.
    """
    if win_prob <= 0.0 or win_prob >= 1.0 or bankroll <= 0.0:
        return 0.0
    if market_price <= 0.0 or market_price >= 1.0:
        return 0.0

    lose_prob = 1.0 - win_prob

    # b = net payout odds: how much you win per dollar risked if correct
    # At market_price=0.50 you risk $0.50 to win $1.00, so b=1.0 (even money)
    b = (1.0 / market_price) - 1.0
    if b <= 0.0:
        return 0.0

    kelly_full = (b * win_prob - lose_prob) / b
    if kelly_full <= 0.0:
        logger.debug(f"Negative Kelly ({kelly_full:.3f}) at prob={win_prob:.3f} price={market_price:.3f}, skipping")
        return 0.0

    position = bankroll * kelly_full * KELLY_FRACTION

    # Clamp to allowed range
    position = max(MIN_POSITION_USDC, min(MAX_POSITION_USDC, position))
    return round(position, 2)


def convert_forecast_to_market_unit(forecast_celsius: float, market_unit: str) -> float:
    """Converts Open-Meteo Celsius forecast to the unit used by the market."""
    if market_unit.upper() == "F":
        return celsius_to_fahrenheit(forecast_celsius)
    return forecast_celsius  # Already Celsius


if __name__ == "__main__":
    from datetime import timedelta

    print("Strategy module test - Dynamic probability floor")
    print("=" * 60)

    # Show floor values
    print("\nProbability floors by horizon:")
    for d in range(7):
        dt = (date.today() + timedelta(days=d)).isoformat()
        print(f"  Day {d} ({dt}): floor = {get_prob_floor(dt):.0%}")

    # Test scenarios with different horizons
    tests = [
        # (label, forecast, low, high, market_price, days_ahead, description)
        ("Same-day centered", 72.0, 71.0, 73.0, 0.10, 0,
         "Same-day, centered: prob ~38%, floor=30% => TRADE"),
        ("Same-day adjacent", 72.0, 69.0, 71.0, 0.05, 0,
         "Same-day, adjacent bucket: prob ~17%, floor=30% => NO TRADE"),
        ("1-day centered", 72.0, 71.0, 73.0, 0.10, 1,
         "1-day, centered: prob ~30%, floor=20% => TRADE"),
        ("1-day far bucket", 72.0, 66.0, 68.0, 0.02, 1,
         "1-day, far bucket: prob ~5%, floor=20% => NO TRADE (the trap)"),
        ("3-day centered", 72.0, 71.0, 73.0, 0.08, 3,
         "3-day, centered: prob ~16%, floor=15% => TRADE"),
        ("3-day adjacent", 72.0, 69.0, 71.0, 0.03, 3,
         "3-day, adjacent: prob ~14%, floor=15% => NO TRADE"),
        ("Open-ended high", 65.0, 60.0, None, 0.40, 1,
         "1-day, open-ended above: prob ~90%, floor=20% => TRADE"),
    ]

    for label, fc, lo, hi, mp, days, desc in tests:
        dt = (date.today() + timedelta(days=days)).isoformat()
        prob = forecast_probability(fc, lo, hi, unit="F", market_date=dt)
        edge = find_edge(prob, mp)
        floor = get_prob_floor(dt)
        trade = should_trade(edge, forecast_prob=prob, market_date=dt)
        size = kelly_position_size(bankroll=200.0, edge=edge, win_prob=prob, market_price=mp)

        print(f"\n--- {label} (day {days}) ---")
        print(f"  {desc}")
        print(f"  Forecast: {fc}F | Bucket: [{lo}, {hi}] | Date: {dt}")
        print(f"  Prob: {prob:.1%} | Market: {mp:.1%} | Edge: {edge:+.1%}")
        print(f"  Floor: {floor:.0%} | Floor pass: {prob >= floor} | Edge pass: {edge >= ENTRY_THRESHOLD}")
        print(f"  TRADE: {trade} | Size: ${size:.2f}")

    print("\n" + "=" * 60)
    print("Dynamic probability floor test complete.")
