#!/usr/bin/env python3
"""
backfill_ground_truth.py — populate observed temperatures so we can
ground-truth calibration v2.

Why: wu_positions has 659 past-dated rows with no actual_temp_c and no
resolved flag; positions has 28 recently-resolved rows where actual_temp
is NULL (CLOB-based resolution path doesn't set it). Without these, the
scan_log + wu_positions join produces zero usable rows.

Two passes:
  1. wu_positions: for every distinct (city, market_date, bucket_type)
     in the past, fetch observed temp via observed_temps API. Update
     actual_temp_c and resolved flag.
  2. positions: for every resolved row with NULL actual_temp where
     market_date is past, fetch observed temp and update actual_temp.

Uses an in-process cache so duplicate (city, date, market_type) lookups
hit the API once. Safe to re-run — only updates rows still missing data.

Usage:
  python3 backfill_ground_truth.py             # run both passes
  python3 backfill_ground_truth.py --dry-run   # show what would change
  python3 backfill_ground_truth.py --wu-only   # only the wu_positions pass
"""
import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from observed_temps import get_historical_max_temp  # noqa: E402

DB = "/root/weatherbot/positions.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [backfill] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backfill")

# Cache: (city, date, market_type) -> dict or None
_cache: dict = {}


def fetch_cached(city: str, date: str, market_type: str):
    key = (city, date, market_type)
    if key in _cache:
        return _cache[key]
    try:
        result = get_historical_max_temp(city, date, market_type=market_type)
    except Exception as e:
        logger.warning(f"  fetch failed {city} {date} {market_type}: {e}")
        result = None
    _cache[key] = result
    # Be polite to the API
    time.sleep(0.4)
    return result


def backfill_wu_positions(conn: sqlite3.Connection, dry_run: bool) -> dict:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        SELECT id, city, market_date, bucket_type, bucket_low_c, bucket_high_c
        FROM wu_positions
        WHERE market_date < ?
          AND (actual_temp_c IS NULL OR resolved IS NULL)
        ORDER BY market_date, city
        """,
        (today,),
    ).fetchall()
    logger.info(f"wu_positions to backfill: {len(rows)}")

    fetched = 0
    failed = 0
    updated = 0
    won_count = 0
    for r in rows:
        wid, city, mdate, btype, bl, bh = r
        result = fetch_cached(city, mdate, btype)
        if result is None:
            failed += 1
            continue
        fetched += 1
        actual_c = result["temp_c"]

        # Compute resolved: actual_temp_c in [bucket_low_c, bucket_high_c)
        above = bl is None or actual_c >= bl
        below = bh is None or actual_c < bh
        won = 1 if (above and below) else 0
        won_count += won

        if dry_run:
            logger.debug(
                f"  DRY id={wid} {city} {mdate} {btype} actual={actual_c}C "
                f"bucket=[{bl},{bh}] → resolved={won}"
            )
        else:
            conn.execute(
                "UPDATE wu_positions SET actual_temp_c = ?, resolved = ? WHERE id = ?",
                (actual_c, won, wid),
            )
            updated += 1

    if not dry_run:
        conn.commit()

    return {
        "rows_considered": len(rows),
        "api_fetched_or_cached": fetched,
        "api_failed": failed,
        "rows_updated": updated,
        "won": won_count,
        "lost": fetched - won_count,
    }


def backfill_positions(conn: sqlite3.Connection, dry_run: bool) -> dict:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        SELECT id, city, market_date, unit
        FROM positions
        WHERE status IN ('resolved_won', 'resolved_lost')
          AND actual_temp IS NULL
          AND city != 'OnChainDetect'
          AND market_date < ?
          AND market_date != '1970-01-01'
        ORDER BY market_date, city
        """,
        (today,),
    ).fetchall()
    logger.info(f"positions to backfill: {len(rows)}")

    fetched = 0
    failed = 0
    updated = 0
    for r in rows:
        pid, city, mdate, unit = r
        # We don't know market_type from positions alone; default to 'highest'.
        # The observed_temps API returns both min and max via different params,
        # but for ground-truth max temp we use 'highest'. For 'lowest' markets
        # the actual_temp will be the daily max; the caller must understand
        # this. Looking up market_type from a sibling row:
        mtype_row = conn.execute(
            "SELECT market_type FROM positions WHERE id = ?", (pid,)
        ).fetchone()
        market_type = (mtype_row[0] if mtype_row else "highest") or "highest"
        result = fetch_cached(city, mdate, market_type)
        if result is None:
            failed += 1
            continue
        fetched += 1
        actual = result["temp_c"] if (unit or "F").upper() == "C" else result["temp_f"]

        if dry_run:
            logger.debug(
                f"  DRY id={pid} {city} {mdate} → actual={actual} {unit}"
            )
        else:
            conn.execute(
                "UPDATE positions SET actual_temp = ?, actual_temp_source = COALESCE(actual_temp_source, 'backfill') WHERE id = ?",
                (actual, pid),
            )
            updated += 1

    if not dry_run:
        conn.commit()

    return {
        "rows_considered": len(rows),
        "api_fetched_or_cached": fetched,
        "api_failed": failed,
        "rows_updated": updated,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--wu-only", action="store_true")
    p.add_argument("--positions-only", action="store_true")
    args = p.parse_args()

    conn = sqlite3.connect(DB)

    logger.info("=" * 60)
    logger.info(f"Backfill ground truth — {datetime.now().isoformat()}")
    logger.info(f"DB: {DB}  dry_run={args.dry_run}")
    logger.info("=" * 60)

    if not args.positions_only:
        logger.info("--- Pass 1: wu_positions ---")
        wu_stats = backfill_wu_positions(conn, args.dry_run)
        logger.info(f"wu_positions: {wu_stats}")

    if not args.wu_only:
        logger.info("--- Pass 2: positions.actual_temp ---")
        p_stats = backfill_positions(conn, args.dry_run)
        logger.info(f"positions: {p_stats}")

    logger.info(f"Cache size: {len(_cache)} unique (city, date, type) lookups")
    conn.close()


if __name__ == "__main__":
    main()
