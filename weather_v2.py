"""weather_v2.py — Robust, cached, rate-limit-resistant weather fetching.
Single source of truth for all weather data used by weatherbot and sniperweatherbot.

Ensemble approach (3 Open-Meteo models queried in parallel):
  1. Open-Meteo GFS        (/v1/forecast — primary NWP model)
  2. Open-Meteo ECMWF IFS  (/v1/ecmwf?models=ecmwf_ifs025)
  3. Open-Meteo GraphCast  (/v1/gfs?models=gfs_graphcast025)

  All 3 models are queried every time. Per-model forecasts are returned
  so that strategy.py can compute separate probabilities per model,
  take the 2 highest, and average them.

  If fewer than 3 models return data, a warning is logged but processing
  continues with whatever is available.

  Fallback (used ONLY when all 3 Open-Meteo models fail):
  4. Tomorrow.io           (proprietary blend, requires TOMORROW_IO_API_KEY)
  5. wttr.in               (last resort, no key required)

Caching (2 layers, 3-hour TTL):
  - In-memory TTLCache  (zero latency on hit, lost on restart)
  - Disk JSON cache     (survives restarts, path via WEATHER_CACHE_PATH env var)

Paid Open-Meteo API:
  Set OPENMETEO_API_KEY in .env to route requests to customer-api.open-meteo.com
  (higher quota + SLA). Without key, falls back to the free api.open-meteo.com tier.
"""

import json
import time
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import httpx
from cachetools import TTLCache

# Station coord migration (see station_migration/)
import json as _json_station
_STATIONS_FILE = os.environ.get(
    "WEATHER_STATIONS_FILE",
    "/root/weathercore/station_migration/stations.json",
)
try:
    with open(_STATIONS_FILE) as _f:
        STATIONS: Dict[str, dict] = _json_station.load(_f)
except Exception as _e:
    STATIONS = {}
    # Keep logger uninitialised safe — will log on first use
    _STATIONS_LOAD_ERROR = str(_e)
else:
    _STATIONS_LOAD_ERROR = None

# Station coordinates: enabled by default. All city lookups route through
# airport ICAO stations for precise forecast alignment with METAR observations.
# Set STATION_COORDS=0 to revert to city-center coordinates.
_STATION_COORDS_ENABLED = os.environ.get("STATION_COORDS", "1") == "1"

# Feature flag: set BIAS_CORRECTION=1 to add station-level METAR/ERA5 bias to forecasts.
_BIAS_CORRECTION_ENABLED = os.environ.get("BIAS_CORRECTION", "0") == "1"
_BIAS_DB_PATH = os.environ.get("BIAS_DB_PATH", "/root/weathercore/bias/bias.db")
_BIAS_MIN_SAMPLES = int(os.environ.get("BIAS_MIN_SAMPLES", "3"))
_BIAS_CACHE: Optional[Dict[str, float]] = None

# Feature flag: set TAF_FUSION=1 to blend TAF TX values for ≤30h horizons.
_TAF_FUSION_ENABLED = os.environ.get("TAF_FUSION", "0") == "1"
# Weight placed on the TAF signal when available (rest goes to model+bias).
_TAF_WEIGHT = float(os.environ.get("TAF_WEIGHT", "0.7"))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CACHE_TTL_HOURS = 3
CACHE_FILE = Path(os.environ.get(
    "WEATHER_CACHE_PATH",
    str(Path.home() / "weatherbot" / "weather_cache.json"),
))
MAX_RETRIES = 3
BASE_BACKOFF = 8.0  # seconds; doubles on each retry for 429s

OPENMETEO_API_KEY: str = os.environ.get("OPENMETEO_API_KEY", "")
_OM_HOST = (
    "https://customer-api.open-meteo.com"
    if OPENMETEO_API_KEY
    else "https://api.open-meteo.com"
)

TOMORROW_IO_API_KEY: str = os.environ.get("TOMORROW_IO_API_KEY", "")

# ---------------------------------------------------------------------------
# City database — single source of truth for all 51 tracked cities
# Covers every city currently listed on Polymarket weather markets.
# ---------------------------------------------------------------------------
CITIES: Dict[str, dict] = {
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
    "Amsterdam":     {"lat": 52.3676,  "lon": 4.9041,    "tz": "Europe/Amsterdam"},
    "Helsinki":      {"lat": 60.1699,  "lon": 24.9384,   "tz": "Europe/Helsinki"},
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
    "Kuala Lumpur":  {"lat": 3.1390,   "lon": 101.6869,  "tz": "Asia/Kuala_Lumpur"},
    "Jakarta":       {"lat": -6.2088,  "lon": 106.8456,  "tz": "Asia/Jakarta"},
    "Busan":         {"lat": 35.1796,  "lon": 129.0756,  "tz": "Asia/Seoul"},
    "Guangzhou":     {"lat": 23.1291,  "lon": 113.2644,  "tz": "Asia/Shanghai"},
    "Manila":        {"lat": 14.5995,  "lon": 120.9842,  "tz": "Asia/Manila"},
    # Middle East
    "Jeddah":        {"lat": 21.4858,  "lon": 39.1925,   "tz": "Asia/Riyadh"},
    "Karachi":       {"lat": 24.8607,  "lon": 67.0011,   "tz": "Asia/Karachi"},
    # Africa
    "Lagos":         {"lat": 6.5244,   "lon": 3.3792,    "tz": "Africa/Lagos"},
    "Cape Town":     {"lat": -33.9249, "lon": 18.4241,   "tz": "Africa/Johannesburg"},
    # Oceania / Americas (other)
    "Wellington":    {"lat": -41.2866, "lon": 174.7756,  "tz": "Pacific/Auckland"},
    "Panama City":   {"lat": 8.9824,   "lon": -79.5199,  "tz": "America/Panama"},
}

# ---------------------------------------------------------------------------
# GFS Ensemble — 30-member physics-based uncertainty (no calibration needed)
# Endpoint: ensemble-api.open-meteo.com/v1/ensemble?models=gfs_seamless
# Returns per-date arrays of 30 perturbed GFS runs. The spread between members
# IS GFS's own uncertainty estimate — used by empirical_probability() in
# strategy.py to replace the Student's-t calibration layer.
# ---------------------------------------------------------------------------
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Separate cache for ensemble data (much larger payloads — 30 members × 10 days).
# Same TTL as main forecast cache.
gfs_ensemble_cache: TTLCache = TTLCache(maxsize=200, ttl=3600 * CACHE_TTL_HOURS)

def fetch_gfs_ensemble(lat: float, lon: float, tz: str,
                       days: int = 7) -> dict[str, list[float]]:
    """Fetch GFS 30-member ensemble max-temp forecast.

    Returns:
        {date_str: [member0_val, member1_val, ..., member29_val], ...}
        Values are in Celsius (as returned by the API).

    Raises:
        WeatherFetchError if the API call fails entirely.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone": tz,
        "models": "gfs_seamless",
        "forecast_days": min(days, 16),
    }
    try:
        resp = _get_with_backoff(ENSEMBLE_URL, params, timeout=20.0)
        data = resp.json()
    except Exception as e:
        raise WeatherFetchError(f"GFS ensemble fetch failed: {e}") from e

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    if not dates:
        raise WeatherFetchError("GFS ensemble response missing 'time' array")

    # Collect member arrays
    members: list[list[float]] = []
    for i in range(1, 31):
        key = f"temperature_2m_max_member{i:02d}"
        member_arr = daily.get(key)
        if member_arr:
            members.append(member_arr)

    if not members:
        raise WeatherFetchError(f"GFS ensemble returned 0 member arrays")

    # Build per-date result, dropping None values
    result: dict[str, list[float]] = {}
    for d_idx, date_str in enumerate(dates):
        vals = []
        for m_arr in members:
            if d_idx < len(m_arr) and m_arr[d_idx] is not None:
                vals.append(float(m_arr[d_idx]))
        if vals:
            result[date_str] = vals

    logger.debug(
        "GFS ensemble: %d members × %d dates for (%.2f, %.2f)",
        len(members), len(result), lat, lon,
    )
    return result


def get_city_gfs_ensemble(city_name: str, days: int = 7) -> dict[str, list[float]]:
    """Fetch GFS ensemble for a city. Cached per (city_name, days).

    Returns:
        {date_str: [member_values], ...}  or  {} on failure.
    """
    city_name = city_name.strip()
    if city_name not in CITIES:
        logger.warning("get_city_gfs_ensemble: unknown city %r", city_name)
        return {}

    cache_key = f"gfs_ens_{city_name}_{days}"
    if cache_key in gfs_ensemble_cache:
        return gfs_ensemble_cache[cache_key]

    info = CITIES[city_name]
    try:
        result = fetch_gfs_ensemble(info["lat"], info["lon"], info["tz"], days=days)
    except WeatherFetchError as e:
        logger.warning("GFS ensemble unavailable for %s: %s", city_name, e)
        result = {}

    gfs_ensemble_cache[cache_key] = result
    return result


def get_gfs_spread(city_name: str, target_date: str) -> float | None:
    """Return GFS ensemble std for a city+date. None if unavailable.

    Used by strategy.py for the HIGH_SPREAD_THRESHOLD check.
    """
    ensemble = get_city_gfs_ensemble(city_name, days=10)
    if target_date not in ensemble:
        return None
    vals = ensemble[target_date]
    if len(vals) < 2:
        return None
    mean_c = sum(vals) / len(vals)
    variance = sum((v - mean_c) ** 2 for v in vals) / (len(vals) - 1)
    return variance ** 0.5


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------
class WeatherFetchError(Exception):
    """Raised when all 5 weather sources fail for a city.
    Callers must handle this and skip the market — never trade on missing data.
    """
    pass

# ---------------------------------------------------------------------------
# Cache initialisation
# ---------------------------------------------------------------------------
memory_cache: TTLCache = TTLCache(maxsize=500, ttl=3600 * CACHE_TTL_HOURS)


def _load_disk_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load disk cache {CACHE_FILE}: {e}")
    return {}


def _save_disk_cache(data: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Failed to save disk cache {CACHE_FILE}: {e}")


disk_cache: dict = _load_disk_cache()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# City -> ICAO reverse map, derived from STATIONS at import (no external dep)
_CITY_TO_ICAO: Dict[str, str] = {
    v["city"]: icao
    for icao, v in STATIONS.items()
    if v.get("city")
}


def _resolve_coords(city_name: str) -> dict:
    """Return {lat, lon, tz} for a city.

    If STATION_COORDS=1 is set and the city has an ICAO mapping with a known
    station, returns the station's precise coords. Otherwise returns the
    city-center entry from CITIES. The returned dict is always safe to pass
    to Open-Meteo / Tomorrow.io query builders.
    """
    if _STATION_COORDS_ENABLED and STATIONS:
        icao = _CITY_TO_ICAO.get(city_name)
        if icao and icao in STATIONS:
            s = STATIONS[icao]
            return {
                "lat": s["lat"],
                "lon": s["lon"],
                "tz": s.get("tz") or CITIES[city_name]["tz"],
                "_source": f"station:{icao}",
            }
    entry = dict(CITIES[city_name])
    entry["_source"] = "city_center"
    return entry


def get_station_forecast(icao: str, days: int = 3) -> Dict[str, float]:
    """Forecast keyed by ICAO station code (Phase 2 API).

    Bypasses the city cache. Uses station coords directly. Queries all
    3 Open-Meteo models and returns the ensemble average.
    """
    model_forecasts = get_station_ensemble_forecast(icao, days)
    return _ensemble_average(model_forecasts)


def get_station_ensemble_forecast(icao: str, days: int = 3) -> Dict[str, Dict[str, float]]:
    """Per-model forecasts keyed by ICAO station code.

    Returns:
        {model_label: {date_str: max_temp_celsius, ...}, ...}

    Logs a warning if fewer than 3 models respond. Raises WeatherFetchError
    only if ALL models fail.
    """
    icao = icao.upper().strip()
    if icao not in STATIONS:
        raise ValueError(f"Unknown ICAO {icao!r}. Rebuild stations.json if this is a new market.")
    s = STATIONS[icao]
    synthetic = {"lat": s["lat"], "lon": s["lon"], "tz": s.get("tz") or "UTC"}
    errors = []
    model_forecasts: Dict[str, Dict[str, float]] = {}
    om_models = [
        (f"{_OM_HOST}/v1/forecast", None, "GFS", 10),
        (f"{_OM_HOST}/v1/ecmwf",   "ecmwf_ifs025", "ECMWF", 10),
        (f"{_OM_HOST}/v1/gfs",     "gfs_graphcast025", "GraphCast", 16),
    ]
    for url, model, label, max_days in om_models:
        try:
            params = _om_params(synthetic, min(days, max_days), model)
            resp = _get_with_backoff(url, params, timeout=15.0)
            model_forecasts[label] = _parse_om(resp.json(), days)
            logger.debug(f"Open-Meteo {label} OK for station {icao}")
        except Exception as e:
            logger.warning(f"Open-Meteo {label} failed for station {icao}: {e}")
            errors.append(f"{label}: {e}")
    if not model_forecasts:
        raise WeatherFetchError(f"All Open-Meteo models failed for {icao}: {errors}")
    if len(model_forecasts) < 3:
        logger.warning(
            f"ENSEMBLE WARNING: only {len(model_forecasts)}/3 models returned data "
            f"for station {icao}. Proceeding with available models."
        )
    return model_forecasts


def get_forecast(city_name: str, days: int = 3) -> Dict[str, float]:
    """
    Returns {date_str: max_temp_celsius} for the next `days` days.
    Example: {'2026-04-21': 18.4, '2026-04-22': 20.1, ...}

    Raises:
        ValueError:          city_name not in CITIES
        WeatherFetchError:   all 5 sources failed — caller must skip the market
    """
    city_name = city_name.strip()
    if city_name not in CITIES:
        raise ValueError(f"Unknown city: {city_name!r}. Add it to CITIES in weathercore/weather_v2.py")

    cache_key = f"{city_name}_{days}"

    # Layer 1 — memory cache (instant, lost on restart)
    if cache_key in memory_cache:
        logger.debug(f"Memory cache hit: {city_name}")
        return memory_cache[cache_key]

    # Layer 2 — disk cache (survives restarts, 3-hr TTL)
    if cache_key in disk_cache:
        entry = disk_cache[cache_key]
        if isinstance(entry, dict) and "timestamp" in entry:
            age = datetime.now() - datetime.fromisoformat(entry["timestamp"])
            if age < timedelta(hours=CACHE_TTL_HOURS):
                logger.debug(f"Disk cache hit: {city_name} (age {age})")
                memory_cache[cache_key] = entry["forecast"]
                return entry["forecast"]

    # Layers 3-7 — live fetch across all sources
    forecast = _fetch_with_fallback(city_name, days)

    # Populate both caches
    memory_cache[cache_key] = forecast
    disk_cache[cache_key] = {
        "forecast":  forecast,
        "timestamp": datetime.now().isoformat(),
    }
    _save_disk_cache(disk_cache)

    return forecast


def get_ensemble_forecast(city_name: str, days: int = 3) -> Dict[str, Dict[str, float]]:
    """Returns per-model forecasts for a city.

    Returns:
        {model_label: {date_str: max_temp_celsius, ...}, ...}
        e.g. {"GFS": {"2026-04-22": 18.4}, "ECMWF": {...}, "GraphCast": {...}}

    Cache behavior: uses the same cache as get_forecast() for the ensemble-
    averaged result, but always fetches fresh per-model data. For hot-path
    callers that need per-model detail, call this directly.

    Raises:
        ValueError:          city_name not in CITIES
        WeatherFetchError:   all sources failed
    """
    city_name = city_name.strip()
    if city_name not in CITIES:
        raise ValueError(f"Unknown city: {city_name!r}. Add it to CITIES in weathercore/weather_v2.py")
    return _fetch_ensemble(city_name, days)


def _load_bias_map() -> Dict[str, float]:
    """Read bias_corrections from the SQLite DB and return {ICAO: bias_c}.

    Cached for the process lifetime. Stations with n_samples below
    BIAS_MIN_SAMPLES are skipped so we do not apply noisy corrections.
    """
    global _BIAS_CACHE
    if _BIAS_CACHE is not None:
        return _BIAS_CACHE
    out: Dict[str, float] = {}
    try:
        import sqlite3
        if os.path.exists(_BIAS_DB_PATH):
            conn = sqlite3.connect(_BIAS_DB_PATH)
            cur = conn.execute("SELECT icao, bias_c, n_samples FROM bias_corrections")
            for icao, bias_c, n in cur.fetchall():
                if n is None or n < _BIAS_MIN_SAMPLES:
                    continue
                if bias_c is None:
                    continue
                out[icao.upper()] = float(bias_c)
            conn.close()
    except Exception as e:
        logger.warning("bias load failed (%s) — corrections disabled", e)
    _BIAS_CACHE = out
    return out


def _apply_bias(icao: Optional[str], forecast: Dict[str, float]) -> Dict[str, float]:
    if not icao:
        return forecast
    biases = _load_bias_map()
    bias = biases.get(icao.upper())
    if bias is None:
        return forecast
    return {d: (v + bias) for d, v in forecast.items()}


def get_corrected_forecast(city_name: str, days: int = 3) -> Dict[str, float]:
    """Like get_forecast but applies the station-level bias correction when enabled.

    Requires STATION_COORDS=1 (coords must match the station the bias was
    learned from) and BIAS_CORRECTION=1. Falls back to raw forecast if flag
    off or bias unavailable.
    """
    raw = get_forecast(city_name, days)
    if not _BIAS_CORRECTION_ENABLED:
        return raw
    icao = _CITY_TO_ICAO.get(city_name)
    return _apply_bias(icao, raw)


def get_corrected_station_forecast(icao: str, days: int = 3) -> Dict[str, float]:
    raw = get_station_forecast(icao, days)
    if not _BIAS_CORRECTION_ENABLED:
        return raw
    return _apply_bias(icao, raw)


def get_fused_forecast(city_name: str, days: int = 2) -> Dict[str, float]:
    """Short-horizon forecast that blends TAF TX (when available) with the
    bias-corrected model forecast.

    Behaviour:
      * If TAF_FUSION=0: returns get_corrected_forecast unchanged.
      * If TAF_FUSION=1 and a TAF TX exists for a target date within `days`:
        fused = TAF_WEIGHT * taf_tx + (1 - TAF_WEIGHT) * corrected_model
      * Otherwise falls back to the corrected model forecast for that date.

    TAF TX is only meaningful for the next ~30h so callers should keep
    `days` small (2 is the default; anything beyond day+1 is usually a
    no-op because the TAF does not cover it).
    """
    base = get_corrected_forecast(city_name, days)
    if not _TAF_FUSION_ENABLED:
        return base
    icao = _CITY_TO_ICAO.get(city_name)
    if not icao:
        return base
    try:
        from taf import get_taf_forecast  # lazy import to avoid httpx at module load
        taf_tx = get_taf_forecast(icao)
    except Exception as e:
        logger.warning("TAF fetch failed for %s (%s) — using model only", icao, e)
        return base
    if not taf_tx:
        return base
    w = max(0.0, min(1.0, _TAF_WEIGHT))
    fused = dict(base)
    for date, tx in taf_tx.items():
        if date in fused:
            fused[date] = w * tx + (1.0 - w) * fused[date]
    return fused


def get_forecast_fahrenheit(city_name: str, days: int = 3) -> Dict[str, float]:
    """Same as get_forecast but returns temperatures in Fahrenheit."""
    return {d: round((t * 9 / 5) + 32, 1) for d, t in get_forecast(city_name, days).items()}


def celsius_to_fahrenheit(c: float) -> float:
    return round((c * 9 / 5) + 32, 1)


def fahrenheit_to_celsius(f: float) -> float:
    return round((f - 32) * 5 / 9, 1)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _om_params(city: dict, days: int, model: Optional[str] = None,
               daily_var: str = "temperature_2m_max") -> dict:
    """Build an Open-Meteo query param dict, injecting the paid API key if set.

    daily_var: Open-Meteo daily variable. Use "temperature_2m_max" for
    highest-temperature markets (default) or "temperature_2m_min" for
    lowest-temperature markets.
    """
    p: dict = {
        "latitude":         city["lat"],
        "longitude":        city["lon"],
        "daily":            daily_var,
        "temperature_unit": "celsius",
        "timezone":         city["tz"],
        "forecast_days":    days,
    }
    if model:
        p["models"] = model
    if OPENMETEO_API_KEY:
        p["apikey"] = OPENMETEO_API_KEY
    return p


def _parse_om(data: dict, days: int,
              daily_var: str = "temperature_2m_max") -> Dict[str, float]:
    """Parse Open-Meteo API response into {date: temp} dict.

    Guard: Validates response structure. Raises ValueError on malformed data
    instead of returning bad/partial results.
    """
    if "daily" not in data:
        raise ValueError(f"Open-Meteo: missing 'daily' key. Keys: {list(data.keys())[:10]}")

    daily = data["daily"]

    if "time" not in daily:
        raise ValueError("Open-Meteo: missing 'daily.time' key")

    if daily_var not in daily:
        raise ValueError(
            f"Open-Meteo: missing '{daily_var}' in daily. "
            f"Available: {list(daily.keys())}"
        )

    times = daily["time"]
    temps = daily[daily_var]

    if len(times) != len(temps):
        raise ValueError(
            f"Open-Meteo: mismatched arrays: time={len(times)} temps={len(temps)}"
        )

    if not times:
        raise ValueError("Open-Meteo: empty 'time' array")

    # Filter out None values — don't silently skip
    result = {}
    for i in range(min(days, len(times))):
        if temps[i] is None:
            raise ValueError(
                f"Open-Meteo: None value at index {i} ({times[i]}) for {daily_var}"
            )
        result[times[i]] = float(temps[i])

    if not result:
        raise ValueError(f"Open-Meteo: no valid data after parsing {daily_var}")

    return result


def _parse_wttr(data: dict, days: int, key: str = "maxtempC") -> Dict[str, float]:
    weather = data.get("weather", [])
    if not weather:
        raise ValueError("wttr.in: empty 'weather' array")
    result = {
        e["date"]: float(e[key])
        for e in weather[:days]
        if "date" in e and key in e
    }
    if not result:
        raise ValueError("wttr.in: parsed to empty dict")
    return result


def _parse_tomorrow(data: dict, days: int, key: str = "temperatureMax") -> Dict[str, float]:
    try:
        daily = data["timelines"]["daily"]
    except (KeyError, TypeError):
        raise ValueError("tomorrow.io: missing timelines.daily")
    result = {
        e["time"][:10]: float(e["values"][key])
        for e in daily[:days]
        if "time" in e and "values" in e and key in e["values"]
    }
    if not result:
        raise ValueError("tomorrow.io: parsed to empty dict")
    return result


def _get_with_backoff(url: str, params: Optional[dict], timeout: float) -> httpx.Response:
    """GET with exponential backoff on 429. Raises WeatherFetchError after MAX_RETRIES."""
    for attempt in range(MAX_RETRIES):
        resp = httpx.get(url, params=params, timeout=timeout)
        if resp.status_code == 429:
            wait = BASE_BACKOFF * (2 ** attempt)
            logger.warning(f"429 from {url} (attempt {attempt + 1}/{MAX_RETRIES}). Backoff {wait:.0f}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    raise WeatherFetchError(f"Rate-limited after {MAX_RETRIES} retries: {url}")


def _fetch_ensemble(city_name: str, days: int,
                    daily_var: str = "temperature_2m_max") -> Dict[str, Dict[str, float]]:
    """
    Queries all 3 Open-Meteo models and returns per-model forecasts.

    daily_var: "temperature_2m_max" (default, daily high) or
    "temperature_2m_min" (daily low, for lowest-temperature markets).

    Returns:
        {model_label: {date_str: temp_celsius, ...}, ...}
        e.g. {"GFS": {"2026-04-22": 18.4}, "ECMWF": {"2026-04-22": 19.1}, ...}

    If fewer than 3 models succeed, logs a warning but returns what is available.
    If ALL 3 Open-Meteo models fail, falls back to Tomorrow.io / wttr.in
    and returns a single-model dict (keyed "Tomorrow.io" or "wttr.in").

    Raises WeatherFetchError only when every source fails.
    """
    city = _resolve_coords(city_name)
    errors: list = []
    model_forecasts: Dict[str, Dict[str, float]] = {}
    is_low = daily_var == "temperature_2m_min"

    # --- Sources 1-3: Open-Meteo model variants (all queried) ---
    om_models = [
        (f"{_OM_HOST}/v1/forecast", None,               "GFS",       10),
        (f"{_OM_HOST}/v1/ecmwf",   "ecmwf_ifs025",      "ECMWF",     10),
        (f"{_OM_HOST}/v1/gfs",     "gfs_graphcast025",  "GraphCast", 16),
    ]
    for url, model, label, max_days in om_models:
        try:
            params = _om_params(city, min(days, max_days), model, daily_var=daily_var)
            resp = _get_with_backoff(url, params, timeout=15.0)
            result = _parse_om(resp.json(), days, daily_var=daily_var)
            model_forecasts[label] = result
            logger.debug(f"Open-Meteo {label} OK for {city_name} ({daily_var})")
        except Exception as e:
            logger.warning(f"Open-Meteo {label} failed for {city_name} ({daily_var}): {e}")
            errors.append(f"OM-{label}: {e}")

    if model_forecasts:
        n = len(model_forecasts)
        if n < 3:
            logger.warning(
                f"ENSEMBLE WARNING: only {n}/3 Open-Meteo models returned data "
                f"for {city_name} ({daily_var}). Missing: {[l for _, _, l, _ in om_models if l not in model_forecasts]}. "
                f"Proceeding with available models."
            )
        return model_forecasts

    # --- All 3 OM models failed: fallback to Tomorrow.io / wttr.in ---
    logger.warning(f"All 3 Open-Meteo models failed for {city_name} ({daily_var}). Trying fallbacks.")

    if TOMORROW_IO_API_KEY:
        try:
            tio_field = "temperatureMin" if is_low else "temperatureMax"
            params = {
                "location":  f"{city['lat']},{city['lon']}",
                "apikey":    TOMORROW_IO_API_KEY,
                "units":     "metric",
                "timesteps": "1d",
                "fields":    tio_field,
            }
            resp = _get_with_backoff(
                "https://api.tomorrow.io/v4/weather/forecast", params, timeout=15.0
            )
            result = _parse_tomorrow(resp.json(), days, key=tio_field)
            logger.info(f"Tomorrow.io fallback OK for {city_name} ({daily_var})")
            return {"Tomorrow.io": result}
        except Exception as e:
            logger.warning(f"Tomorrow.io failed for {city_name}: {e}")
            errors.append(f"Tomorrow.io: {e}")
    else:
        errors.append("Tomorrow.io: TOMORROW_IO_API_KEY not set")

    try:
        url = f"https://wttr.in/{city_name.replace(' ', '+')}?format=j1"
        resp = _get_with_backoff(url, params=None, timeout=12.0)
        wttr_key = "mintempC" if is_low else "maxtempC"
        result = _parse_wttr(resp.json(), days, key=wttr_key)
        logger.info(f"wttr.in fallback OK for {city_name} ({daily_var})")
        return {"wttr.in": result}
    except Exception as e:
        logger.warning(f"wttr.in failed for {city_name}: {e}")
        errors.append(f"wttr.in: {e}")

    raise WeatherFetchError(
        f"All weather sources failed for {city_name!r} ({daily_var}). "
        f"Errors: {'; '.join(str(e) for e in errors)}"
    )


def _ensemble_average(model_forecasts: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    """Compute the simple average across all model forecasts per date.

    Used as the single-value forecast for caching / backward compatibility.
    """
    all_dates: set = set()
    for fc in model_forecasts.values():
        all_dates.update(fc.keys())

    averaged: Dict[str, float] = {}
    for d in sorted(all_dates):
        temps = [fc[d] for fc in model_forecasts.values() if d in fc]
        if temps:
            averaged[d] = round(sum(temps) / len(temps), 2)
    return averaged


def _fetch_with_fallback(city_name: str, days: int,
                         daily_var: str = "temperature_2m_max") -> Dict[str, float]:
    """Backward-compatible single-forecast interface.

    Internally runs the ensemble, averages the per-model results, and
    returns a single {date: temp} dict. Callers that need per-model
    detail should use get_ensemble_forecast() instead.
    """
    model_forecasts = _fetch_ensemble(city_name, days, daily_var=daily_var)
    return _ensemble_average(model_forecasts)


# ---------------------------------------------------------------------------
# Lowest-temperature public API
# ---------------------------------------------------------------------------

def get_forecast_low(city_name: str, days: int = 3) -> Dict[str, float]:
    """
    Returns {date_str: min_temp_celsius} for the next `days` days.
    Parallel to get_forecast() but fetches daily LOW (temperature_2m_min).

    Cached separately from high-temp forecasts (key suffix "_low").

    Raises:
        ValueError:          city_name not in CITIES
        WeatherFetchError:   all sources failed — caller must skip the market
    """
    city_name = city_name.strip()
    if city_name not in CITIES:
        raise ValueError(f"Unknown city: {city_name!r}. Add it to CITIES in weathercore/weather_v2.py")

    cache_key = f"{city_name}_{days}_low"

    if cache_key in memory_cache:
        logger.debug(f"Memory cache hit (low): {city_name}")
        return memory_cache[cache_key]

    if cache_key in disk_cache:
        entry = disk_cache[cache_key]
        if isinstance(entry, dict) and "timestamp" in entry:
            age = datetime.now() - datetime.fromisoformat(entry["timestamp"])
            if age < timedelta(hours=CACHE_TTL_HOURS):
                logger.debug(f"Disk cache hit (low): {city_name} (age {age})")
                memory_cache[cache_key] = entry["forecast"]
                return entry["forecast"]

    forecast = _fetch_with_fallback(city_name, days, daily_var="temperature_2m_min")

    memory_cache[cache_key] = forecast
    disk_cache[cache_key] = {
        "forecast":  forecast,
        "timestamp": datetime.now().isoformat(),
    }
    _save_disk_cache(disk_cache)

    return forecast


def get_ensemble_forecast_low(city_name: str, days: int = 3) -> Dict[str, Dict[str, float]]:
    """Per-model daily-low forecasts. Parallel to get_ensemble_forecast()."""
    city_name = city_name.strip()
    if city_name not in CITIES:
        raise ValueError(f"Unknown city: {city_name!r}. Add it to CITIES in weathercore/weather_v2.py")
    return _fetch_ensemble(city_name, days, daily_var="temperature_2m_min")


def get_forecast_low_fahrenheit(city_name: str, days: int = 3) -> Dict[str, float]:
    """Same as get_forecast_low but returns temperatures in Fahrenheit."""
    return {d: round((t * 9 / 5) + 32, 1) for d, t in get_forecast_low(city_name, days).items()}


# ---------------------------------------------------------------------------
# Standalone smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s [%(module)s] %(message)s")
    print(f"weathercore/weather_v2.py  —  {len(CITIES)} cities")
    print(f"Open-Meteo host : {_OM_HOST}")
    print(f"Cache file      : {CACHE_FILE}")
    print(f"Paid API key    : {'YES' if OPENMETEO_API_KEY else 'NO (free tier)'}")
    print(f"Tomorrow.io key : {'YES' if TOMORROW_IO_API_KEY else 'NO (source skipped)'}")
    print()
    test_cities = sys.argv[1:] or ["NYC", "Tokyo", "London"]
    for city in test_cities:
        try:
            f = get_forecast(city, 3)
            print(f"  {city}: {f}")
        except WeatherFetchError as e:
            print(f"  {city} FAILED: {e}")
        except ValueError as e:
            print(f"  {city} ERROR: {e}")
