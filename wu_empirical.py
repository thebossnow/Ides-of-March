"""
wu_empirical.py - Per-city WU empirical probability table.

Collects WU forecast-vs-actual data from resolved positions and
builds an empirical lookup table. Each row = hit rate for a specific
city + bucket_type + buffer_band combination.

When a city has 30+ samples, wu_empirical_prob() returns the
historical hit rate directly — no distributional assumptions.

Table schema:
  CREATE TABLE wu_empirical (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      city TEXT NOT NULL,
      bucket_type TEXT NOT NULL,   -- 'highest' or 'lowest'
      buffer_band TEXT NOT NULL,   -- '0F-2F', '2F-5F', '5F-10F', '10F+'
      band_low_f REAL,
      band_high_f REAL,
      hit_rate REAL NOT NULL,
      sample_size INTEGER NOT NULL DEFAULT 0,
      last_updated TEXT NOT NULL
  );
  CREATE UNIQUE INDEX idx_wu_emp_city_type_band
      ON wu_empirical(city, bucket_type, buffer_band);
  CREATE TABLE wu_positions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      city TEXT NOT NULL,
      market_date TEXT NOT NULL,
      bucket_type TEXT NOT NULL,
      wu_forecast_c REAL,
      bucket_low_c REAL,
      bucket_high_c REAL,
      actual_temp_c REAL,
      resolved INTEGER NOT NULL,  -- 1=won, 0=lost
      added_at TEXT NOT NULL DEFAULT (datetime('now'))
  );
  CREATE INDEX idx_wu_pos_city ON wu_positions(city);
"""

import sqlite3
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), 'positions.db')

# Buffer bands in Fahrenheit
BUFFER_BANDS = [
    ("0F-2F",    0.0,  2.0),
    ("2F-5F",    2.0,  5.0),
    ("5F-10F",   5.0, 10.0),
    ("10F+",    10.0, None),
]

# Minimum samples for a reliable empirical hit rate
MIN_EMPIRICAL_SAMPLES = 30

# Fallback: use wu_normal_probability when sample_size < MIN_EMPIRICAL_SAMPLES
FALLBACK_PROB_FN = None  # Set externally to avoid circular import


def init_db():
    """Create tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    try:
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wu_pos_city ON wu_positions(city)")
        conn.commit()
    finally:
        conn.close()



def log_wu_scan(city: str, market_date: str, bucket_type: str,
                wu_forecast_c: float, bucket_low_c, bucket_high_c) -> None:
    """
    Log a WU scan attempt with pending resolution.

    Called for EVERY market where WU produces a valid forecast, regardless of
    whether we trade or skip it. When the market resolves, record_wu_resolution()
    finds this row by (city, market_date, bucket_type) and fills in actual_temp_c
    and resolved — completing the calibration sample.

    Duplicate-safe: skips insert if a row already exists for this key.
    This means the first scan per (city, date, type) wins; subsequent rescans
    of the same market in the same bot cycle are ignored.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
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
                resolved INTEGER,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wu_pos_city ON wu_positions(city)")
        existing = conn.execute(
            "SELECT id FROM wu_positions WHERE city=? AND market_date=? AND bucket_type=?",
            (city, market_date, bucket_type)
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO wu_positions
                   (city, market_date, bucket_type, wu_forecast_c,
                    bucket_low_c, bucket_high_c, resolved)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                (city, market_date, bucket_type, wu_forecast_c, bucket_low_c, bucket_high_c)
            )
            conn.commit()
            logger.debug(
                f"WU scan logged: {city} {market_date} {bucket_type} "
                f"forecast={wu_forecast_c:.1f}°C bl={bucket_low_c} bh={bucket_high_c}"
            )
    except Exception as e:
        logger.debug(f"log_wu_scan failed for {city} {market_date}: {e}")
    finally:
        conn.close()

def record_wu_resolution(city: str, market_date: str, bucket_type: str,
                          wu_forecast_c: float, bucket_low_c: float | None,
                          bucket_high_c: float | None, actual_temp_c: float,
                          resolved_won: bool) -> None:
    """Record one resolved WU position for empirical calibration."""
    conn = sqlite3.connect(DB_PATH)
    try:
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wu_pos_city ON wu_positions(city)")
        # Try to update an existing pending scan-log row first (logged by log_wu_scan).
        # Falls back to INSERT so resolution still works even if scan logging missed it.
        updated = conn.execute(
            """UPDATE wu_positions
               SET actual_temp_c=?, resolved=?
               WHERE city=? AND market_date=? AND bucket_type=? AND resolved IS NULL""",
            (actual_temp_c, 1 if resolved_won else 0, city, market_date, bucket_type)
        ).rowcount
        if not updated:
            conn.execute(
                """INSERT INTO wu_positions
                   (city, market_date, bucket_type, wu_forecast_c,
                    bucket_low_c, bucket_high_c, actual_temp_c, resolved)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (city, market_date, bucket_type, wu_forecast_c,
                 bucket_low_c, bucket_high_c, actual_temp_c, 1 if resolved_won else 0)
            )
        conn.commit()
    finally:
        conn.close()


def _classify_buffer(forecast_c: float, bucket_low_c: float,
                      bucket_high_c: float) -> tuple[str, str]:
    """
    Classify the forecast position relative to the bucket.

    Returns (bucket_type, buffer_band).

    For highest-temp: buffer = forecast - bucket_low
    For lowest-temp:  buffer = bucket_high - forecast
    """
    if bucket_high_c is None:
        # ORHIGHER bucket: buffer = forecast - bucket_low
        buffer_c = forecast_c - bucket_low_c
        bucket_type = "highest"
    elif bucket_low_c is None:
        # ORBELOW bucket: buffer = bucket_high - forecast
        buffer_c = bucket_high_c - forecast_c
        bucket_type = "lowest"
    else:
        # Closed range — use distance to nearest edge
        buffer_c = min(
            abs(forecast_c - bucket_low_c),
            abs(forecast_c - bucket_high_c)
        )
        bucket_type = "highest"  # Default

    # Convert to Fahrenheit for band classification
    buffer_f = buffer_c * 1.8

    for band_name, lo_f, hi_f in BUFFER_BANDS:
        if hi_f is None:
            if buffer_f >= lo_f:
                return bucket_type, band_name
        elif lo_f <= buffer_f < hi_f:
            return bucket_type, band_name

    return bucket_type, "10F+"  # Fallback


def rebuild_table():
    """Rebuild the empirical table from all recorded WU positions."""
    conn = sqlite3.connect(DB_PATH)
    try:
        # Clear and rebuild
        conn.execute("DELETE FROM wu_empirical")
        conn.execute("DELETE FROM wu_positions WHERE resolved IS NULL")

        rows = conn.execute(
            """SELECT city, bucket_type, bucket_low_c, bucket_high_c,
                      wu_forecast_c, actual_temp_c, resolved
               FROM wu_positions
               WHERE resolved IS NOT NULL"""
        ).fetchall()

        # Aggregate by city + bucket_type + buffer_band
        agg: dict = {}
        for city, bucket_type, bl_c, bh_c, wu_fc_c, actual_c, resolved in rows:
            if actual_c is None:
                continue
            actual_bt, band = _classify_buffer(wu_fc_c, bl_c, bh_c)
            key = (city, actual_bt, band)
            if key not in agg:
                agg[key] = {"hits": 0, "total": 0}
            agg[key]["total"] += 1
            if resolved == 1:
                agg[key]["hits"] += 1

        now = datetime.utcnow().isoformat()
        for (city, bt, band), data in agg.items():
            hit_rate = data["hits"] / data["total"] if data["total"] > 0 else 0.0
            band_lo = None
            band_hi = None
            for bname, blo, bhi in BUFFER_BANDS:
                if bname == band:
                    band_lo, band_hi = blo, bhi
                    break
            conn.execute(
                """INSERT OR REPLACE INTO wu_empirical
                   (city, bucket_type, buffer_band, band_low_f, band_high_f,
                    hit_rate, sample_size, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (city, bt, band, band_lo, band_hi,
                 hit_rate, data["total"], now)
            )

        conn.commit()
        total = sum(v["total"] for v in agg.values())
        cities = len(set(k[0] for k in agg))
        logger.info(
            f"WU empirical table rebuilt: {total} samples across {cities} cities, "
            f"{len(agg)} city-type-band combos"
        )
        return {"samples": total, "cities": cities, "combos": len(agg)}

    finally:
        conn.close()


def get_empirical_prob(city: str, bucket_type: str,
                        wu_forecast_c: float, bucket_low_c: float,
                        bucket_high_c: float) -> float | None:
    """
    Get empirical probability for a WU trade.

    Returns:
        float: empirical hit rate, or None if insufficient data.
        Use wu_normal_probability as fallback when None.
    """
    _bt, band = _classify_buffer(wu_forecast_c, bucket_low_c, bucket_high_c)
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            """SELECT hit_rate, sample_size FROM wu_empirical
               WHERE city = ? AND bucket_type = ? AND buffer_band = ?""",
            (city, _bt, band)
        ).fetchone()

        if row and row[1] >= MIN_EMPIRICAL_SAMPLES:
            return row[0]  # hit rate from real data

        return None  # insufficient data or no entry

    finally:
        conn.close()


def dump_table(limit: int = 30) -> str:
    """Print the empirical table for debugging."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """SELECT city, bucket_type, buffer_band, hit_rate, sample_size
               FROM wu_empirical
               WHERE sample_size > 0
               ORDER BY sample_size DESC, city
               LIMIT ?""",
            (limit,)
        ).fetchall()
        if not rows:
            return "No empirical data yet."
        lines = ["City | Type | Band | Hit Rate | Samples"]
        for city, bt, band, hr, n in rows:
            lines.append(f"{city:15s} | {bt:7s} | {band:7s} | {hr:.0%} | {n}")
        return "\n".join(lines)
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    result = rebuild_table()
    print(f"Rebuild: {result}")
    print(dump_table(50))
