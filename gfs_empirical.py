#!/usr/bin/env python3
"""
gfs_empirical.py — Probabilities straight from GFS ensemble member counts.
Zero calibration. Zero sigma. Zero t-distribution.
P(bucket) = count(GFS members predicting temp in bucket) / 30
"""
import json, sys, time, traceback
from datetime import date, timedelta
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from weather_v2 import CITIES

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

def fetch_gfs_ensemble(lat, lon, tz, days=7):
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone": tz,
        "models": "gfs_seamless",
        "forecast_days": min(days, 16),
    }
    try:
        resp = httpx.get(ENSEMBLE_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        members = []
        for i in range(1, 31):
            key = "temperature_2m_max_member{:02d}".format(i)
            if key in daily:
                members.append(daily[key])
        if not members or not dates:
            return None
        result = {}
        for d_idx, date_str in enumerate(dates):
            member_vals = [m_arr[d_idx] for m_arr in members 
                          if d_idx < len(m_arr) and m_arr[d_idx] is not None]
            if member_vals:
                result[date_str] = member_vals
        return result
    except Exception:
        return None

def empirical_probability(member_vals, bucket_low, bucket_high):
    if not member_vals:
        return 0.0
    in_bucket = sum(1 for v in member_vals if bucket_low <= v < bucket_high)
    return in_bucket / len(member_vals)

# Test cities
test_cities = ["Tokyo", "NYC", "London", "Lucknow", "Singapore", "Istanbul"]
tomorrow = (date.today() + timedelta(days=1)).isoformat()

print("=== GFS Ensemble Empirical vs Student's-t ===")
print("Date: {}".format(tomorrow))
print()

from strategy import forecast_probability

results = []
for city in test_cities:
    if city not in CITIES:
        print("{} not in CITIES".format(city))
        continue
    info = CITIES[city]
    
    ensemble = fetch_gfs_ensemble(info["lat"], info["lon"], info["tz"], days=3)
    if not ensemble:
        print("{}: ensemble fetch FAILED".format(city))
        continue
    if tomorrow not in ensemble:
        print("{}: date {} not in ensemble. Available: {}".format(
            city, tomorrow, list(ensemble.keys())))
        continue
    
    vals = ensemble[tomorrow]
    n = len(vals)
    mean_c = sum(vals) / n
    std_c = (sum((v - mean_c)**2 for v in vals) / (n - 1)) ** 0.5
    
    lo, hi = round(mean_c - 0.5, 1), round(mean_c + 0.5, 1)
    p_emp = empirical_probability(vals, lo, hi)
    
    try:
        p_t = forecast_probability(mean_c, lo, hi, unit="C", 
                                   market_date=tomorrow, city=city)
    except Exception as e:
        p_t = None
        print("  t-dist exception for {}: {}".format(city, e), file=sys.stderr)
    
    print("{}: n={} mean={:.1f}C std={:.2f}C p_emp={:.1%} p_t={}".format(
        city, n, mean_c, std_c, p_emp, 
        "{:.1%}".format(p_t) if p_t else "N/A"))
    
    results.append({"city": city, "n": n, "mean": mean_c, "std": std_c, 
                    "p_emp": p_emp, "p_t": p_t})
    time.sleep(0.4)

# Summary
if results:
    avg_std = sum(r["std"] for r in results) / len(results)
    print("\n=== Key Finding ===")
    print("Avg GFS ensemble std: {:.2f}C".format(avg_std))
    print("Our MIN_SAME_DAY_SIGMA: 0.44C")
    print("GFS uncertainty is {:.1f}x our floor".format(avg_std / 0.44))
    for r in results:
        print("  {}: GFS std={:.2f}C, p_emp={:.1%}".format(r["city"], r["std"], r["p_emp"]))
