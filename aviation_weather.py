"""
aviation_weather.py - Fetches real-time METAR observations and TAF forecasts
from the AviationWeather.gov JSON API.

Provides higher-fidelity same-day observed temperatures using official
airport-station data.  Falls back gracefully on any error so the existing
Open-Meteo pipeline is never disrupted.

Data source (free, public, no API key):
  - METAR endpoint:  https://aviationweather.gov/api/data/metar
  - TAF endpoint:    https://aviationweather.gov/api/data/taf

API guidelines (per AviationWeather.gov):
  - Max 100 requests per minute (enforced by rate_limiter.aviation_limiter).
  - Custom User-Agent header required.
  - Max 400 entries per response.

Mirrors the retry/backoff pattern used in weather.py.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

from weather_v2 import celsius_to_fahrenheit
from rate_limiter import aviation_limiter

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------
METAR_API_URL = "https://aviationweather.gov/api/data/metar"
TAF_API_URL = "https://aviationweather.gov/api/data/taf"

# Custom User-Agent per API guidelines
USER_AGENT = "PolymarketWeatherBot/2.0 (Python/requests)"

# Retry config (matches weather.py pattern)
MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0
RATE_LIMIT_BACKOFF_S = 10.0


# -----------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------
def _api_get(url: str, params: dict, timeout: int = 15) -> list | dict | None:
    """Fetches JSON from the AviationWeather API with retry, backoff,
    and rate limiting.

    Args:
        url: API endpoint URL.
        params: Query parameters dict.
        timeout: Per-request timeout in seconds.

    Returns:
        Parsed JSON (list or dict), or None if HTTP 204 (no data).

    Raises:
        requests.exceptions.ConnectionError: After all retries exhausted.
        requests.exceptions.HTTPError: On non-retryable 4xx errors.
    """
    last_exc: Optional[Exception] = None
    headers = {"User-Agent": USER_AGENT}

    for attempt in range(MAX_RETRIES):
        # Respect rate limit before every request
        aviation_limiter.wait()

        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=timeout,
            )

            # 204 = valid request, no data available
            if response.status_code == 204:
                return None

            response.raise_for_status()
            return response.json()

        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            last_exc = exc
            wait = BACKOFF_BASE_S * (2 ** attempt)
            logger.debug(
                "AviationWeather API attempt %d/%d failed (%s): %s. "
                "Retrying in %.1fs...",
                attempt + 1, MAX_RETRIES, url, type(exc).__name__, wait,
            )
            time.sleep(wait)

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None

            if status == 429:
                # Rate limited: back off aggressively
                retry_after = exc.response.headers.get("Retry-After")
                wait = (
                    float(retry_after) if retry_after
                    else RATE_LIMIT_BACKOFF_S * (2 ** attempt)
                )
                last_exc = exc
                logger.warning(
                    "AviationWeather API rate limited (429) attempt %d/%d. "
                    "Retrying in %.1fs...",
                    attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)

            elif status is not None and status < 500:
                # Other 4xx: not transient, fail immediately
                raise

            else:
                # 5xx: retryable server error
                last_exc = exc
                wait = BACKOFF_BASE_S * (2 ** attempt)
                logger.debug(
                    "AviationWeather API attempt %d/%d server error (%s): %s. "
                    "Retrying in %.1fs...",
                    attempt + 1, MAX_RETRIES, url, exc, wait,
                )
                time.sleep(wait)

    raise requests.exceptions.ConnectionError(
        f"AviationWeather API unreachable after {MAX_RETRIES} retries: {last_exc}"
    )


# -----------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------
def get_current_metar_temps(icao_list: list[str]) -> dict:
    """Fetches the latest METAR temperature for each station in *icao_list*.

    Uses the JSON API with comma-separated IDs (single request for all
    stations).

    Args:
        icao_list: List of ICAO station identifiers (e.g. ["KJFK", "KORD"]).

    Returns:
        Dict keyed by ICAO code::

            {
                "KJFK": {
                    "temp_c": 18.0,
                    "temp_f": 64.4,
                    "raw_metar": "METAR KJFK 071856Z ...",
                    "source": "metar",
                },
                ...
            }

        Stations with no data or parse errors are omitted.
    """
    if not icao_list:
        return {}

    wanted = set(s.upper() for s in icao_list)
    ids_param = ",".join(sorted(wanted))

    try:
        data = _api_get(METAR_API_URL, params={"ids": ids_param, "format": "json"})
    except Exception as exc:
        logger.warning("Failed to fetch METAR temps: %s", exc)
        return {}

    if not data:
        return {}

    results: dict = {}
    for obs in data:
        icao = str(obs.get("icaoId", "")).upper()
        if icao not in wanted:
            continue

        temp_c = obs.get("temp")
        if temp_c is None:
            continue

        try:
            temp_c = float(temp_c)
        except (ValueError, TypeError):
            continue

        raw = str(obs.get("rawOb", ""))[:200]

        # Keep only the most recent observation per station
        # (API returns most recent first)
        if icao not in results:
            results[icao] = {
                "temp_c": temp_c,
                "temp_f": celsius_to_fahrenheit(temp_c),
                "raw_metar": raw,
                "source": "metar",
            }

    missing = wanted - set(results.keys())
    if missing:
        logger.debug("No METAR data found for stations: %s", missing)

    return results


def get_historical_metar(icao: str, hours_back: int = 24) -> Optional[pd.DataFrame]:
    """Fetches recent METAR history for a station from the JSON API.

    Uses ``/api/data/metar?ids=XXXX&hours=N&format=json`` to retrieve
    all observations within the requested window.

    Args:
        icao: ICAO station code.
        hours_back: Number of hours of history to request (max 72 per API).

    Returns:
        DataFrame with columns [time, temp_c, temp_f, obs_epoch],
        sorted chronologically (oldest first).  Returns None on failure
        or no data.
    """
    icao = icao.upper()

    try:
        data = _api_get(
            METAR_API_URL,
            params={"ids": icao, "hours": min(hours_back, 72), "format": "json"},
        )
    except Exception as exc:
        logger.warning("Failed to fetch historical METAR for %s: %s", icao, exc)
        return None

    if not data:
        logger.debug("No historical METAR data for %s (hours_back=%d)", icao, hours_back)
        return None

    rows = []
    for obs in data:
        temp_c = obs.get("temp")
        obs_epoch = obs.get("obsTime")
        if temp_c is None or obs_epoch is None:
            continue

        try:
            temp_c = float(temp_c)
            obs_time = datetime.fromtimestamp(obs_epoch, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            continue

        rows.append({
            "time": obs_time.isoformat(),
            "temp_c": temp_c,
            "temp_f": celsius_to_fahrenheit(temp_c),
            "obs_epoch": obs_epoch,
        })

    if not rows:
        return None

    df = pd.DataFrame(rows).sort_values("obs_epoch").reset_index(drop=True)
    return df


def get_metar_current_day_max(icao: str) -> Optional[dict]:
    """Returns the running daily maximum temperature from METAR history.

    Fetches the last 24 hours of observations and computes the maximum
    temperature across all reports. This gives the true intra-day max,
    not just the latest single observation.

    This is the primary function consumed by observed_temps.py.

    Args:
        icao: Single ICAO station code.

    Returns:
        Dict with temp_c, temp_f, source, obs_count keys, or None.
    """
    df = get_historical_metar(icao, hours_back=24)
    if df is None or df.empty:
        return None

    max_row = df.loc[df["temp_c"].idxmax()]
    obs_count = len(df)

    logger.debug(
        "METAR running daily max for %s: %.1fC from %d observations "
        "(latest=%.1fC)",
        icao, max_row["temp_c"], obs_count, df.iloc[-1]["temp_c"],
    )

    return {
        "temp_c": float(max_row["temp_c"]),
        "temp_f": float(max_row["temp_f"]),
        "source": "metar",
        "obs_count": obs_count,
    }


def get_current_taf(icao: str) -> Optional[dict]:
    """Fetches the current TAF for a station via the JSON API.

    Returns parsed forecast periods (wind, visibility, clouds, time ranges).
    Temperature data is not typically available in US TAFs; the ``temp``
    array within each forecast period may be empty.

    Args:
        icao: ICAO station code.

    Returns:
        Dict with keys:
            icao: str
            raw_taf: str (full raw TAF text)
            valid_from: int (epoch)
            valid_to: int (epoch)
            forecasts: list of forecast period dicts
            source: str ("taf")
        Or None on failure / no data.
    """
    icao = icao.upper()

    try:
        data = _api_get(
            TAF_API_URL,
            params={"ids": icao, "format": "json"},
        )
    except Exception as exc:
        logger.debug("Failed to fetch TAF for %s: %s", icao, exc)
        return None

    if not data:
        logger.debug("No TAF data for station %s", icao)
        return None

    # API may return multiple TAFs; take the most recent
    taf = data[0]

    forecasts = []
    for fcst in taf.get("fcsts", []):
        forecasts.append({
            "time_from": fcst.get("timeFrom"),
            "time_to": fcst.get("timeTo"),
            "change": fcst.get("fcstChange"),
            "wind_dir": fcst.get("wdir"),
            "wind_speed_kt": fcst.get("wspd"),
            "wind_gust_kt": fcst.get("wgst"),
            "visibility": fcst.get("visib"),
            "clouds": fcst.get("clouds", []),
            "wx_string": fcst.get("wxString"),
            "temp": fcst.get("temp", []),
        })

    return {
        "icao": icao,
        "raw_taf": str(taf.get("rawTAF", ""))[:500],
        "valid_from": taf.get("validTimeFrom"),
        "valid_to": taf.get("validTimeTo"),
        "forecasts": forecasts,
        "source": "taf",
    }


# -----------------------------------------------------------------------
# Smoke test
# -----------------------------------------------------------------------
if __name__ == "__main__":
    from rate_limiter import aviation_limiter as _limiter

    print("Aviation Weather Module Test (JSON API)")
    print("=" * 60)

    # Test latest METAR temps (batched single request)
    print("\n--- Latest METAR temps: KJFK, KORD, EGLL ---")
    temps = get_current_metar_temps(["KJFK", "KORD", "EGLL"])
    for station, d in temps.items():
        print(f"  {station}: {d['temp_c']:.1f}C / {d['temp_f']:.1f}F")
        print(f"    Raw: {d['raw_metar'][:80]}...")
    if not temps:
        print("  No METAR data returned (check network)")

    # Test historical METAR (intra-day history)
    print("\n--- Historical METAR: KJFK last 12 hours ---")
    hist_df = get_historical_metar("KJFK", hours_back=12)
    if hist_df is not None:
        print(f"  Observations: {len(hist_df)}")
        print(f"  Temp range: {hist_df['temp_c'].min():.1f}C to {hist_df['temp_c'].max():.1f}C")
        print(f"  Latest: {hist_df.iloc[-1]['temp_c']:.1f}C at {hist_df.iloc[-1]['time']}")
        print(f"  Max:    {hist_df['temp_c'].max():.1f}C")
    else:
        print("  No historical data")

    # Test running daily max (the key function)
    print("\n--- Running daily max: KJFK ---")
    day_max = get_metar_current_day_max("KJFK")
    if day_max:
        print(
            f"  Daily max: {day_max['temp_c']:.1f}C / {day_max['temp_f']:.1f}F "
            f"(from {day_max['obs_count']} observations, source: {day_max['source']})"
        )
    else:
        print("  No data")

    # Compare: latest single observation vs running max
    print("\n--- Latest vs. Running Max comparison ---")
    if temps.get("KJFK") and day_max:
        latest = temps["KJFK"]["temp_c"]
        running = day_max["temp_c"]
        diff = running - latest
        print(f"  Latest observation: {latest:.1f}C")
        print(f"  Running daily max:  {running:.1f}C")
        print(f"  Difference:         {diff:+.1f}C")
        if diff > 0:
            print("  (Running max is higher, as expected when temp has peaked and cooled)")
        else:
            print("  (Latest IS the current max)")

    # Test TAF (structured JSON)
    print("\n--- TAF: KJFK ---")
    taf = get_current_taf("KJFK")
    if taf:
        print(f"  Raw: {taf['raw_taf'][:100]}...")
        print(f"  Forecast periods: {len(taf['forecasts'])}")
        for i, fcst in enumerate(taf["forecasts"][:3]):
            wind = f"{fcst['wind_dir']}@{fcst['wind_speed_kt']}kt"
            if fcst["wind_gust_kt"]:
                wind += f" G{fcst['wind_gust_kt']}kt"
            print(f"    Period {i + 1}: {wind} | vis={fcst['visibility']} | change={fcst['change']}")
    else:
        print("  No TAF data")

    # Rate limiter status
    print(f"\n--- Rate limiter: {_limiter} ---")

# ==================== AVIATION_ICAO DICT FOR SNIPER ====================
# One ICAO station per city — the exact station Polymarket uses to resolve markets.
AVIATION_ICAO = {
    # North America
    "NYC":           "KJFK",   # JFK International
    "Chicago":       "KORD",
    "LA":            "KLAX",
    "Miami":         "KMIA",
    "Denver":        "KDEN",
    "DC":            "KDCA",   # Reagan National
    "San Francisco": "KSFO",
    "Houston":       "KIAH",
    "Austin":        "KAUS",
    "Dallas":        "KDFW",
    "Atlanta":       "KATL",
    "Seattle":       "KSEA",
    "Toronto":       "CYYZ",
    "Mexico City":   "MMMX",
    # South America
    "Sao Paulo":     "SBGR",   # Guarulhos International
    "Buenos Aires":  "SAEZ",   # Ezeiza International
    # Europe
    "London":        "EGLL",   # Heathrow
    "Paris":         "LFPG",   # Charles de Gaulle
    "Madrid":        "LEMD",
    "Milan":         "LIMC",   # Malpensa
    "Munich":        "EDDM",
    "Warsaw":        "EPWA",
    "Moscow":        "UUEE",   # Sheremetyevo
    "Istanbul":      "LTFM",   # Istanbul Airport
    "Ankara":        "LTAC",
    "Tel Aviv":      "LLBG",
    # Asia
    "Tokyo":         "RJTT",   # Haneda
    "Seoul":         "RKSI",   # Incheon International
    "Beijing":       "ZBAA",
    "Shanghai":      "ZSPD",   # Pudong
    "Chengdu":       "ZUUU",
    "Chongqing":     "ZUCK",
    "Wuhan":         "ZHHH",
    "Shenzhen":      "ZGSZ",
    "Hong Kong":     "VHHH",
    "Taipei":        "RCTP",
    "Singapore":     "WSSS",
    "Lucknow":       "VILK",
    # Oceania
    "Wellington":    "NZWN",
    # Additional cities
    "Amsterdam":     "EHAM",   # Schiphol
    "Helsinki":      "EFHK",   # Helsinki-Vantaa
    "Panama City":   "MPTO",   # Tocumen International
    "Kuala Lumpur":  "WMKK",   # KL International
    "Jakarta":       "WIII",   # Soekarno-Hatta
}
