"""
wunderground_client.py — Prototype Wunderground/Weather.com forecast fetcher.

Pulls 5-day daily high/low forecasts from the same weather.com API that
powers Wunderground (Polymarket's resolution source). The API key is the
public demo key visible in Wunderground's JavaScript source.

Why Wunderground?
  - It's the resolution source for all Polymarket weather markets.
  - It uses airport station data (ICAO), not grid model output.
  - The resolution source's own forecast is the best predictor of what it
    will later report as the resolution temperature.
"""

import json
import logging
import time
import re
from datetime import date, timedelta
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

# Temperature conversion (WU API returns integers, Polymarket uses C or F)
def _celsius_to_fahrenheit(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return round(c * 9.0 / 5.0 + 32.0, 1)

logger = logging.getLogger("wunderground")

# ── Public demo API key from Wunderground's JavaScript ──
API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
FORECAST_URL = (
    "https://api.weather.com/v3/wx/forecast/daily/5day"
    "?geocode={lat},{lon}"
    "&format=json"
    "&units=m"
    "&language=en-US"
    "&apiKey=" + API_KEY
)

# ── Airport coordinates (city name → lat, lon) ──
AIRPORT_COORDS: dict[str, tuple[float, float]] = {
    "NYC":      (40.6413, -73.7781),
    "Chicago":  (41.9742, -87.9073),
    "LA":       (33.9425, -118.4081),
    "Miami":    (25.7959, -80.2870),
    "Denver":   (39.8561, -104.6737),
    "DC":       (38.8512, -77.0402),
    "San Francisco": (37.6213, -122.3790),
    "Houston":  (29.9902, -95.3368),
    "Austin":   (30.1975, -97.6664),
    "Dallas":   (32.8998, -97.0403),
    "Atlanta":  (33.6407, -84.4277),
    "Seattle":  (47.4502, -122.3088),
    "Toronto":  (43.6777, -79.6248),
    "Mexico City": (19.4326, -99.1332),
    "Sao Paulo": (-23.4356, -46.4731),
    "Buenos Aires": (-34.8222, -58.5358),
    "London":   (51.4700, -0.4543),
    "Paris":    (49.0097, 2.5479),
    "Madrid":   (40.4719, -3.5626),
    "Milan":    (45.6306, 8.7281),
    "Munich":   (48.3538, 11.7861),
    "Warsaw":   (52.1657, 20.9671),
    "Moscow":   (55.9726, 37.4146),
    "Istanbul": (41.2753, 28.7519),
    "Ankara":   (40.1281, 32.9951),
    "Tel Aviv": (32.0114, 34.8867),
    "Amsterdam":(52.3105, 4.7683),
    "Helsinki": (60.3184, 24.9633),
    "Tokyo":    (35.5494, 139.7798),
    "Seoul":    (37.4602, 126.4407),
    "Beijing":  (40.0799, 116.6031),
    "Shanghai": (31.1434, 121.8052),
    "Chengdu":  (30.5785, 103.9471),
    "Chongqing":(29.7192, 106.6416),
    "Wuhan":    (30.7831, 114.2081),
    "Shenzhen": (22.6393, 113.8107),
    "Hong Kong":(22.3080, 113.9185),
    "Taipei":   (25.0777, 121.2328),
    "Singapore":(1.3592, 103.9894),
    "Lucknow":  (26.7606, 80.8893),
    "Kuala Lumpur": (2.7456, 101.7099),
    "Jakarta":  (-6.1256, 106.6559),
    "Busan":    (35.1796, 128.9382),
    "Guangzhou":(23.3924, 113.2988),
    "Manila":   (14.5086, 121.0196),
    "Jeddah":   (21.6700, 39.1506),
    "Karachi":  (24.9067, 67.1608),
    # Central America
    "Panama City": (9.0719, -79.3830),   # MPTO
    "Lagos":    (6.5774, 3.3215),
    "Cape Town":(-33.9715, 18.6021),
    "Wellington": (-41.3279, 174.8049),
}


def fetch_forecasts(cities: list[str]) -> dict[str, dict]:
    """Fetch 5-day Wunderground forecasts for given city names.

    Guard: Validates response structure before returning. If highs/lows are
    empty, mismatched, or contain None values, sets error instead of returning
    bad data. Never returns a success response with empty forecast_days.
    """
    results = {}
    for city in cities:
        coords = AIRPORT_COORDS.get(city)
        if not coords:
            results[city] = {"city": city, "error": "No coords"}
            continue

        lat, lon = coords
        url = FORECAST_URL.format(lat=lat, lon=lon)

        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            highs = data.get("calendarDayTemperatureMax", [])
            lows  = data.get("calendarDayTemperatureMin", [])
            narratives = data.get("narrative", [])

            # ── Guard: validate response structure ──
            if not highs or not lows:
                results[city] = {
                    "city": city,
                    "error": f"WU API returned empty temps: highs={len(highs)} lows={len(lows)}",
                }
                continue

            if len(highs) != len(lows):
                results[city] = {
                    "city": city,
                    "error": f"WU API mismatched arrays: highs={len(highs)} lows={len(lows)}",
                }
                continue

            if any(h is None for h in highs) or any(l is None for l in lows):
                results[city] = {
                    "city": city,
                    "error": "WU API returned None temperature values",
                }
                continue

            days = []
            today = date.today()
            for i in range(len(highs)):
                day_info = {
                    "date": str(today + timedelta(days=i)),
                    "high_c": highs[i],
                    "low_c": lows[i],
                    "narrative": narratives[i] if i < len(narratives) else "",
                }
                nar = narratives[i] if i < len(narratives) else ""
                _parse_narrative_ranges(nar, day_info)
                days.append(day_info)

            # ── Guard: ensure we got at least 1 valid day ──
            if not days:
                results[city] = {
                    "city": city,
                    "error": "WU API: no valid forecast days after parsing",
                }
                continue

            results[city] = {
                "city": city,
                "lat": lat, "lon": lon,
                "forecast_days": days,
                "error": None,
            }
        except URLError as e:
            results[city] = {"city": city, "error": str(e)}
        except Exception as e:
            results[city] = {"city": city, "error": str(e)}

        time.sleep(0.3)

    return results


def _parse_narrative_ranges(narrative: str, day_info: dict) -> None:
    """Parse high/low ranges from narrative like 'Highs 16 to 18C and lows 14 to 16C.'"""
    high_m = re.search(r"Highs?\s+(-?\d+)\s+to\s+(-?\d+)\s*[CF]", narrative)
    if high_m:
        day_info["high_range_min_c"] = int(high_m.group(1))
        day_info["high_range_max_c"] = int(high_m.group(2))

    low_m = re.search(r"lows?\s+(-?\d+)\s+to\s+(-?\d+)\s*[CF]", narrative)
    if low_m:
        day_info["low_range_min_c"] = int(low_m.group(1))
        day_info["low_range_max_c"] = int(low_m.group(2))


def get_forecast_for_date(city_data: dict, target_date: str) -> dict | None:
    """Get forecast for a specific date from city_data."""
    for day in city_data.get("forecast_days", []):
        if day["date"] == target_date:
            return day
    return None


def wunderground_prob(city_data: dict, market_date: str,
                      market_type: str, bucket_low: float | None,
                      bucket_high: float | None) -> tuple[float, str]:
    """
    Simple probability: does the resolution source's own forecast fall
    within the bucket?

    Why so simple:
      - Wunderground IS the resolver. They will report whatever their
        station says. Their own forecast is the single best predictor
        of their own future station reading.
      - GFS grid-model output doesn't know about station microclimate.
      - The t-distribution assumes a stationary error distribution that
        doesn't match station-level Wunderground rounding rules.

    Returns (probability, reason_string).
    """
    day = get_forecast_for_date(city_data, market_date)
    if not day:
        return (0.0, f"No Wunderground forecast for {market_date}")

    if market_type == "highest":
        forecast_c = day.get("high_c")
        range_min = day.get("high_range_min_c", forecast_c - 2 if forecast_c else 0)
        range_max = day.get("high_range_max_c", forecast_c + 2 if forecast_c else 0)
    elif market_type == "lowest":
        forecast_c = day.get("low_c")
        range_min = day.get("low_range_min_c", forecast_c - 2 if forecast_c else 0)
        range_max = day.get("low_range_max_c", forecast_c + 2 if forecast_c else 0)
    else:
        return (0.0, f"Unknown market_type: {market_type}")

    if forecast_c is None:
        return (0.0, "No forecast temperature available")

    reason = ""

    # Bucket: [low, high) — range bucket
    # "Grazing" only counts if the forecast point or a meaningful portion
    # of the range overlaps the bucket. If the range barely touches at
    # the extreme, treat as outside.
    if bucket_low is not None and bucket_high is not None:
        if bucket_low <= forecast_c < bucket_high:
            prob = 0.85
            reason = f"Wunderground {market_type}={forecast_c}C inside [{bucket_low}-{bucket_high})"
        elif range_min < bucket_high and range_max > bucket_low:
            # Real overlap — the forecast range meaningfully overlaps the bucket
            overlap_low  = max(range_min, bucket_low or float("-inf"))
            overlap_high = min(range_max, bucket_high or float("inf"))
            overlap_width = overlap_high - overlap_low
            range_width = range_max - range_min or 1
            if overlap_width / range_width >= 0.25:
                prob = 0.35
                reason = f"Wunderground range [{range_min}-{range_max}C] overlaps bucket [{bucket_low}-{bucket_high}) by {overlap_width/range_width:.0%}"
            else:
                prob = 0.02
                reason = f"Wunderground range [{range_min}-{range_max}C] barely touches bucket [{bucket_low}-{bucket_high})"
        else:
            prob = 0.02
            reason = f"Wunderground {market_type}={forecast_c}C outside [{bucket_low}-{bucket_high})"

    # Bucket: [X, None) — "X or higher"
    elif bucket_low is not None:
        if forecast_c >= bucket_low:
            prob = 0.85
            reason = f"Wunderground {market_type}={forecast_c}C >= {bucket_low}C"
        elif range_max >= bucket_low:
            prob = 0.35
            reason = f"Wunderground range up to {range_max}C touches {bucket_low}C"
        else:
            prob = 0.02
            reason = f"Wunderground {market_type}={forecast_c}C < {bucket_low}C"

    # Bucket: [None, X] — "X or below"
    elif bucket_high is not None:
        if forecast_c <= bucket_high:
            prob = 0.85
            reason = f"Wunderground {market_type}={forecast_c}C <= {bucket_high}C"
        elif range_min <= bucket_high:
            prob = 0.35
            reason = f"Wunderground range down to {range_min}C touches {bucket_high}C"
        else:
            prob = 0.02
            reason = f"Wunderground {market_type}={forecast_c}C > {bucket_high}C"

    else:
        prob = 0.50
        reason = "Both bucket bounds None (unbounded)"

    return (prob, reason)


def wunderground_match(city_data: dict, market_date: str,
                       market_type: str, bucket_low: float | None,
                       bucket_high: float | None,
                       unit: str = "C") -> tuple[str, str, float | None]:
    """Deterministic match: does WU forecast fall inside the Polymarket bucket?

    No probabilities. No range-grazing. No calibration. Pure binary decision.

    Args:
        city_data: Forecast data from fetch_forecasts().
        market_date: ISO date string.
        market_type: "highest" or "lowest".
        bucket_low: Lower bound (None = -inf). In the market's unit (C or F).
        bucket_high: Upper bound (None = +inf). In the market's unit (C or F).
        unit: "C" or "F" — the unit of bucket_low/bucket_high.

    Returns:
        ("TRADE" | "SKIP", reason_string, temperature_celsius | None)
    """
    day = get_forecast_for_date(city_data, market_date)
    if not day:
        return ("SKIP", f"No WU forecast for {market_date}", None)

    temp_c = day.get("low_c") if market_type == "lowest" else day.get("high_c")
    if temp_c is None:
        return ("SKIP", "No temperature in WU forecast", None)

    # Convert WU temperature to market's unit for comparison
    if unit.upper() == "F":
        temp_market = round(temp_c * 9.0 / 5.0 + 32.0)
    else:
        temp_market = temp_c

    in_bucket = False
    if bucket_low is not None and bucket_high is not None:
        # Range bucket: [low, high)
        in_bucket = bucket_low <= temp_market < bucket_high
        where = "inside" if in_bucket else "outside"
        reason = f"WU {market_type}={temp_market}{unit} {where} [{bucket_low}-{bucket_high})"
    elif bucket_low is not None:
        # "X or higher"
        in_bucket = temp_market >= bucket_low
        op = ">=" if in_bucket else "<"
        reason = f"WU {market_type}={temp_market}{unit} {op} {bucket_low}{unit}"
    elif bucket_high is not None:
        # "X or below"
        in_bucket = temp_market <= bucket_high
        op = "<=" if in_bucket else ">"
        reason = f"WU {market_type}={temp_market}{unit} {op} {bucket_high}{unit}"
    else:
        reason = "Unbounded bucket (both bounds None)"
        in_bucket = True

    return ("TRADE" if in_bucket else "SKIP", reason, temp_c)


# ── CLI ──
if __name__ == "__main__":
    import sys
    cities = sys.argv[1:] if len(sys.argv) > 1 else ["Wellington", "London", "Shanghai", "NYC", "Sao Paulo"]
    results = fetch_forecasts(cities)
    for city, data in results.items():
        if data.get("error"):
            print(f"{city}: ERROR — {data['error']}")
        else:
            print(f"\n=== {city} ===")
            for day in data["forecast_days"]:
                print(f"  {day['date']}: H {day['high_c']}C  L {day['low_c']}C  |  {day.get('narrative','')[:80]}")


# ── Resolution temperature (post-market) ─────────────────────────────────
# After the market date passes, Polymarket resolves against the WU station
# reading. We use the WU forecast API's calendarDayTemperatureMax/Min for
# the target date as our best proxy. This is WU's own prediction of what
# their station will report — far more aligned than Open-Meteo ERA5.
#
# For dates within the 5-day window, this returns WU's published forecast.
# For dates beyond 5 days, falls back to Open-Meteo Archive.
# ────────────────────────────────────────────────────────────────────────

# Cache: {(city, date): {"high_c": ..., "low_c": ..., "source": "wu"|...}}
_wu_resolution_cache: dict[tuple, dict] = {}


def get_wu_resolution_temp(
    city: str,
    target_date: str,
    market_type: str = "highest",
) -> Optional[dict]:
    """Fetch the Wunderground-predicted temperature for a specific date.

    This is the resolution source's own forecast — the best available proxy
    for what the WU station will actually report as the daily high/low.

    Args:
        city: City name (must be in AIRPORT_COORDS).
        target_date: ISO date string (YYYY-MM-DD).
        market_type: "highest" for daily max, "lowest" for daily min.

    Returns:
        dict with keys:
            temp_c: float (Celsius)
            temp_f: float (Fahrenheit)
            source: str ("wunderground-forecast" or "open-meteo-archive")
            date: str
        or None on failure.
    """
    global _wu_resolution_cache

    cache_key = (city, target_date, market_type)
    if cache_key in _wu_resolution_cache:
        return _wu_resolution_cache[cache_key]

    coords = AIRPORT_COORDS.get(city)
    if not coords:
        logger.warning(f"get_wu_resolution_temp: no coords for {city}")
        return None

    lat, lon = coords
    url = FORECAST_URL.format(lat=lat, lon=lon)

    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        # Find the target_date in the forecast
        dates = data.get("validTimeLocal", [])
        highs = data.get("calendarDayTemperatureMax", [])
        lows = data.get("calendarDayTemperatureMin", [])

        # ── Guard: validate response structure ──
        if not dates or not highs or not lows:
            logger.warning(
                f"WU resolution: empty response for {city}: "
                f"dates={len(dates)} highs={len(highs)} lows={len(lows)}"
            )
            return None

        if len(dates) != len(highs) or len(dates) != len(lows):
            logger.warning(
                f"WU resolution: mismatched arrays for {city}: "
                f"dates={len(dates)} highs={len(highs)} lows={len(lows)}"
            )
            return None

        for i, d in enumerate(dates):
            # WU returns dates like "2026-05-15T07:00:00+0800"
            date_str = d[:10]
            if date_str == target_date:
                if market_type == "lowest" and i < len(lows) and lows[i] is not None:
                    temp_c = float(lows[i])
                elif market_type != "lowest" and i < len(highs) and highs[i] is not None:
                    temp_c = float(highs[i])
                else:
                    logger.debug(f"WU resolution: no {market_type} temp for {city} on {target_date}")
                    return None

                result = {
                    "temp_c": temp_c,
                    "temp_f": _celsius_to_fahrenheit(temp_c),
                    "source": "wunderground-forecast",
                    "date": target_date,
                }
                _wu_resolution_cache[cache_key] = result
                logger.info(
                    f"WU resolution temp: {city} {target_date} | "
                    f"{market_type}={temp_c}C (source=wunderground-forecast)"
                )
                return result

        # Target date not in 5-day window — fall through to Open-Meteo
        logger.debug(
            f"WU resolution: {target_date} not in 5-day window for {city}, "
            f"falling back to Open-Meteo"
        )
        return None

    except Exception as e:
        logger.warning(f"WU resolution fetch failed for {city} {target_date}: {e}")
        return None
