"""
metar_block.py — Pre-trade METAR block checks.

Prevents the bot from placing trades when live METAR observations
contradict the forecast bucket. This is a hard safety gate — it BLOCKS
the trade entirely, regardless of what the probability model says.

Two modes:

1. SAME-DAY BLOCK: If the METAR running daily max already EXCEEDS the
   bucket's upper bound, the day is already too hot for this bucket to
   win. BLOCK the trade. (Example: Seoul May 2 — METAR showed 17°C at
   10am but bot wanted to bet on [15,16]°C. Should have blocked.)

2. NEXT-DAY WARN: If current METAR temp + expected diurnal range would
   place tomorrow's max outside the bucket, flag as high-risk. Less
   reliable than same-day, so weighted softer.

Design decisions:
    - Direct HTTP to AviationWeather.gov (no circular imports)
    - ICAO mapping inlined (same as metar_bias.py)
    - Returns (block: bool, reason: str) for integration simplicity
    - 5-minute cache for same-day running max (avoids re-fetching
      during a single scan cycle)

Usage:
    from metar_block import should_block_trade

    block, reason = should_block_trade("Seoul", 15.0, 16.0, "C",
                                        "2026-05-02", metar_cache)
    if block:
        logger.warning(f"METAR block: {reason}")
        continue  # skip this trade
"""

import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from cachetools import TTLCache

logger = logging.getLogger(__name__)

# ── ICAO mapping (mirrors aviation_weather.AVIATION_ICAO) ─────────
_AVIATION_ICAO: dict = {
    "NYC": "KJFK", "Chicago": "KORD", "LA": "KLAX", "Miami": "KMIA",
    "Denver": "KDEN", "DC": "KDCA", "San Francisco": "KSFO",
    "Houston": "KIAH", "Austin": "KAUS", "Dallas": "KDFW",
    "Atlanta": "KATL", "Seattle": "KSEA", "Toronto": "CYYZ",
    "Mexico City": "MMMX", "Sao Paulo": "SBGR", "Buenos Aires": "SAEZ",
    "London": "EGLL", "Paris": "LFPG", "Madrid": "LEMD", "Milan": "LIMC",
    "Munich": "EDDM", "Warsaw": "EPWA", "Moscow": "UUEE",
    "Istanbul": "LTFM", "Ankara": "LTAC", "Tel Aviv": "LLBG",
    "Amsterdam": "EHAM", "Helsinki": "EFHK", "Tokyo": "RJTT",
    "Seoul": "RKSI", "Beijing": "ZBAA", "Shanghai": "ZSPD",
    "Chengdu": "ZUUU", "Chongqing": "ZUCK", "Wuhan": "ZHHH",
    "Shenzhen": "ZGSZ", "Hong Kong": "VHHH", "Taipei": "RCTP",
    "Singapore": "WSSS", "Lucknow": "VILK", "Kuala Lumpur": "WMKK",
    "Jakarta": "WIII", "Busan": "RKPK", "Guangzhou": "ZGGG",
    "Manila": "RPLL", "Jeddah": "OEJN", "Karachi": "OPKC",
    "Lagos": "DNMM", "Cape Town": "FACT", "Wellington": "NZWN",
    "Panama City": "MPTO",
}

# ── Tunables ───────────────────────────────────────────────────────
# Typical diurnal (day-night) temperature range in Celsius for spring
# Used for next-day estimates: METAR_current + diurnal_range = estimated max
DIURNAL_RANGE_DEFAULT = 7.5   # temperate spring default
DIURNAL_RANGE_CONTINENTAL = 10.0  # inland/continental
DIURNAL_RANGE_COASTAL = 5.0       # maritime/coastal

# How many METAR observations needed before we trust the running max
# (avoid false blocks in early morning when only 1-2 obs available)
MIN_METAR_OBS = 4

# Next-day risk threshold: if estimated max exceeds bucket by this much, skip
NEXT_DAY_SKIP_THRESHOLD_C = 5.0

# Cache: 5-min TTL for running daily max (avoids re-fetching per market)
_RUNNING_MAX_CACHE: TTLCache = TTLCache(maxsize=100, ttl=300)


# ── Diurnal range estimator ────────────────────────────────────────
def _diurnal_range(city: str) -> float:
    """Return expected diurnal temperature range for a city."""
    continental = {"Moscow", "Warsaw", "Ankara", "Toronto", "Chicago",
                   "Denver", "Beijing", "Wuhan", "Chongqing", "Seoul",
                   "Helsinki", "Atlanta", "Houston"}
    coastal = {"Tokyo", "London", "Amsterdam", "LA", "Miami",
               "Singapore", "Hong Kong", "Wellington", "Cape Town",
               "Shenzhen", "Tel Aviv", "Madrid", "Istanbul"}
    if city in continental:
        return DIURNAL_RANGE_CONTINENTAL
    if city in coastal:
        return DIURNAL_RANGE_COASTAL
    return DIURNAL_RANGE_DEFAULT


# ── Running daily max fetcher ─────────────────────────────────────
def _get_running_day_max(icao: str) -> Optional[dict]:
    """Fetch running daily max temp from METAR history for a station.

    Returns {temp_c, temp_f, obs_count} or None.
    Cached for 5 minutes per ICAO.
    """
    if icao in _RUNNING_MAX_CACHE:
        return _RUNNING_MAX_CACHE[icao]

    url = (
        "https://aviationweather.gov/api/data/metar"
        f"?ids={icao}&hours=24&format=json"
    )
    headers = {"User-Agent": "PolymarketWeatherBot/2.0 (metar_block)"}

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("METAR history fetch failed for %s: %s", icao, e)
        return None

    if not data:
        return None

    temps = []
    for obs in data:
        t = obs.get("temp")
        if t is not None:
            try:
                temps.append(float(t))
            except (ValueError, TypeError):
                pass

    if not temps:
        return None

    max_c = max(temps)
    obs_count = len(temps)

    from weather_v2 import celsius_to_fahrenheit

    result = {
        "temp_c": max_c,
        "temp_f": celsius_to_fahrenheit(max_c),
        "obs_count": obs_count,
    }
    _RUNNING_MAX_CACHE[icao] = result
    return result


# ── Core block check ──────────────────────────────────────────────
def should_block_trade(
    city: str,
    bucket_low: Optional[float],
    bucket_high: Optional[float],
    unit: str,
    market_date: str,
    metar_cache: Optional[dict] = None,
) -> tuple[bool, str]:
    """
    Determine if a trade should be BLOCKED based on live METAR data.

    Args:
        city: City name (must have ICAO mapping).
        bucket_low: Lower bound of the temperature bucket (None = -inf).
        bucket_high: Upper bound of the temperature bucket (None = +inf).
        unit: "F" or "C" (bucket units).
        market_date: ISO date string of the market.
        metar_cache: Optional pre-fetched METAR data {city: {temp_c, temp_f}}.
                    If provided, used for next-day estimates. Not used for
                    same-day (running max is always fetched fresh).

    Returns:
        (block: bool, reason: str)
        - block=True means the trade should be SKIPPED.
        - reason explains why (for logging/Telegram).
    """
    if city not in _AVIATION_ICAO:
        return False, ""

    icao = _AVIATION_ICAO[city]

    # Determine if same-day or future
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    is_same_day = (market_date == today)

    # ── Same-day: block if running max already exceeds bucket ─────
    if is_same_day and bucket_high is not None:
        day_max = _get_running_day_max(icao)

        if day_max is None:
            return False, ""  # No METAR data — can't block, let it trade

        if day_max["obs_count"] < MIN_METAR_OBS:
            logger.debug(
                "%s: only %d METAR obs — skipping same-day block (need %d)",
                city, day_max["obs_count"], MIN_METAR_OBS,
            )
            return False, ""

        # Convert running max to market unit
        if unit.upper() == "F":
            actual_high = day_max["temp_f"]
        else:
            actual_high = day_max["temp_c"]

        if actual_high >= bucket_high:
            reason = (
                f"METAR BLOCK: {city} same-day max={actual_high:.1f}{unit} "
                f"≥ bucket_high={bucket_high:.0f}{unit} "
                f"(from {day_max['obs_count']} obs)"
            )
            logger.warning(reason)
            return True, reason

        logger.debug(
            "%s: METAR max=%.1f%s < bucket_high=%.0f%s — trade allowed",
            city, actual_high, unit, bucket_high, unit,
        )

    # ── Same-day: block if running max is below bucket_low ────────
    if is_same_day and bucket_low is not None:
        day_max = _get_running_day_max(icao)

        if day_max is None or day_max["obs_count"] < MIN_METAR_OBS:
            return False, ""

        if unit.upper() == "F":
            actual_high = day_max["temp_f"]
        else:
            actual_high = day_max["temp_c"]

        # If running max is already below bucket_low AND it's past peak
        # heating time (late afternoon), the day can't reach the bucket
        # But if it's still morning, skip — the day could still warm up
        current_hour_utc = datetime.now(timezone.utc).hour
        past_peak = current_hour_utc >= 14  # 2pm UTC = late afternoon for most

        if past_peak and actual_high < bucket_low - 2.0:
            reason = (
                f"METAR BLOCK: {city} same-day max={actual_high:.1f}{unit} "
                f"< bucket_low-2={bucket_low-2:.0f}{unit} "
                f"(past peak heating, {day_max['obs_count']} obs)"
            )
            logger.warning(reason)
            return True, reason

    # ── Next-day: estimate if METAR trend contradicts bucket ──────
    if not is_same_day and bucket_high is not None and metar_cache:
        metar_obs = metar_cache.get(city)
        if metar_obs:
            current_temp = metar_obs["temp_c"]
            diurnal = _diurnal_range(city)
            est_max = current_temp + diurnal  # rough estimate of tomorrow's max

            if unit.upper() == "F":
                from weather_v2 import celsius_to_fahrenheit
                est_max = celsius_to_fahrenheit(est_max)
                bucket_high_f = bucket_high
                skip_threshold = celsius_to_fahrenheit(NEXT_DAY_SKIP_THRESHOLD_C)
            else:
                bucket_high_f = bucket_high
                skip_threshold = NEXT_DAY_SKIP_THRESHOLD_C

            # If estimated max exceeds bucket by threshold → high risk, skip
            if est_max > bucket_high_f + skip_threshold:
                reason = (
                    f"METAR WARN: {city} {market_date} est_max={est_max:.1f}{unit} "
                    f"(current={current_temp:.1f}C + diurnal={diurnal:.0f}C) "
                    f">> bucket_high={bucket_high_f:.0f}{unit} "
                    f"— skipping trade"
                )
                logger.warning(reason)
                return True, reason

            # Low-side check: if current is way below bucket_low, even
            # with max warming it may not reach
            if bucket_low is not None and unit.upper() != "F":
                if current_temp + diurnal < bucket_low - 2.0:
                    reason = (
                        f"METAR WARN: {city} {market_date} est_max={est_max:.1f}{unit} "
                        f"< bucket_low={bucket_low:.0f}{unit} "
                        f"— skipping trade"
                    )
                    logger.warning(reason)
                    return True, reason

    return False, ""


# ── Batch helper for scan cycle integration ──────────────────────
def check_all_blocked(
    signals: list[dict],
    metar_cache: dict,
) -> tuple[list[dict], list[dict]]:
    """
    Filter a list of trade signals, removing those blocked by METAR.

    Args:
        signals: List of signal dicts, each must have: city, bucket_low,
                bucket_high, unit, market_date.
        metar_cache: {city: {temp_c, temp_f}} from Phase 0 pre-fetch.

    Returns:
        (passed: list[dict], blocked: list[dict])
    """
    passed = []
    blocked = []
    for sig in signals:
        block, reason = should_block_trade(
            sig["city"],
            sig.get("bucket_low"),
            sig.get("bucket_high"),
            sig.get("unit", "C"),
            sig.get("market_date", ""),
            metar_cache,
        )
        if block:
            sig["block_reason"] = reason
            blocked.append(sig)
        else:
            passed.append(sig)

    if blocked:
        logger.info(
            "METAR blocked %d/%d signals: %s",
            len(blocked), len(signals),
            ", ".join(f"{s['city']}({s.get('block_reason','?')[:40]})"
                      for s in blocked),
        )

    return passed, blocked


# ── Smoke test ─────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== METAR Pre-Trade Block — Live Test ===\n")

    # Test same-day block on Seoul
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    city = "Seoul"
    bucket = (15.0, 16.0)

    block, reason = should_block_trade(city, bucket[0], bucket[1], "C", today)
    print(f"{city} [{bucket[0]},{bucket[1]}]°C today:")
    if block:
        print(f"  BLOCKED: {reason}")
    else:
        print(f"  ALLOWED")

    # Test next-day warning
    tomorrow = (datetime.now(timezone.utc).strftime("%Y-%m-%d") if False
                else "2026-05-04")  # placeholder for smoke test
    from datetime import timedelta
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    # Mock metar_cache for next-day test
    mock_cache = {"Seoul": {"temp_c": 12.0, "temp_f": 53.6}}
    block, reason = should_block_trade(
        city, 14.0, 15.0, "C", tomorrow, metar_cache=mock_cache
    )
    print(f"\n{city} [{14},{15}]°C next-day (METAR={mock_cache['Seoul']['temp_c']}°C):")
    if block:
        print(f"  BLOCKED: {reason}")
    else:
        print(f"  ALLOWED")
