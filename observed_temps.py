"""
observed_temps.py - Fetches actual observed temperatures and resolves positions.

Two main capabilities:
1. Historical daily max: for positions where market_date has passed, fetch the
   actual recorded high temperature and determine win/loss.
2. Intra-day current max: for same-day positions, fetch hourly observations to
   get the running daily high so far (used by position_monitor.py for exits).

Uses Open-Meteo Archive API (free, no key) for historical data and
Open-Meteo Forecast API hourly endpoint for same-day observations.

Open-Meteo historical data uses ERA5 reanalysis blended with station data.
This may differ from the exact station Polymarket uses for resolution by
1-2 degrees. Positions where the actual temp lands within 1 degree of a
bucket boundary are flagged for manual review.
"""

import logging
import time
import requests
from datetime import datetime, date, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Reuse city coordinates from weather.py
from weather import CITIES, celsius_to_fahrenheit

# Open-Meteo Archive API (free, no key, historical observations)
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Open-Meteo Forecast API (hourly observations for today)
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Retry config
MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0


def _fetch_with_retry(url: str, params: dict) -> dict:
    """Fetches JSON from Open-Meteo with retry and exponential backoff."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.SSLError) as e:
            last_exc = e
            wait = BACKOFF_BASE_S * (2 ** attempt)
            logger.debug(f"Archive API attempt {attempt + 1} failed: {e}. Retry in {wait:.1f}s")
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code < 500:
                raise
            last_exc = e
            wait = BACKOFF_BASE_S * (2 ** attempt)
            time.sleep(wait)

    raise requests.exceptions.ConnectionError(
        f"Open-Meteo API unreachable after {MAX_RETRIES} retries: {last_exc}"
    )


def get_historical_max_temp(city: str, target_date: str) -> Optional[dict]:
    """
    Fetches the actual recorded daily max temperature for a past date.

    Args:
        city: City key matching weather.py CITIES dict
        target_date: ISO date string (YYYY-MM-DD), must be in the past

    Returns:
        dict with keys:
            temp_c: float (Celsius)
            temp_f: float (Fahrenheit)
            source: str ("open-meteo-archive")
            date: str
        or None on failure
    """
    if city not in CITIES:
        logger.warning(f"Unknown city for historical temp: {city}")
        return None

    city_info = CITIES[city]

    params = {
        "latitude": city_info["lat"],
        "longitude": city_info["lon"],
        "start_date": target_date,
        "end_date": target_date,
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone": city_info["tz"],
    }

    try:
        data = _fetch_with_retry(ARCHIVE_URL, params)
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        dates = data.get("daily", {}).get("time", [])

        if not temps or temps[0] is None:
            logger.warning(f"No historical temp data for {city} on {target_date}")
            return None

        temp_c = temps[0]
        return {
            "temp_c": temp_c,
            "temp_f": celsius_to_fahrenheit(temp_c),
            "source": "open-meteo-archive",
            "date": dates[0] if dates else target_date,
        }

    except Exception as e:
        logger.error(f"Failed to fetch historical temp for {city} {target_date}: {e}")
        return None


def get_current_day_max(city: str) -> Optional[dict]:
    """
    Fetches the current running daily max temperature for today.
    Uses hourly observations from the forecast API (which includes
    past hours for today).

    Returns:
        dict with keys:
            temp_c: float (highest hourly temp so far today)
            temp_f: float
            source: str ("open-meteo-hourly")
            hour_count: int (number of hours of data)
            last_hour: str (time of last observation)
        or None on failure
    """
    if city not in CITIES:
        logger.warning(f"Unknown city for current max: {city}")
        return None

    city_info = CITIES[city]
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    params = {
        "latitude": city_info["lat"],
        "longitude": city_info["lon"],
        "hourly": "temperature_2m",
        "temperature_unit": "celsius",
        "timezone": city_info["tz"],
        "start_date": today_str,
        "end_date": today_str,
        "past_hours": 24,  # Include all hours from today so far
    }

    try:
        data = _fetch_with_retry(FORECAST_URL, params)
        temps = data.get("hourly", {}).get("temperature_2m", [])
        times = data.get("hourly", {}).get("time", [])

        if not temps:
            logger.warning(f"No hourly data for {city} today")
            return None

        # Filter out None values and future hours (which may be forecasts)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        valid_temps = []
        last_time = None
        for t, temp in zip(times, temps):
            if temp is not None and t <= now_str:
                valid_temps.append(temp)
                last_time = t

        if not valid_temps:
            return None

        max_temp_c = max(valid_temps)
        return {
            "temp_c": max_temp_c,
            "temp_f": celsius_to_fahrenheit(max_temp_c),
            "source": "open-meteo-hourly",
            "hour_count": len(valid_temps),
            "last_hour": last_time,
        }

    except Exception as e:
        logger.error(f"Failed to fetch current day max for {city}: {e}")
        return None


def check_bucket_result(
    actual_temp: float,
    bucket_low: Optional[float],
    bucket_high: Optional[float],
) -> dict:
    """
    Determines if the actual temperature falls within the bucket.

    Returns:
        dict with keys:
            won: bool
            margin: float (distance from nearest bucket boundary, negative = inside)
            boundary_flag: bool (True if within 1 degree of boundary)
    """
    in_bucket = True

    if bucket_low is not None and actual_temp < bucket_low:
        in_bucket = False
    if bucket_high is not None and actual_temp >= bucket_high:
        in_bucket = False

    # Calculate distance from nearest boundary
    distances = []
    if bucket_low is not None:
        distances.append(actual_temp - bucket_low)
    if bucket_high is not None:
        distances.append(bucket_high - actual_temp)

    margin = min(distances) if distances else 999.0
    boundary_flag = abs(margin) < 1.0

    return {
        "won": in_bucket,
        "margin": round(margin, 2),
        "boundary_flag": boundary_flag,
    }


def resolve_positions(positions: list[dict]) -> list[dict]:
    """
    Takes a list of open positions with past market_dates and resolves them.
    Fetches actual temps and determines win/loss for each.

    Args:
        positions: list of position dicts from positions.py

    Returns:
        list of result dicts with keys:
            position_id, city, market_date, actual_temp_c, actual_temp_f,
            won, margin, boundary_flag, source
    """
    results = []
    # Cache to avoid duplicate API calls for same city/date
    temp_cache = {}

    for pos in positions:
        city = pos["city"]
        market_date = pos["market_date"]
        cache_key = (city, market_date)

        if cache_key not in temp_cache:
            temp_data = get_historical_max_temp(city, market_date)
            temp_cache[cache_key] = temp_data

        temp_data = temp_cache[cache_key]
        if temp_data is None:
            logger.warning(
                f"Cannot resolve position {pos['id']}: no temp data for "
                f"{city} {market_date}"
            )
            continue

        # Use the correct unit for bucket comparison
        unit = pos.get("unit", "F")
        if unit.upper() == "F":
            actual_temp = temp_data["temp_f"]
        else:
            actual_temp = temp_data["temp_c"]

        bucket_result = check_bucket_result(
            actual_temp, pos["bucket_low"], pos["bucket_high"]
        )

        results.append({
            "position_id": pos["id"],
            "city": city,
            "market_date": market_date,
            "actual_temp_c": temp_data["temp_c"],
            "actual_temp_f": temp_data["temp_f"],
            "actual_temp_unit": actual_temp,
            "won": bucket_result["won"],
            "margin": bucket_result["margin"],
            "boundary_flag": bucket_result["boundary_flag"],
            "source": temp_data["source"],
            "bucket_low": pos["bucket_low"],
            "bucket_high": pos["bucket_high"],
            "unit": unit,
        })

        status_str = "WON" if bucket_result["won"] else "LOST"
        boundary_str = " [BOUNDARY - REVIEW]" if bucket_result["boundary_flag"] else ""
        logger.info(
            f"Resolution: {city} {market_date} | actual={actual_temp:.1f}{unit} | "
            f"bucket=[{pos['bucket_low']},{pos['bucket_high']}]{unit} | "
            f"{status_str} (margin={bucket_result['margin']:.1f}){boundary_str}"
        )

    return results


if __name__ == "__main__":
    import sys

    print("Observed Temps Module Test")
    print("=" * 50)

    # Test historical max
    print("\n--- Historical max for NYC yesterday ---")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    result = get_historical_max_temp("NYC", yesterday)
    if result:
        print(f"  Date: {result['date']}")
        print(f"  Max: {result['temp_c']:.1f}C / {result['temp_f']:.1f}F")
        print(f"  Source: {result['source']}")
    else:
        print("  No data available (may be too recent for archive)")

    # Test current day max
    print("\n--- Current day max for NYC ---")
    current = get_current_day_max("NYC")
    if current:
        print(f"  Current max: {current['temp_c']:.1f}C / {current['temp_f']:.1f}F")
        print(f"  Hours observed: {current['hour_count']}")
        print(f"  Last observation: {current['last_hour']}")
        print(f"  Source: {current['source']}")
    else:
        print("  No data available")

    # Test bucket checking
    print("\n--- Bucket check examples ---")
    tests = [
        (72.5, 71.0, 73.0, "72.5 in [71,73]"),
        (70.8, 71.0, 73.0, "70.8 in [71,73]"),
        (73.2, 71.0, 73.0, "73.2 in [71,73]"),
        (72.5, 71.0, 73.0, "72.5 in [71,73] (boundary check)"),
        (85.0, 84.0, None, "85.0 in [84,+inf]"),
        (83.5, 84.0, None, "83.5 in [84,+inf]"),
    ]
    for actual, low, high, desc in tests:
        r = check_bucket_result(actual, low, high)
        flag = " [BOUNDARY]" if r["boundary_flag"] else ""
        print(f"  {desc}: {'WON' if r['won'] else 'LOST'} | margin={r['margin']}{flag}")
