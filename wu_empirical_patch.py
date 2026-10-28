# Patch to apply to wu_empirical.py

patch_log_wu_scan = '''
def log_wu_scan(city: str, market_date: str, bucket_type: str,
                wu_forecast_c: float, bucket_low_c, bucket_high_c) -> None:
    """
    Log a WU scan attempt (pending resolution).

    Called for every market where WU produces a valid forecast, regardless of
    whether we trade it or skip it. When the market resolves, record_wu_resolution()
    will find this row by (city, market_date, bucket_type) and fill in actual_temp_c
    and resolved, completing the calibration sample.

    Duplicate-safe: skips insert if a row already exists for this key.
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
                added_at TEXT NOT NULL DEFAULT (datetime(\'now\'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wu_pos_city ON wu_positions(city)")
        # Only insert if no row exists yet for this (city, market_date, bucket_type)
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
                f"forecast={wu_forecast_c:.1f}°C"
            )
    except Exception as e:
        logger.debug(f"log_wu_scan failed for {city} {market_date}: {e}")
    finally:
        conn.close()
'''
