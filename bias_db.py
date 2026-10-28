#!/usr/bin/env python3
"""
bias_db.py — Per-city GFS bias database for empirical probability correction.

Stores: (city, month, mean_bias_c, n_samples)
Where mean_bias_c = avg(GFS_forecast - METAR_actual) in Celsius.
Positive = GFS over-forecasts (too hot). Negative = GFS under-forecasts.

Used by strategy.py to shift GFS ensemble members before bucket counting.
"""

import sqlite3, json, time, sys, os
from datetime import date, timedelta
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from weather_v2 import CITIES

DB_PATH = Path(os.environ.get("BIAS_DB_PATH", "/root/weathercore/bias/bias.db"))
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS city_bias (
            city TEXT NOT NULL,
            month INTEGER NOT NULL,    -- 1-12
            mean_bias_c REAL NOT NULL,  -- avg(forecast - actual) in Celsius
            std_bias_c REAL,            -- std dev of bias
            n_samples INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (city, month)
        )
    """)
    conn.commit()
    return conn

def fetch_actual(city_info: dict, target_date: str) -> float | None:
    """Fetch actual max temp from Open-Meteo Archive (ERA5)."""
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
        resp = httpx.get(ARCHIVE_URL, params=params, timeout=15)
        data = resp.json()
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        if temps and temps[0] is not None:
            return float(temps[0])
    except Exception:
        pass
    return None

def fetch_forecast(city_info: dict, target_date: str) -> float | None:
    """Fetch same-day forecast max temp from Open-Meteo."""
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
        resp = httpx.get(FORECAST_URL, params=params, timeout=15)
        data = resp.json()
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        if temps and temps[0] is not None:
            return float(temps[0])
    except Exception:
        pass
    return None

def build_bias_db(days_back: int = 30):
    """Build/update per-city bias database from historical data."""
    conn = init_db()
    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days_back - 1)

    print(f"Building bias DB: {start_date} → {end_date} ({days_back} days)")
    print()

    for city, info in sorted(CITIES.items()):
        biases = []
        current = start_date
        while current <= end_date:
            date_str = current.isoformat()
            actual = fetch_actual(info, date_str)
            forecast = fetch_forecast(info, date_str)
            if actual is not None and forecast is not None:
                biases.append(forecast - actual)
            current += timedelta(days=1)
            time.sleep(0.15)  # Rate limit safety

        if len(biases) < 5:
            print(f"  {city:15s}: insufficient data ({len(biases)} samples)")
            continue

        n = len(biases)
        mean_bias = sum(biases) / n
        std_bias = (sum((b - mean_bias)**2 for b in biases) / (n - 1)) ** 0.5
        month = date.today().month

        conn.execute("""
            INSERT OR REPLACE INTO city_bias (city, month, mean_bias_c, std_bias_c, n_samples, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (city, month, round(mean_bias, 2), round(std_bias, 2), n, date.today().isoformat()))
        conn.commit()

        direction = "HOT" if mean_bias > 0 else "COLD" if mean_bias < 0 else "NEUTRAL"
        print(f"  {city:15s}: bias={mean_bias:+.2f}°C [{direction}] std={std_bias:.2f} n={n}")

    print(f"\nBias DB saved to {DB_PATH}")
    conn.close()

def get_city_bias(city: str, month: int = None) -> float:
    """Get stored GFS bias for a city — monthly with all-time fallback.

    NOAA EMC verification (Colle et al. 2020, Baño-Medina et al. 2021)
    shows GFS 2m temperature bias swings 2-4°C between seasons.
    Monthly corrections prevent cross-season contamination.

    Query order:
      1. Per-month bias (current month, ≥5 samples) — seasonal precision
      2. All-time rolling 30-day bias — stable fallback
    Returns 0.0 if no data available.
    """
    from weather_v2 import _CITY_TO_ICAO
    icao = _CITY_TO_ICAO.get(city)
    if not icao:
        return 0.0

    if month is None:
        month = date.today().month

    conn = sqlite3.connect(str(DB_PATH))
    try:
        # 1. Per-month bias (seasonal, requires ≥5 samples)
        row = conn.execute(
            "SELECT bias_c FROM bias_corrections_monthly "
            "WHERE icao = ? AND month = ? AND n_samples >= 5",
            (icao, month)
        ).fetchone()
        if row:
            return row[0]

        # 2. Fallback: all-time rolling 30-day bias
        row = conn.execute(
            "SELECT bias_c FROM bias_corrections WHERE icao = ?",
            (icao,)
        ).fetchone()
        return row[0] if row else 0.0
    except sqlite3.OperationalError:
        return 0.0
    finally:
        conn.close()

def apply_bias_to_ensemble(gfs_members: list[float], bias_c: float) -> list[float]:
    """Shift GFS ensemble members by -bias to correct systematic error.

    If GFS over-forecasts by 1.5°C (bias=+1.5), subtract 1.5 from all members.
    Capped at ±3°C to prevent wild swings from station-noise or thin samples.
    """
    if abs(bias_c) < 0.2:
        return gfs_members  # Negligible bias
    capped = max(-3.0, min(3.0, bias_c))
    return [v + capped for v in gfs_members]

if __name__ == "__main__":
    import os
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    build_bias_db(days_back=days)
