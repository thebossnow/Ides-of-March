"""
calibration.py - Empirical calibration for the forecast probability model.

Reads resolved positions from the DB, computes per-city bias (systematic
difference between Open-Meteo gridded forecast and actual temperature),
and exposes bias offsets for use in forecast_probability().

Pipeline:
  1. After N >= 10 resolved positions for a city, compute mean forecast
     residual:  residual = forecast_temp - actual_temp  (in Celsius)
     Positive residual = Open-Meteo runs warm (forecast too high)
     -> apply negative bias offset to mu to correct
  2. The city_bias passed to forecast_probability() is in the market's unit.
     Conversion is handled per-call in bot.py.

Also tracks win rate and Brier score for model health monitoring.

Usage:
    from calibration import get_city_bias, run_calibration, get_calibration_summary
    run_calibration()               # load from DB
    bias = get_city_bias("NYC")     # e.g. +1.2°C = model runs 1.2°C warm for NYC
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Minimum resolved positions before applying a bias correction.
# Too few samples → bias estimate is noise.
MIN_SAMPLES_FOR_BIAS = 10

# In-memory cache: {city: calibration_dict}
_cache: dict = {}
_last_calibrated: str = ""


def run_calibration() -> dict:
    """
    Reads all resolved positions with actual_temp and forecast_temp_c,
    computes per-city bias and win rate, and updates the in-memory cache.

    Returns dict of results keyed by city.
    """
    global _cache, _last_calibrated

    from positions import get_calibration_data
    data = get_calibration_data()

    if not data:
        logger.info("Calibration: no resolved positions with temp data yet")
        return {}

    city_buckets: dict = {}

    for row in data:
        city = row["city"]
        if city not in city_buckets:
            city_buckets[city] = {
                "residuals_c": [],  # forecast_temp_c - actual_temp_c
                "outcomes":    [],  # (forecast_prob, won)
            }

        won = row["status"] in ("resolved_won", "redeemed")
        fp = row.get("forecast_prob") or 0.0
        city_buckets[city]["outcomes"].append((fp, won))

        # Compute temperature residual when both forecast and actual are available
        fc = row.get("forecast_temp_c")
        actual = row.get("actual_temp")
        unit = row.get("unit", "F")
        if fc is not None and actual is not None:
            # Convert actual_temp to Celsius for consistent residual calculation
            if unit.upper() == "F":
                actual_c = (actual - 32.0) * 5.0 / 9.0
            else:
                actual_c = actual
            city_buckets[city]["residuals_c"].append(fc - actual_c)

    results = {}

    for city, d in city_buckets.items():
        outcomes = d["outcomes"]
        n = len(outcomes)
        if n == 0:
            continue

        actual_win_rate = sum(won for _, won in outcomes) / n
        avg_fp = sum(fp for fp, _ in outcomes) / n

        # Brier score: mean squared error of probability forecasts
        # Lower = better calibrated. Perfect = 0, random = 0.25
        brier = sum((fp - int(won)) ** 2 for fp, won in outcomes) / n

        # Calibration gap: positive means model is overconfident
        calib_gap = avg_fp - actual_win_rate

        residuals = d["residuals_c"]
        n_resid = len(residuals)
        bias_c = sum(residuals) / n_resid if n_resid >= MIN_SAMPLES_FOR_BIAS else 0.0

        results[city] = {
            "n_samples":       n,
            "n_with_forecast": n_resid,
            "win_rate":        round(actual_win_rate, 3),
            "avg_forecast_prob": round(avg_fp, 3),
            "calibration_gap": round(calib_gap, 3),
            "brier_score":     round(brier, 4),
            "bias_c":          round(bias_c, 2),  # Celsius; × 1.8 for Fahrenheit
        }

        logger.info(
            f"Calibration [{city}]: n={n} | win={actual_win_rate:.1%} | "
            f"avg_prob={avg_fp:.1%} | gap={calib_gap:+.1%} | "
            f"brier={brier:.3f} | bias={bias_c:+.2f}°C"
            + (f" (from {n_resid} residuals)" if n_resid > 0 else " (no forecast_temp yet)")
        )

    _cache = results
    _last_calibrated = datetime.now(timezone.utc).isoformat()
    return results


def get_city_bias(city: str, unit: str = "F") -> float:
    """
    Returns the per-city temperature bias offset to apply to the forecast center.

    A positive bias means Open-Meteo forecasts run *warm* for this city —
    the returned value (negative) should be added to mu to correct it.

    Returns 0.0 if fewer than MIN_SAMPLES_FOR_BIAS resolved positions exist.

    Args:
        city: city key matching weather.py CITIES
        unit: "F" or "C" — determines return unit

    Returns:
        bias offset in requested unit (negative = model runs warm, forecast
        should be shifted cooler)
    """
    entry = _cache.get(city)
    if entry is None:
        return 0.0
    if entry.get("n_with_forecast", 0) < MIN_SAMPLES_FOR_BIAS:
        return 0.0

    bias_c = entry.get("bias_c", 0.0)
    # bias_c > 0: forecast runs warm → apply negative correction
    # We return the raw bias so caller can decide how to apply it
    if unit.upper() == "C":
        return bias_c
    return bias_c * 1.8  # Convert °C difference to °F difference


def get_calibration_summary() -> dict:
    """Returns the cached calibration results dict."""
    return dict(_cache)


def print_calibration_report() -> None:
    """Prints a human-readable calibration summary to stdout."""
    if not _cache:
        print("No calibration data available. Run run_calibration() first.")
        return

    print(f"\nCalibration Report (as of {_last_calibrated or 'unknown'})")
    print("=" * 72)
    print(f"{'City':<18} {'N':>5} {'WinRate':>8} {'AvgProb':>8} {'Gap':>7} "
          f"{'Brier':>7} {'Bias_C':>8}")
    print("-" * 72)

    for city, d in sorted(_cache.items()):
        n = d["n_samples"]
        wr = d["win_rate"]
        ap = d["avg_forecast_prob"]
        gap = d["calibration_gap"]
        b = d["brier_score"]
        bias = d["bias_c"]
        print(f"{city:<18} {n:>5} {wr:>8.1%} {ap:>8.1%} {gap:>+7.1%} "
              f"{b:>7.3f} {bias:>+8.2f}")

    print("=" * 72)
    print("Gap = avg_forecast_prob - win_rate (positive = overconfident)")
    print("Bias_C = mean(forecast_temp_c - actual_temp_c) (positive = runs warm)")


if __name__ == "__main__":
    run_calibration()
    print_calibration_report()
