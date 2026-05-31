"""
weather.py - Open-Meteo weather forecast fetcher
Fetches GFS/ECMWF ensemble max daily temperature for target cities.
No API key required (non-commercial use, CC BY 4.0).

Includes retry with exponential backoff and a secondary endpoint fallback
to handle transient SSL/rate-limit failures common on VPS IPs.
"""

import logging
import time
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Target cities with coordinates and timezone
# Covers all cities currently listed on Polymarket weather markets
CITIES = {
    # North America
    "NYC":           {"lat": 40.7128,  "lon": -74.0060,  "tz": "America/New_York"},
    "Chicago":       {"lat": 41.8781,  "lon": -87.6298,  "tz": "America/Chicago"},
    "LA":            {"lat": 34.0522,  "lon": -118.2437, "tz": "America/Los_Angeles"},
    "Miami":         {"lat": 25.7617,  "lon": -80.1918,  "tz": "America/New_York"},
    "Denver":        {"lat": 39.7392,  "lon": -104.9903, "tz": "America/Denver"},
    "DC":            {"lat": 38.9072,  "lon": -77.0369,  "tz": "America/New_York"},
    "San Francisco": {"lat": 37.7749,  "lon": -122.4194, "tz": "America/Los_Angeles"},
    "Houston":       {"lat": 29.7604,  "lon": -95.3698,  "tz": "America/Chicago"},
    "Austin":        {"lat": 30.2672,  "lon": -97.7431,  "tz": "America/Chicago"},
    "Dallas":        {"lat": 32.7767,  "lon": -96.7970,  "tz": "America/Chicago"},
    "Atlanta":       {"lat": 33.7490,  "lon": -84.3880,  "tz": "America/New_York"},
    "Seattle":       {"lat": 47.6062,  "lon": -122.3321, "tz": "America/Los_Angeles"},
    "Toronto":       {"lat": 43.6532,  "lon": -79.3832,  "tz": "America/Toronto"},
    "Mexico City":   {"lat": 19.4326,  "lon": -99.1332,  "tz": "America/Mexico_City"},
    # South America
    "Sao Paulo":     {"lat": -23.5505, "lon": -46.6333,  "tz": "America/Sao_Paulo"},
    "Buenos Aires":  {"lat": -34.6037, "lon": -58.3816,  "tz": "America/Argentina/Buenos_Aires"},
    # Europe
    "London":        {"lat": 51.5074,  "lon": -0.1278,   "tz": "Europe/London"},
    "Paris":         {"lat": 48.8566,  "lon": 2.3522,    "tz": "Europe/Paris"},
    "Madrid":        {"lat": 40.4168,  "lon": -3.7038,   "tz": "Europe/Madrid"},
    "Milan":         {"lat": 45.4642,  "lon": 9.1900,    "tz": "Europe/Rome"},
    "Munich":        {"lat": 48.1351,  "lon": 11.5820,   "tz": "Europe/Berlin"},
    "Warsaw":        {"lat": 52.2297,  "lon": 21.0122,   "tz": "Europe/Warsaw"},
    "Moscow":        {"lat": 55.7558,  "lon": 37.6173,   "tz": "Europe/Moscow"},
    "Istanbul":      {"lat": 41.0082,  "lon": 28.9784,   "tz": "Europe/Istanbul"},
    "Ankara":        {"lat": 39.9334,  "lon": 32.8597,   "tz": "Europe/Istanbul"},
    "Tel Aviv":      {"lat": 32.0853,  "lon": 34.7818,   "tz": "Asia/Jerusalem"},
    # Asia
    "Tokyo":         {"lat": 35.6762,  "lon": 139.6503,  "tz": "Asia/Tokyo"},
    "Seoul":         {"lat": 37.5665,  "lon": 126.9780,  "tz": "Asia/Seoul"},
    "Beijing":       {"lat": 39.9042,  "lon": 116.4074,  "tz": "Asia/Shanghai"},
    "Shanghai":      {"lat": 31.2304,  "lon": 121.4737,  "tz": "Asia/Shanghai"},
    "Chengdu":       {"lat": 30.5728,  "lon": 104.0668,  "tz": "Asia/Shanghai"},
    "Chongqing":     {"lat": 29.4316,  "lon": 106.9123,  "tz": "Asia/Shanghai"},
    "Wuhan":         {"lat": 30.5928,  "lon": 114.3055,  "tz": "Asia/Shanghai"},
    "Shenzhen":      {"lat": 22.5431,  "lon": 114.0579,  "tz": "Asia/Shanghai"},
    "Hong Kong":     {"lat": 22.3193,  "lon": 114.1694,  "tz": "Asia/Hong_Kong"},
    "Taipei":        {"lat": 25.0330,  "lon": 121.5654,  "tz": "Asia/Taipei"},
    "Singapore":     {"lat": 1.3521,   "lon": 103.8198,  "tz": "Asia/Singapore"},
    "Lucknow":       {"lat": 26.8467,  "lon": 80.9462,   "tz": "Asia/Kolkata"},
    # Oceania
    "Wellington":    {"lat": -41.2866, "lon": 174.7756,  "tz": "Pacific/Auckland"},
}

# Primary and fallback forecast endpoints.
OPEN_METEO_URLS = [
    "https://api.open-meteo.com/v1/forecast",
    "https://ensemble-api.open-meteo.com/v1/forecast",
]

# Ensemble API endpoint — provides per-member temperature forecasts for spread estimation
ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Retry config
MAX_RETRIES    = 3
BACKOFF_BASE_S = 1.0   # 1s, 2s, 4s


def _fetch_with_retry(params: dict) -> dict:
    """
    Attempts to fetch forecast data with retry + exponential backoff.
    Tries each endpoint in OPEN_METEO_URLS before giving up.
    Returns parsed JSON dict on success, raises on total failure.
    """
    last_exc = None

    for url in OPEN_METEO_URLS:
        for attempt in range(MAX_RETRIES):
            try:
                response = requests.get(url, params=params, timeout=10)
                response.raise_for_status()
                return response.json()
            except (requests.exceptions.SSLError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_exc = e
                wait = BACKOFF_BASE_S * (2 ** attempt)
                logger.debug(
                    f"Weather API attempt {attempt + 1}/{MAX_RETRIES} failed "
                    f"({url}): {type(e).__name__}. Retrying in {wait:.1f}s..."
                )
                time.sleep(wait)
            except requests.exceptions.HTTPError as e:
                # Non-transient HTTP errors (4xx) should not be retried
                if e.response is not None and e.response.status_code < 500:
                    raise
                last_exc = e
                wait = BACKOFF_BASE_S * (2 ** attempt)
                logger.debug(
                    f"Weather API attempt {attempt + 1}/{MAX_RETRIES} "
                    f"server error ({url}): {e}. Retrying in {wait:.1f}s..."
                )
                time.sleep(wait)

        logger.warning(f"All {MAX_RETRIES} retries exhausted for {url}")

    raise requests.exceptions.ConnectionError(
        f"Weather API unreachable after {MAX_RETRIES} retries "
        f"across {len(OPEN_METEO_URLS)} endpoints: {last_exc}"
    )


def get_forecast(city_name: str, days: int = 3) -> dict:
    """
    Returns a dict of {date_str: temp_celsius} for the next `days` days.
    Example: {'2026-03-24': 12.3, '2026-03-25': 9.8, '2026-03-26': 11.1}
    Raises ValueError if city not found. Raises requests.ConnectionError after
    all retries exhausted.
    """
    if city_name not in CITIES:
        raise ValueError(f"Unknown city: {city_name}. Available: {list(CITIES.keys())}")

    city = CITIES[city_name]
    params = {
        "latitude":         city["lat"],
        "longitude":        city["lon"],
        "daily":            "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone":         city["tz"],
        "forecast_days":    days,
    }

    data = _fetch_with_retry(params)

    dates = data["daily"]["time"]
    temps = data["daily"]["temperature_2m_max"]
    return dict(zip(dates, temps))


def get_forecast_fahrenheit(city_name: str, days: int = 3) -> dict:
    """Same as get_forecast but returns temperatures in Fahrenheit."""
    celsius_data = get_forecast(city_name, days)
    return {date: (temp * 9 / 5) + 32 for date, temp in celsius_data.items()}


def get_ensemble_spread(city_name: str, target_date: str) -> float | None:
    """
    Fetches GFS ensemble member daily max temperatures for a city/date and
    returns the inter-member standard deviation (in Celsius) as a
    physically-grounded sigma for that specific forecast.

    This is more accurate than the static SIGMA_BY_HORIZON_F table because
    it reflects actual model spread — low spread means high confidence that
    day; high spread means increased uncertainty.

    Returns sigma in Celsius, or None on API failure (caller falls back to
    the static horizon table).  Convert to Fahrenheit with × 1.8.
    """
    if city_name not in CITIES:
        return None

    city = CITIES[city_name]

    try:
        today = datetime.now(timezone.utc).date()
        import datetime as _dt
        target = _dt.date.fromisoformat(target_date)
        days_ahead = (target - today).days + 1  # +1 to include target_date itself
    except (ValueError, TypeError):
        return None

    if days_ahead < 1 or days_ahead > 16:
        return None  # Ensemble API max horizon is 16 days

    params = {
        "latitude":         city["lat"],
        "longitude":        city["lon"],
        "daily":            "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone":         city["tz"],
        "forecast_days":    days_ahead,
        "models":           "gfs_seamless",  # 30-member ensemble, up to 16 days
    }

    try:
        response = requests.get(ENSEMBLE_API_URL, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.debug(f"Ensemble API failed for {city_name} {target_date}: {e}")
        return None

    daily = data.get("daily", {})
    times = daily.get("time", [])

    if target_date not in times:
        return None

    date_idx = times.index(target_date)

    member_values = []
    for key, values in daily.items():
        if (key.startswith("temperature_2m_max_member")
                and values and date_idx < len(values)):
            val = values[date_idx]
            if val is not None:
                member_values.append(float(val))

    if len(member_values) < 5:
        logger.debug(
            f"Too few ensemble members ({len(member_values)}) for {city_name} {target_date}"
        )
        return None

    mean = sum(member_values) / len(member_values)
    variance = sum((v - mean) ** 2 for v in member_values) / (len(member_values) - 1)
    spread = variance ** 0.5
    logger.debug(
        f"Ensemble spread for {city_name} {target_date}: {spread:.2f}°C "
        f"({len(member_values)} members)"
    )
    return spread


def celsius_to_fahrenheit(c: float) -> float:
    return (c * 9 / 5) + 32


def fahrenheit_to_celsius(f: float) -> float:
    return (f - 32) * 5 / 9


if __name__ == "__main__":
    for city in CITIES:
        print(f"\n{city}:")
        try:
            forecast_c = get_forecast(city)
            forecast_f = get_forecast_fahrenheit(city)
            for date in forecast_c:
                print(f"  {date}: {forecast_c[date]:.1f}C / {forecast_f[date]:.1f}F")
        except Exception as e:
            print(f"  ERROR: {e}")
