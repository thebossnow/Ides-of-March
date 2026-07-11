#!/usr/bin/env python3
"""
backfill_wu_empirical.py — Backfill the WU empirical probability table from
historical Open-Meteo actual temperatures + existing resolved positions.

Strategy:
  For each of the 51 cities, pull daily high/low temps from Open-Meteo
  (past_days=90) and pair with the bucket thresholds from resolved positions
  to build a "forecast vs actual" history.

  Since we don't have historical WU forecasts, we use a proxy:
    - For each resolved position, we know the bucket and the actual temp.
    - We compute the buffer = distance from actual temp to nearest bucket edge.
    - This gives us the empirical distribution of "how far off" outcomes are
      from bucket thresholds, which is exactly what the empirical table needs.

  Additionally, for ALL cities (not just ones we traded), we pull 90 days of
  historical actuals from Open-Meteo and compute what the hit rate would have
  been for each buffer band, using the city's typical bucket range.

  The key insight: we don't need the historical forecast. We need the
  distribution of (actual_temp - bucket_threshold) for each city. The
  resolved positions give us exactly this.

Usage:
  python3 backfill_wu_empirical.py          # Full backfill
  python3 backfill_wu_empirical.py --dry    # Preview only
  python3 backfill_wu_empirical.py --stats  # Show current table stats
"""

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, date
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill")

# ── Configuration ──────────────────────────────────────────────────────────────
DB_PATH = "/root/weatherbot/positions.db"
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&daily=temperature_2m_max,temperature_2m_min"
    "&past_days={past_days}"
    "&timezone={tz}"
)
BATCH_SIZE = 5          # cities per batch
BATCH_DELAY = 2.0       # seconds between batches (rate limiting)
REQUEST_TIMEOUT = 15    # seconds per HTTP request
PAST_DAYS = 90          # how far back to pull

# Buffer bands in Fahrenheit (matching wu_empirical.py)
BUFFER_BANDS = [
    ("0F-2F",    0.0,  2.0),
    ("2F-5F",    2.0,  5.0),
    ("5F-10F",   5.0, 10.0),
    ("10F+",    10.0, None),
]

# ── City coordinates (from wunderground_client.py) ────────────────────────────
CITY_COORDS: dict[str, tuple[float, float]] = {
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
    "Panama City": (9.0719, -79.3830),
    "Lagos":    (6.5774, 3.3215),
    "Cape Town":(-33.9715, 18.6021),
    "Wellington": (-41.3279, 174.8049),
}

# Timezone mapping for major cities (Open-Meteo uses IANA timezone names)
CITY_TIMEZONES: dict[str, str] = {
    "NYC": "America/New_York",
    "Chicago": "America/Chicago",
    "LA": "America/Los_Angeles",
    "Miami": "America/New_York",
    "Denver": "America/Denver",
    "DC": "America/New_York",
    "San Francisco": "America/Los_Angeles",
    "Houston": "America/Chicago",
    "Austin": "America/Chicago",
    "Dallas": "America/Chicago",
    "Atlanta": "America/New_York",
    "Seattle": "America/Los_Angeles",
    "Toronto": "America/Toronto",
    "Mexico City": "America/Mexico_City",
    "Sao Paulo": "America/Sao_Paulo",
    "Buenos Aires": "America/Argentina/Buenos_Aires",
    "London": "Europe/London",
    "Paris": "Europe/Paris",
    "Madrid": "Europe/Madrid",
    "Milan": "Europe/Rome",
    "Munich": "Europe/Berlin",
    "Warsaw": "Europe/Warsaw",
    "Moscow": "Europe/Moscow",
    "Istanbul": "Europe/Istanbul",
    "Ankara": "Europe/Istanbul",
    "Tel Aviv": "Asia/Jerusalem",
    "Amsterdam": "Europe/Amsterdam",
    "Helsinki": "Europe/Helsinki",
    "Tokyo": "Asia/Tokyo",
    "Seoul": "Asia/Seoul",
    "Beijing": "Asia/Shanghai",
    "Shanghai": "Asia/Shanghai",
    "Chengdu": "Asia/Shanghai",
    "Chongqing": "Asia/Shanghai",
    "Wuhan": "Asia/Shanghai",
    "Shenzhen": "Asia/Shanghai",
    "Hong Kong": "Asia/Hong_Kong",
    "Taipei": "Asia/Taipei",
    "Singapore": "Asia/Singapore",
    "Lucknow": "Asia/Kolkata",
    "Kuala Lumpur": "Asia/Kuala_Lumpur",
    "Jakarta": "Asia/Jakarta",
    "Busan": "Asia/Seoul",
    "Guangzhou": "Asia/Shanghai",
    "Manila": "Asia/Manila",
    "Jeddah": "Asia/Riyadh",
    "Karachi": "Asia/Karachi",
    "Panama City": "America/Panama",
    "Lagos": "Africa/Lagos",
    "Cape Town": "Africa/Johannesburg",
    "Wellington": "Pacific/Auckland",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def f_to_c(f: float) -> float:
    return (f - 32.0) / 1.8


def c_to_f(c: float) -> float:
    return c * 1.8 + 32.0


def classify_buffer(buffer_f: float) -> str:
    """Classify a buffer (in °F) into a band name."""
    for band_name, lo_f, hi_f in BUFFER_BANDS:
        if hi_f is None:
            if buffer_f >= lo_f:
                return band_name
        elif lo_f <= buffer_f < hi_f:
            return band_name
    return "10F+"


def resolve_position(actual_c: float, bucket_low: float | None,
                     bucket_high: float | None, unit: str,
                     market_type: str) -> bool | None:
    """
    Determine if a resolved position was a win (True) or loss (False).
    Returns None if indeterminate.

    For 'highest' markets: win if actual_temp >= bucket_low (within bucket range).
    For 'lowest' markets:  win if actual_temp <= bucket_high (within bucket range).
    """
    # Convert bucket to Celsius if needed
    if unit.upper() == "F":
        bl_c = f_to_c(bucket_low) if bucket_low is not None else None
        bh_c = f_to_c(bucket_high) if bucket_high is not None else None
    else:
        bl_c = bucket_low
        bh_c = bucket_high

    if market_type == "highest":
        # ORHIGHER: bucket_low is the threshold, no upper bound
        if bh_c is None:
            return actual_c >= bl_c if bl_c is not None else None
        # Closed range: [bucket_low, bucket_high)
        if bl_c is not None and bh_c is not None:
            return bl_c <= actual_c < bh_c
        return None
    elif market_type == "lowest":
        # ORBELOW: bucket_high is the threshold, no lower bound
        if bl_c is None:
            return actual_c <= bh_c if bh_c is not None else None
        # Closed range
        if bl_c is not None and bh_c is not None:
            return bl_c <= actual_c <= bh_c
        return None
    return None


def compute_buffer(actual_c: float, bucket_low: float | None,
                   bucket_high: float | None, unit: str,
                   market_type: str) -> float | None:
    """
    Compute the buffer (distance from actual temp to nearest bucket edge) in °F.
    Positive = actual was inside the bucket. Negative = actual was outside.
    """
    if unit.upper() == "F":
        bl_c = f_to_c(bucket_low) if bucket_low is not None else None
        bh_c = f_to_c(bucket_high) if bucket_high is not None else None
    else:
        bl_c = bucket_low
        bh_c = bucket_high

    if market_type == "highest":
        if bh_c is None:
            # ORHIGHER: buffer = actual - bucket_low (positive = above threshold)
            if bl_c is not None:
                return (actual_c - bl_c) * 1.8  # Convert °C buffer to °F
        elif bl_c is not None:
            # Closed range: distance to nearest edge
            d_low = (actual_c - bl_c) * 1.8
            d_high = (bh_c - actual_c) * 1.8
            return min(d_low, d_high)
    elif market_type == "lowest":
        if bl_c is None:
            # ORBELOW: buffer = bucket_high - actual (positive = below threshold)
            if bh_c is not None:
                return (bh_c - actual_c) * 1.8
        elif bh_c is not None:
            d_high = (bh_c - actual_c) * 1.8
            d_low = (actual_c - bl_c) * 1.8
            return min(d_high, d_low)
    return None


# ── Open-Meteo fetch ──────────────────────────────────────────────────────────

def fetch_city_actuals(city: str, lat: float, lon: float,
                       tz: str, past_days: int) -> dict | None:
    """Fetch historical daily high/low temps from Open-Meteo."""
    url = OPEN_METEO_URL.format(lat=lat, lon=lon, past_days=past_days, tz=tz)
    try:
        req = Request(url, headers={"User-Agent": "weatherbot-backfill/1.0"})
        with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        if not dates or not max_temps:
            logger.warning(f"  {city}: empty response")
            return None
        return {
            "dates": dates,
            "max_c": max_temps,
            "min_c": min_temps,
        }
    except (URLError, HTTPError, OSError) as e:
        logger.warning(f"  {city}: fetch failed: {e}")
        return None


# ── Main backfill logic ───────────────────────────────────────────────────────

def backfill_from_resolved_positions(conn: sqlite3.Connection, dry: bool = False) -> int:
    """
    Phase 1: Use existing resolved positions to build empirical data.
    For each resolved position, compute the buffer and classify into bands.
    """
    logger.info("Phase 1: Backfilling from resolved positions...")

    rows = conn.execute("""
        SELECT city, market_date, actual_temp, bucket_low, bucket_high,
               unit, market_type, status
        FROM positions
        WHERE actual_temp IS NOT NULL
          AND status IN ('resolved_won', 'resolved_lost')
          AND market_type IS NOT NULL
          AND (bucket_low IS NOT NULL OR bucket_high IS NOT NULL)
    """).fetchall()

    logger.info(f"  Found {len(rows)} resolved positions with bucket data")

    # Aggregate: city → bucket_type → buffer_band → {hits, total}
    agg: dict[str, dict[str, dict[str, list]]] = {}
    inserted = 0

    for city, mdate, actual, bl, bh, unit, mtype, status in rows:
        won = resolve_position(actual, bl, bh, unit, mtype)
        if won is None:
            continue

        buf = compute_buffer(actual, bl, bh, unit, mtype)
        if buf is None:
            continue

        # For the empirical table, we care about the absolute buffer at trade time.
        # We use |buffer| as the band key (distance from threshold).
        abs_buf_f = abs(buf)
        band = classify_buffer(abs_buf_f)
        bt = mtype  # 'highest' or 'lowest'

        if city not in agg:
            agg[city] = {}
        if bt not in agg[city]:
            agg[city][bt] = {}
        if band not in agg[city][bt]:
            agg[city][bt][band] = [0, 0]  # [hits, total]
        agg[city][bt][band][1] += 1
        if won:
            agg[city][bt][band][0] += 1

    now = datetime.utcnow().isoformat()

    for city, type_data in agg.items():
        for bt, band_data in type_data.items():
            for band, (hits, total) in band_data.items():
                hit_rate = hits / total if total > 0 else 0.0
                band_lo = None
                band_hi = None
                for bname, blo, bhi in BUFFER_BANDS:
                    if bname == band:
                        band_lo, band_hi = blo, bhi
                        break

                if not dry:
                    conn.execute("""
                        INSERT OR REPLACE INTO wu_empirical
                        (city, bucket_type, buffer_band, band_low_f, band_high_f,
                         hit_rate, sample_size, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (city, bt, band, band_lo, band_hi,
                          hit_rate, total, now))

                    # Also insert individual wu_positions rows for traceability
                    # (only for positions that have wu_forecast_c — most won't)
                inserted += 1

                logger.info(
                    f"  {city:15s} {bt:7s} {band:7s} → "
                    f"hit_rate={hit_rate:.1%} (n={total})"
                )

    if not dry:
        conn.commit()

    logger.info(f"Phase 1 complete: {inserted} city-type-band entries from positions")
    return inserted


def backfill_from_open_meteo(conn: sqlite3.Connection, dry: bool = False) -> int:
    """
    Phase 2: Pull 90 days of historical actuals from Open-Meteo for all 51 cities.
    For each city, compute what the hit rate would have been for each buffer band
    using typical bucket thresholds derived from the city's climate.

    This gives us a climate-based prior: "historically, how often does this city's
    temp fall within X°F of a typical bucket threshold?"
    """
    logger.info("Phase 2: Backfilling from Open-Meteo historical actuals...")

    # For each city, we need to know what bucket thresholds are typical.
    # We derive this from the resolved positions: what buckets were actually traded?
    # For cities with no trades, we use climate-based defaults.

    # Get typical bucket thresholds per city from resolved positions
    city_buckets: dict[str, list[tuple[float, float, str]]] = {}
    rows = conn.execute("""
        SELECT city, bucket_low, bucket_high, unit, market_type, COUNT(*) as cnt
        FROM positions
        WHERE market_type IS NOT NULL
          AND (bucket_low IS NOT NULL OR bucket_high IS NOT NULL)
        GROUP BY city, bucket_low, bucket_high, unit, market_type
        ORDER BY cnt DESC
    """).fetchall()

    for city, bl, bh, unit, mtype, cnt in rows:
        if city not in city_buckets:
            city_buckets[city] = []
        if unit.upper() == "F":
            bl_c = f_to_c(bl) if bl is not None else 0.0
            bh_c = f_to_c(bh) if bh is not None else 0.0
        else:
            bl_c = bl if bl is not None else 0.0
            bh_c = bh if bh is not None else 0.0
        city_buckets[city].append((bl_c, bh_c, mtype))

    # Fetch historical actuals for all cities in batches
    city_actuals: dict[str, dict] = {}
    cities = list(CITY_COORDS.keys())
    total_cities = len(cities)

    for batch_start in range(0, total_cities, BATCH_SIZE):
        batch = cities[batch_start:batch_start + BATCH_SIZE]
        logger.info(
            f"  Fetching batch {batch_start // BATCH_SIZE + 1}/"
            f"{(total_cities + BATCH_SIZE - 1) // BATCH_SIZE}: "
            f"{', '.join(batch)}"
        )
        for city in batch:
            lat, lon = CITY_COORDS[city]
            tz = CITY_TIMEZONES.get(city, "UTC")
            data = fetch_city_actuals(city, lat, lon, tz, PAST_DAYS)
            if data:
                city_actuals[city] = data
        if batch_start + BATCH_SIZE < total_cities:
            time.sleep(BATCH_DELAY)

    logger.info(f"  Fetched data for {len(city_actuals)}/{total_cities} cities")

    # Now compute hit rates per city/band
    # For each city, for each day, for each typical bucket:
    #   compute buffer = distance from actual to bucket edge
    #   classify into band
    #   record hit/miss
    agg: dict[str, dict[str, dict[str, list]]] = {}
    now = datetime.utcnow().isoformat()
    inserted = 0

    for city, actuals in city_actuals.items():
        dates = actuals["dates"]
        max_temps = actuals["max_c"]
        min_temps = actuals["min_c"]

        buckets = city_buckets.get(city, [])
        if not buckets:
            # No traded buckets for this city — skip (no reference point)
            continue

        for i, d in enumerate(dates):
            day_max_c = max_temps[i]
            day_min_c = min_temps[i]
            if day_max_c is None or day_min_c is None:
                continue

            for bl_c, bh_c, mtype in buckets:
                # For 'highest' markets, use day max; for 'lowest', use day min
                if mtype == "highest":
                    actual_c = day_max_c
                elif mtype == "lowest":
                    actual_c = day_min_c
                else:
                    continue

                won = resolve_position(actual_c, bl_c, bh_c, "C", mtype)
                if won is None:
                    continue

                buf = compute_buffer(actual_c, bl_c, bh_c, "C", mtype)
                if buf is None:
                    continue

                abs_buf_f = abs(buf)
                band = classify_buffer(abs_buf_f)

                if city not in agg:
                    agg[city] = {}
                if mtype not in agg[city]:
                    agg[city][mtype] = {}
                if band not in agg[city][mtype]:
                    agg[city][mtype][band] = [0, 0]
                agg[city][mtype][band][1] += 1
                if won:
                    agg[city][mtype][band][0] += 1

    # Merge with existing empirical data (don't overwrite Phase 1 — add Phase 2)
    for city, type_data in sorted(agg.items()):
        for bt, band_data in sorted(type_data.items()):
            for band, (hits, total) in sorted(band_data.items()):
                hit_rate = hits / total if total > 0 else 0.0
                band_lo = None
                band_hi = None
                for bname, blo, bhi in BUFFER_BANDS:
                    if bname == band:
                        band_lo, band_hi = blo, bhi
                        break

                if not dry:
                    # Check if this combo already exists from Phase 1
                    existing = conn.execute(
                        "SELECT hit_rate, sample_size FROM wu_empirical "
                        "WHERE city=? AND bucket_type=? AND buffer_band=?",
                        (city, bt, band)
                    ).fetchone()

                    if existing:
                        # Merge: weighted average of hit rates
                        old_hr, old_n = existing
                        merged_n = old_n + total
                        merged_hr = (old_hr * old_n + hit_rate * total) / merged_n if merged_n > 0 else 0.0
                        conn.execute(
                            "UPDATE wu_empirical SET hit_rate=?, sample_size=?, last_updated=? "
                            "WHERE city=? AND bucket_type=? AND buffer_band=?",
                            (merged_hr, int(merged_n), now, city, bt, band)
                        )
                        logger.info(
                            f"  {city:15s} {bt:7s} {band:7s} → "
                            f"merged hit_rate={merged_hr:.1%} (n={merged_n}, "
                            f"old={old_hr:.1%} n={old_n}, new={hit_rate:.1%} n={total})"
                        )
                    else:
                        conn.execute("""
                            INSERT OR REPLACE INTO wu_empirical
                            (city, bucket_type, buffer_band, band_low_f, band_high_f,
                             hit_rate, sample_size, last_updated)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (city, bt, band, band_lo, band_hi,
                              hit_rate, total, now))
                        logger.info(
                            f"  {city:15s} {bt:7s} {band:7s} → "
                            f"hit_rate={hit_rate:.1%} (n={total})"
                        )
                inserted += 1

    if not dry:
        conn.commit()

    logger.info(f"Phase 2 complete: {inserted} city-type-band entries from Open-Meteo")
    return inserted


def show_stats(conn: sqlite3.Connection):
    """Display current empirical table statistics."""
    total = conn.execute("SELECT COUNT(*) FROM wu_empirical").fetchone()[0]
    cities = conn.execute(
        "SELECT COUNT(DISTINCT city) FROM wu_empirical"
    ).fetchone()[0]
    samples = conn.execute(
        "SELECT SUM(sample_size) FROM wu_empirical"
    ).fetchone()[0]

    print(f"\n{'='*60}")
    print(f"WU Empirical Table Stats")
    print(f"{'='*60}")
    print(f"  Entries:  {total}")
    print(f"  Cities:   {cities}")
    print(f"  Samples:  {samples}")
    print()

    # Show table
    rows = conn.execute("""
        SELECT city, bucket_type, buffer_band, hit_rate, sample_size
        FROM wu_empirical
        WHERE sample_size > 0
        ORDER BY city, bucket_type, buffer_band
    """).fetchall()

    if not rows:
        print("  (empty)")
        return

    print(f"  {'City':15s} | {'Type':7s} | {'Band':7s} | {'Hit Rate':>8s} | {'N':>5s}")
    print(f"  {'-'*15}-+-{'-'*7}-+-{'-'*7}-+-{'-'*8}-+-{'-'*5}")
    for city, bt, band, hr, n in rows:
        print(f"  {city:15s} | {bt:7s} | {band:7s} | {hr:>7.1%} | {n:>5d}")
    print()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    dry = "--dry" in sys.argv
    stats_only = "--stats" in sys.argv

    conn = sqlite3.connect(DB_PATH)
    try:
        # Ensure tables exist
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wu_empirical (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                bucket_type TEXT NOT NULL,
                buffer_band TEXT NOT NULL,
                band_low_f REAL,
                band_high_f REAL,
                hit_rate REAL NOT NULL,
                sample_size INTEGER NOT NULL DEFAULT 0,
                last_updated TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_wu_emp_city_type_band
            ON wu_empirical(city, bucket_type, buffer_band)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wu_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                market_date TEXT NOT NULL,
                bucket_type TEXT NOT NULL,
                wu_forecast_c REAL,
                bucket_low_c REAL,
                bucket_high_c REAL,
                actual_temp_c REAL,
                resolved INTEGER NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wu_pos_city ON wu_positions(city)"
        )
        conn.commit()

        if stats_only:
            show_stats(conn)
            return

        if dry:
            logger.info("=== DRY RUN — no writes ===")

        logger.info("=" * 60)
        logger.info("WU Empirical Backfill Starting")
        logger.info("=" * 60)

        # Phase 1: From resolved positions
        n1 = backfill_from_resolved_positions(conn, dry=dry)

        # Phase 2: From Open-Meteo historical actuals
        n2 = backfill_from_open_meteo(conn, dry=dry)

        logger.info("=" * 60)
        logger.info(f"Backfill complete: {n1 + n2} total entries ({n1} from positions, {n2} from Open-Meteo)")
        logger.info("=" * 60)

        # Show final stats
        if not dry:
            show_stats(conn)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
