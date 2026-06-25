"""
metar_bias.py — Live METAR-based forecast bias correction.

Compares current METAR observed temperatures against Open-Meteo forecasts
and computes a real-time bias correction to shift the forecast distribution
before probability calculation.

Core insight (from May 2, 2026 forensic analysis):
    Open-Meteo ensemble systematically UNDERESTIMATES max temperatures in
    spring/summer conditions. 15/20 losing positions had actual temps HIGHER
    than the forecast bucket. Live METAR data provides same-day ground truth
    that can correct this cold bias before trades are placed.

Math:
    1. Fetch current METAR temp for city's airport (T_metar)
    2. Get Open-Meteo hourly forecast for current hour (T_fcst_now)
    3. Compute bias: Δ = T_metar - T_fcst_now
    4. If |Δ| < 2°C: no correction (within noise tolerance)
    5. If |Δ| ≥ 2°C: apply correction to max-temp forecast
       - Same-day market: full weight (1.0)
       - Next-day market: dampened weight (0.5, tomorrow's bias is less certain)
    6. Clamp correction to ±5°C (safety rails)

Usage:
    from metar_bias import get_live_metar_biases

    biases = get_live_metar_biases(["Tokyo", "Seoul", "Warsaw"])
    # Returns: {"Tokyo": +3.2, "Warsaw": -2.1}
    # Seoul omitted if |bias| < 2°C or no METAR data

    # Then in strategy:
    corrected_forecast = raw_forecast + biases.get(city, 0.0)

Design decisions:
    - ICAO mapping reuses aviation_weather.AVIATION_ICAO (single source of truth)
    - Batch METAR fetch (single API call for all stations) via aviation_weather
    - Open-Meteo hourly forecast fetched via weather_v2 (cached, rate-limited)
    - 15-minute TTLCache prevents excessive API calls during scan loops
    - Falls back gracefully — missing METAR means no correction, not an error
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from cachetools import TTLCache

logger = logging.getLogger(__name__)

# ── Tunables ───────────────────────────────────────────────────────
BIAS_THRESHOLD_C = 2.0        # Min |Δ| to apply correction
BIAS_CAP_C = 5.0              # Max correction magnitude
SAME_DAY_WEIGHT = 1.0         # Full correction for today's markets
NEXT_DAY_WEIGHT = 0.5         # Dampened for tomorrow (bias may not persist)

# Cache: 15-min TTL to avoid re-fetching METAR mid-scan
_BIAS_CACHE: TTLCache = TTLCache(maxsize=1, ttl=900)


# ── City → ICAO mapping ────────────────────────────────────────────
# Mirrors aviation_weather.AVIATION_ICAO but avoids the circular import
# between weather.py ↔ aviation_weather.py.
_AVIATION_ICAO: dict = {
    "NYC":           "KJFK",
    "Chicago":       "KORD",
    "LA":            "KLAX",
    "Miami":         "KMIA",
    "Denver":        "KDEN",
    "DC":            "KDCA",
    "San Francisco": "KSFO",
    "Houston":       "KIAH",
    "Austin":        "KAUS",
    "Dallas":        "KDFW",
    "Atlanta":       "KATL",
    "Seattle":       "KSEA",
    "Toronto":       "CYYZ",
    "Mexico City":   "MMMX",
    "Sao Paulo":     "SBGR",
    "Buenos Aires":  "SAEZ",
    "London":        "EGLL",
    "Paris":         "LFPG",
    "Madrid":        "LEMD",
    "Milan":         "LIMC",
    "Munich":        "EDDM",
    "Warsaw":        "EPWA",
    "Moscow":        "UUEE",
    "Istanbul":      "LTFM",
    "Ankara":        "LTAC",
    "Tel Aviv":      "LLBG",
    "Amsterdam":     "EHAM",
    "Helsinki":      "EFHK",
    "Tokyo":         "RJTT",
    "Seoul":         "RKSI",
    "Beijing":       "ZBAA",
    "Shanghai":      "ZSPD",
    "Chengdu":       "ZUUU",
    "Chongqing":     "ZUCK",
    "Wuhan":         "ZHHH",
    "Shenzhen":      "ZGSZ",
    "Hong Kong":     "VHHH",
    "Taipei":        "RCTP",
    "Singapore":     "WSSS",
    "Lucknow":       "VILK",
    "Kuala Lumpur":  "WMKK",
    "Jakarta":       "WIII",
    "Busan":         "RKPK",
    "Guangzhou":     "ZGGG",
    "Manila":        "RPLL",
    "Jeddah":        "OEJN",
    "Karachi":       "OPKC",
    "Lagos":         "DNMM",
    "Cape Town":     "FACT",
    "Wellington":    "NZWN",
    "Panama City":   "MPTO",
}


def _city_to_icao(city: str) -> Optional[str]:
    """Map a city name to its primary ICAO station code."""
    return _AVIATION_ICAO.get(city)


def _fetch_metar_direct(icao_list: list[str]) -> dict[str, dict]:
    """Fetch METAR temps directly from AviationWeather.gov — no circular deps.

    Replicates aviation_weather.get_current_metar_temps() inline to avoid
    the weather.py ↔ aviation_weather.py circular import chain.
    """
    import json
    import urllib.request

    if not icao_list:
        return {}

    ids_param = ",".join(sorted(set(s.upper() for s in icao_list)))
    url = (
        "https://aviationweather.gov/api/data/metar"
        f"?ids={ids_param}&format=json"
    )
    headers = {"User-Agent": "PolymarketWeatherBot/2.0 (metar_bias)"}

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.warning("METAR HTTP request failed: %s", e)
        return {}

    if not data:
        return {}

    from weather_v2 import celsius_to_fahrenheit

    results: dict[str, dict] = {}
    for obs in data:
        icao = str(obs.get("icaoId", "")).upper()
        temp_c = obs.get("temp")
        if temp_c is None or icao not in set(s.upper() for s in icao_list):
            continue
        try:
            temp_c = float(temp_c)
        except (ValueError, TypeError):
            continue
        if icao not in results:
            results[icao] = {
                "temp_c": temp_c,
                "temp_f": celsius_to_fahrenheit(temp_c),
                "source": "metar",
            }
    return results


def compute_biases_from_metar(
    metar_data: dict[str, dict],
    icao_to_city: dict[str, str],
    forecast_date: Optional[str] = None,
) -> dict[str, float]:
    """
    Compute bias corrections from pre-fetched METAR data.

    Use this when you already have METAR observations (e.g., from bot.py's
    Phase 0 pre-fetch) and want to avoid a duplicate API call.

    Args:
        metar_data: {ICAO: {temp_c, temp_f, source}} from get_current_metar_temps()
        icao_to_city: {ICAO: city_name} reverse mapping
        forecast_date: target date for weight determination

    Returns:
        {city_name: bias_celsius} for cities with |Δ| >= 2°C.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_date = forecast_date or today
    is_same_day = (target_date == today)
    weight = SAME_DAY_WEIGHT if is_same_day else NEXT_DAY_WEIGHT

    # Get hourly forecasts for comparison
    icao_list = list(metar_data.keys())
    hourly_forecasts = _get_hourly_forecasts(icao_list)

    if not hourly_forecasts:
        return {}

    biases: dict[str, float] = {}
    for icao, obs in metar_data.items():
        city = icao_to_city.get(icao.upper())
        if not city:
            continue

        metar_temp_c = obs["temp_c"]
        fcst_temp_c = hourly_forecasts.get(icao.upper())

        if fcst_temp_c is None:
            continue

        raw_bias = metar_temp_c - fcst_temp_c

        if abs(raw_bias) < BIAS_THRESHOLD_C:
            continue

        clamped = max(-BIAS_CAP_C, min(BIAS_CAP_C, raw_bias))
        bias = clamped * weight

        biases[city] = round(bias, 1)
        logger.info(
            "METAR bias: %s (%s) | METAR=%.1f°C | fcst=%.1f°C | Δ=%+.1f°C "
            "→ correction=%+.1f°C",
            city, icao, metar_temp_c, fcst_temp_c, raw_bias, bias,
        )

    if biases:
        logger.info("METAR bias corrections: %s",
                     ", ".join(f"{c}={v:+.1f}°C" for c, v in sorted(biases.items())))

    return biases


# ── Core function ──────────────────────────────────────────────────
def get_live_metar_biases(
    cities: list[str],
    forecast_date: Optional[str] = None,
) -> dict[str, float]:
    """
    Compute live METAR-based bias corrections for a list of cities.

    Args:
        cities: List of city names (must match weather.CITIES keys).
        forecast_date: ISO date string ('2026-05-03') for the target
                       forecast. If None, uses today (UTC). Used to
                       determine weight: same-day=1.0, next-day=0.5.

    Returns:
        {city_name: bias_celsius} for cities with |Δ| >= 2°C.
        Cities without METAR data or with small bias are omitted.

    Side effects:
        Caches METAR results for 15 minutes to avoid re-fetching.
    """
    if not cities:
        return {}

    # Determine horizon weight
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_date = forecast_date or today
    is_same_day = (target_date == today)
    weight = SAME_DAY_WEIGHT if is_same_day else NEXT_DAY_WEIGHT

    # 1. Map cities to ICAO codes
    icao_map: dict[str, str] = {}  # icao → city
    icao_list: list[str] = []
    for city in cities:
        icao = _city_to_icao(city)
        if icao:
            icao_map[icao.upper()] = city
            icao_list.append(icao.upper())
        else:
            logger.debug("No ICAO station for %s — skipping METAR bias", city)

    if not icao_list:
        return {}

    # 2. Fetch METAR (batch, single API call)
    cache_key = ",".join(sorted(icao_list))
    if cache_key in _BIAS_CACHE:
        logger.debug("Using cached METAR biases (%d stations)", len(icao_list))
        return _BIAS_CACHE[cache_key]

    try:
        metar_data = _fetch_metar_direct(icao_list)
    except Exception as e:
        logger.warning("METAR fetch failed: %s — bias correction disabled this cycle", e)
        return {}

    if not metar_data:
        logger.debug("No METAR data returned for %d stations", len(icao_list))
        return {}

    # 3. Get Open-Meteo hourly forecast for comparison
    hourly_forecasts = _get_hourly_forecasts(icao_list)
    if not hourly_forecasts:
        logger.debug("No hourly forecast data — cannot compute bias")
        return {}

    # 4. Compute bias for each station
    biases: dict[str, float] = {}
    for icao, city in icao_map.items():
        if icao not in metar_data:
            logger.debug("No METAR for %s (%s)", city, icao)
            continue

        metar_temp_c = metar_data[icao]["temp_c"]
        fcst_temp_c = hourly_forecasts.get(icao)

        if fcst_temp_c is None:
            logger.debug("No hourly forecast for %s (%s) — skipping", city, icao)
            continue

        raw_bias = metar_temp_c - fcst_temp_c

        if abs(raw_bias) < BIAS_THRESHOLD_C:
            logger.debug(
                "%s (%s): METAR=%.1f°C, forecast=%.1f°C, Δ=%.1f°C — below threshold, no correction",
                city, icao, metar_temp_c, fcst_temp_c, raw_bias,
            )
            continue

        # Clamp and weight
        clamped = max(-BIAS_CAP_C, min(BIAS_CAP_C, raw_bias))
        bias = clamped * weight

        biases[city] = round(bias, 1)
        logger.info(
            "METAR bias: %s (%s) | METAR=%.1f°C | fcst=%.1f°C | Δ=%+.1f°C "
            "| clamped=%+.1f°C | weight=%.1f → correction=%+.1f°C",
            city, icao, metar_temp_c, fcst_temp_c, raw_bias, clamped, weight, bias,
        )

    if biases:
        logger.info("METAR bias corrections applied: %s",
                     ", ".join(f"{c}={v:+.1f}°C" for c, v in sorted(biases.items())))

    _BIAS_CACHE[cache_key] = biases
    return biases


# ── Hourly forecast helper ─────────────────────────────────────────
def _get_hourly_forecasts(icao_list: list[str]) -> dict[str, float]:
    """
    Fetch Open-Meteo hourly forecast for the current hour for each station.

    Returns {ICAO: temp_c_at_current_hour}.
    """
    from weather_v2 import STATIONS

    results: dict[str, float] = {}
    current_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    for icao in icao_list:
        station = STATIONS.get(icao.upper())
        if not station:
            logger.debug("No station coords for ICAO %s", icao)
            continue

        lat = station.get("lat")
        lon = station.get("lon")
        if lat is None or lon is None:
            continue

        try:
            temp = _fetch_hourly_temp(icao, lat, lon)
            if temp is not None:
                results[icao.upper()] = temp
        except Exception as e:
            logger.debug("Hourly fetch failed for %s: %s", icao, e)

    return results


def _fetch_hourly_temp(icao: str, lat: float, lon: float) -> Optional[float]:
    """
    Fetch the current-hour temperature from Open-Meteo forecast API.

    Uses the forecast endpoint (free tier, no API key needed for non-commercial
    hourly data retrieval — keeps it independent of the paid customer API).
    """
    import json
    import urllib.request

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m"
        f"&timezone=UTC"
        f"&start_date={today}&end_date={today}"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("Open-Meteo hourly request failed for %s: %s", icao, e)
        return None

    times = data.get("hourly", {}).get("time", [])
    temps = data.get("hourly", {}).get("temperature_2m", [])

    if not times or not temps or len(times) != len(temps):
        return None

    # Find the forecast for the current UTC hour
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    for t, temp in zip(times, temps):
        if t == now_str and temp is not None:
            return float(temp)

    # Fallback: use closest available hour
    valid = [(t, float(temp)) for t, temp in zip(times, temps) if temp is not None]
    if not valid:
        return None

    # Find closest to now
    now_dt = datetime.now(timezone.utc)
    closest = min(valid, key=lambda x: abs(
        datetime.fromisoformat(x[0]).replace(tzinfo=timezone.utc) - now_dt
    ))
    return closest[1]


# ── Direct integration helper for strategy.py ──────────────────────
def apply_bias_to_forecast(
    city: str,
    forecast_c: float,
    forecast_date: Optional[str] = None,
    precomputed_biases: Optional[dict[str, float]] = None,
) -> float:
    """
    Apply live METAR bias correction to a single forecast temperature.

    Convenience function for strategy.py integration.

    Args:
        city: City name.
        forecast_c: Raw forecast temperature in Celsius.
        forecast_date: Target date (for weight determination).
        precomputed_biases: Pre-fetched bias dict (avoids duplicate METAR calls
                           when processing multiple cities).

    Returns:
        Bias-corrected forecast temperature in Celsius.
    """
    if precomputed_biases and city in precomputed_biases:
        bias = precomputed_biases[city]
    else:
        biases = get_live_metar_biases([city], forecast_date)
        bias = biases.get(city, 0.0)

    corrected = forecast_c + bias
    if bias != 0.0:
        logger.debug(
            "%s: raw=%.1f°C + bias=%+.1f°C → corrected=%.1f°C",
            city, forecast_c, bias, corrected,
        )
    return corrected


# ── Smoke test ─────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== METAR Bias Correction — Live Test ===\n")

    test_cities = ["Tokyo", "Seoul", "Warsaw", "London", "NYC", "Istanbul"]
    print(f"Testing cities: {test_cities}\n")

    biases = get_live_metar_biases(test_cities)

    if biases:
        print(f"\nActive bias corrections ({len(biases)} cities):")
        for city, bias in sorted(biases.items()):
            direction = "↑" if bias > 0 else "↓"
            print(f"  {direction} {city:15s}: {bias:+.1f}°C")
    else:
        print("\nNo significant METAR biases detected (all within ±2°C threshold).")

    print(f"\n{len(test_cities) - len(biases)} cities below threshold or no METAR data.")
