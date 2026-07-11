#!/usr/bin/env python3
"""
sigma_backtest.py — Empirically calibrate CITY_SIGMA_MULTIPLIER.

Compares Open-Meteo ensemble forecast (GFS + ECMWF + GraphCast) against
actual observed max temperatures from the Archive API for the past 30 days.

Output: per-city RMSE, mean bias, and recommended sigma multiplier for strategy.py.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from weather import CITIES

# ── Config ────────────────────────────────────────────────────────
DAYS_BACK = 30
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_DELAY = 0.3  # seconds between API calls (rate limit safety)

# ── Fetch helpers ─────────────────────────────────────────────────
def fetch_json(url: str, params: dict) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    full_url = f"{url}?{qs}"
    req = urllib.request.Request(full_url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

def get_actual_max(city_info: dict, target_date: str) -> float | None:
    """Fetch actual observed max temp from Open-Meteo Archive (ERA5)."""
    params = {
        "latitude": city_info["lat"],
        "longitude": city_info["lon"],
        "start_date": target_date,
        "end_date": target_date,
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone": "UTC",
    }
    try:
        data = fetch_json(ARCHIVE_URL, params)
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        if temps and temps[0] is not None:
            return float(temps[0])
    except Exception as e:
        print(f"  Archive fetch error: {e}", file=sys.stderr)
    return None

def get_forecast_max(city_info: dict, target_date: str) -> float | None:
    """Fetch forecast max temp for a specific date."""
    params = {
        "latitude": city_info["lat"],
        "longitude": city_info["lon"],
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone": "UTC",
        "start_date": target_date,
        "end_date": target_date,
    }
    try:
        data = fetch_json(FORECAST_URL, params)
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        if temps and temps[0] is not None:
            return float(temps[0])
    except Exception as e:
        print(f"  Forecast fetch error: {e}", file=sys.stderr)
    return None


# ── Main ──────────────────────────────────────────────────────────
def main():
    end_date = date.today() - timedelta(days=1)  # yesterday
    start_date = end_date - timedelta(days=DAYS_BACK - 1)

    print(f"=== Sigma Backtest: {start_date} → {end_date} ({DAYS_BACK} days) ===\n")

    results: dict[str, dict] = {}

    for city, info in sorted(CITIES.items()):
        errors = []
        actuals = []
        forecasts = []
        n_missed = 0

        current = start_date
        while current <= end_date:
            date_str = current.isoformat()
            time.sleep(REQUEST_DELAY)

            actual = get_actual_max(info, date_str)
            forecast = get_forecast_max(info, date_str)

            if actual is not None and forecast is not None:
                error = forecast - actual  # positive = forecast too high
                errors.append(error)
                actuals.append(actual)
                forecasts.append(forecast)
            else:
                n_missed += 1

            current += timedelta(days=1)

        if len(errors) < 5:
            print(f"  {city:15s}: insufficient data ({len(errors)} points, {n_missed} missed)")
            continue

        n = len(errors)
        mean_error = sum(errors) / n
        mae = sum(abs(e) for e in errors) / n
        rmse = (sum(e**2 for e in errors) / n) ** 0.5
        std_err = (sum((e - mean_error)**2 for e in errors) / (n - 1)) ** 0.5

        # Current default same-day sigma in Celsius: 2.5°F / 1.8 = 1.39°C
        # Multiplier needed to match observed RMSE
        base_sigma_c = 1.39  # 2.5°F in Celsius
        recommended_mult = rmse / base_sigma_c

        results[city] = {
            "n": n,
            "mean_bias": round(mean_error, 2),
            "mae": round(mae, 2),
            "rmse": round(rmse, 2),
            "std": round(std_err, 2),
            "recommended_mult": round(recommended_mult, 2),
            "forecast_mean": round(sum(forecasts)/n, 1),
            "actual_mean": round(sum(actuals)/n, 1),
        }

    # ── Print results ──────────────────────────────────────────────
    print(f"{'City':15s} {'n':>3s} {'Bias(C)':>8s} {'MAE':>6s} {'RMSE':>6s} {'Mult':>6s} {'FcMean':>7s} {'ActMean':>7s}")
    print("-" * 72)
    for city, r in sorted(results.items()):
        bias_str = f"{r['mean_bias']:+.2f}"
        print(f"{city:15s} {r['n']:3d} {bias_str:>8s} {r['mae']:6.2f} {r['rmse']:6.2f} "
              f"{r['recommended_mult']:6.2f} {r['forecast_mean']:7.1f} {r['actual_mean']:7.1f}")

    # ── Summary stats ──────────────────────────────────────────────
    all_mult = [r["recommended_mult"] for r in results.values()]
    all_rmse = [r["rmse"] for r in results.values()]
    all_bias = [r["mean_bias"] for r in results.values()]

    print(f"\n--- Summary ({len(results)} cities) ---")
    print(f"  RMSE range:    {min(all_rmse):.2f} – {max(all_rmse):.2f}°C  (mean={sum(all_rmse)/len(all_rmse):.2f})")
    print(f"  Mult range:    {min(all_mult):.2f} – {max(all_mult):.2f}x  (mean={sum(all_mult)/len(all_mult):.2f})")
    print(f"  Bias range:    {min(all_bias):+.2f} – {max(all_bias):+.2f}°C  (mean={sum(all_bias)/len(all_bias):+.2f})")

    neg_bias = [b for b in all_bias if b < 0]
    pos_bias = [b for b in all_bias if b > 0]
    print(f"  Cold bias cities: {len(neg_bias)} (forecast too low — the May 2 problem)")
    print(f"  Warm bias cities: {len(pos_bias)} (forecast too high)")

    # ── Generate CITY_SIGMA_MULTIPLIER snippet ─────────────────────
    print("\n# --- Recommended CITY_SIGMA_MULTIPLIER (paste into strategy.py) ---")
    print("CITY_SIGMA_MULTIPLIER = {")
    for city, r in sorted(results.items()):
        mult = max(0.8, min(3.0, r["recommended_mult"]))  # Clamp to safe range
        mult = round(mult * 2) / 2  # Round to nearest 0.5
        comment = f"rmse={r['rmse']:.1f}C, n={r['n']}"
        print(f'    "{city}": {mult:.1f},  # {comment}')
    print("}")


if __name__ == "__main__":
    main()
