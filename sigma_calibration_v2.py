#!/usr/bin/env python3
"""
sigma_calibration_v2.py — unbiased calibration using scan_log + wu_positions.

v1 (sigma_calibration.py) was limited to 97 resolved positions.db trades —
trades that PASSED the bot's edge filter. That's conditional on adverse
selection. v2 uses two unbiased data sources:

  1. scan_log.csv (+ archives) — every market the bot scanned, traded or
     not. Has (city, market_date, bucket_low, bucket_high, unit, model
     prob, market price, decision). 700k+ rows.

  2. wu_positions table — every WU forecast the bot used. Has bucket
     geometry + actual_temp_c (once backfilled).

Outcome is computed by joining (city, market_date) against
positions.actual_temp (after backfill) and wu_positions.actual_temp_c.

Outputs:
  - Reliability diagram across the full unbiased universe
  - Per-cohort breakdown (prob band × bucket geometry × horizon × city ×
    decision) — surfaces where the bot's filter is too tight/loose
  - Saved JSON of all matched (scan_row, won) tuples for downstream
    backtest harness use
"""
import argparse
import csv
import glob
import gzip
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Optional

DB = "/root/weatherbot/positions.db"
SCAN_GLOB = ["/root/weatherbot/scan_log.csv"] + sorted(
    glob.glob("/root/weatherbot/scan_log.csv.*.gz")
)

# scan_log column order (from logger.py SCAN_HEADERS)
COL_TS, COL_SLUG, COL_CITY, COL_DATE = 0, 1, 2, 3
COL_PROB, COL_PRICE, COL_EDGE = 4, 5, 6
COL_DECISION, COL_REASON = 7, 8
COL_ASK, COL_MAXBID = 9, 10
COL_FORECAST_TEMP, COL_UNIT = 11, 12
COL_BL, COL_BH = 13, 14


def fnum(x) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (ValueError, TypeError):
        return None


def load_ground_truth(db: str) -> dict:
    """
    Returns {(city, market_date): {"temp_c": x, "temp_f": y, "source": s}}

    Combines two sources:
      - positions.actual_temp (with unit)
      - wu_positions.actual_temp_c (always celsius)
    """
    conn = sqlite3.connect(db)
    gt = {}

    # wu_positions: take the most recent backfill per (city, date)
    for row in conn.execute(
        """SELECT city, market_date, actual_temp_c FROM wu_positions
           WHERE actual_temp_c IS NOT NULL"""
    ):
        city, mdate, c = row
        gt[(city, mdate)] = {
            "temp_c": c,
            "temp_f": c * 1.8 + 32,
            "source": "wu_positions",
        }

    # positions overrides — these are the gold source (filled by observed_temps)
    for row in conn.execute(
        """SELECT city, market_date, actual_temp, unit FROM positions
           WHERE actual_temp IS NOT NULL AND city != 'OnChainDetect'"""
    ):
        city, mdate, t, u = row
        if (u or "F").upper() == "C":
            gt[(city, mdate)] = {
                "temp_c": t,
                "temp_f": t * 1.8 + 32,
                "source": "positions",
            }
        else:
            gt[(city, mdate)] = {
                "temp_c": (t - 32) / 1.8,
                "temp_f": t,
                "source": "positions",
            }
    conn.close()
    return gt


def actual_in_bucket(actual: float, bl: Optional[float], bh: Optional[float]) -> bool:
    above = bl is None or actual >= bl
    below = bh is None or actual < bh
    return above and below


def horizon_days(scan_ts: str, market_date: str) -> int:
    try:
        s = datetime.fromisoformat(scan_ts[:19]).date()
        m = datetime.strptime(market_date, "%Y-%m-%d").date()
        return max(0, (m - s).days)
    except (ValueError, TypeError):
        return -1


def bucket_geom(bl, bh) -> str:
    if bl is None and bh is not None:
        return "ORBELOW"
    if bh is None and bl is not None:
        return "ORHIGHER"
    if bl is not None and bh is not None:
        return "BOUNDED"
    return "UNBOUNDED"


def load_scans_joined(gt: dict, scan_files: list, max_rows: Optional[int] = None):
    """
    Iterate scan_log rows, join to ground truth, return list of dicts.
    Only emits rows where ground truth exists.
    """
    rows = []
    skipped_no_gt = 0
    skipped_bad = 0
    total = 0
    for fp in scan_files:
        opener = gzip.open if fp.endswith(".gz") else open
        try:
            with opener(fp, "rt", newline="") as f:
                for r in csv.reader(f):
                    if len(r) < 15:
                        skipped_bad += 1
                        continue
                    total += 1
                    if max_rows and total > max_rows:
                        return rows, total, skipped_no_gt, skipped_bad

                    key = (r[COL_CITY], r[COL_DATE])
                    if key not in gt:
                        skipped_no_gt += 1
                        continue

                    bl = fnum(r[COL_BL])
                    bh = fnum(r[COL_BH])
                    unit = (r[COL_UNIT] or "F").upper()
                    actual = gt[key]["temp_c"] if unit == "C" else gt[key]["temp_f"]
                    prob = fnum(r[COL_PROB])
                    if prob is None:
                        skipped_bad += 1
                        continue
                    # ── Critical filter (added 2026-06-08 after H7 obstacle) ──
                    # Rows with no forecast_temp logged are pre-prob skip markers
                    # (bot rejected the market before computing probability —
                    # e.g. WU buffer guard fired). These have prob=0 and price=0
                    # written as defaults, contaminating any prob-based cohort.
                    # 68% of all scan rows are these artifacts; including them
                    # silently faked an 80% win rate in the ORHIGHER 0-10% band.
                    forecast = fnum(r[COL_FORECAST_TEMP])
                    if forecast is None:
                        skipped_bad += 1
                        continue

                    rows.append({
                        "ts": r[COL_TS],
                        "slug": r[COL_SLUG],
                        "city": r[COL_CITY],
                        "date": r[COL_DATE],
                        "prob": prob,
                        "price": fnum(r[COL_PRICE]) or 0.0,
                        "edge": fnum(r[COL_EDGE]) or 0.0,
                        "decision": r[COL_DECISION],
                        "reason": r[COL_REASON],
                        "ask": fnum(r[COL_ASK]),
                        "max_bid": fnum(r[COL_MAXBID]),
                        "forecast": forecast,
                        "unit": unit,
                        "bl": bl,
                        "bh": bh,
                        "geom": bucket_geom(bl, bh),
                        "horizon": horizon_days(r[COL_TS], r[COL_DATE]),
                        "actual": actual,
                        "won": actual_in_bucket(actual, bl, bh),
                    })
        except Exception as e:
            print(f"! error reading {fp}: {e}", file=sys.stderr)
    return rows, total, skipped_no_gt, skipped_bad


def reliability(rows):
    bins = [(i/10, (i+1)/10) for i in range(10)]
    print("\n" + "=" * 78)
    print("RELIABILITY DIAGRAM — UNBIASED (all scans, not just trades)")
    print("=" * 78)
    print(f"{'Band':>12} {'n':>7} {'Model':>7} {'Actual':>7} {'Gap':>7} "
          f"{'BOUNDED':>14} {'ORBELOW':>14} {'ORHIGHER':>14}")
    print("-" * 78)

    brier_sum = 0.0
    for lo, hi in bins:
        bk = [r for r in rows if lo <= r["prob"] < hi]
        if not bk:
            continue
        n = len(bk)
        mp = mean(r["prob"] for r in bk)
        wr = sum(1 for r in bk if r["won"]) / n
        gap = wr - mp
        geoms = Counter(r["geom"] for r in bk)
        wons_by_geom = Counter()
        for r in bk:
            if r["won"]:
                wons_by_geom[r["geom"]] += 1
        def cell(g):
            if geoms[g] == 0:
                return "—"
            return f"{wons_by_geom[g]}/{geoms[g]}({100*wons_by_geom[g]/geoms[g]:.0f}%)"
        brier_sum += sum((r["prob"] - (1 if r["won"] else 0)) ** 2 for r in bk)
        print(f"  {lo:.0%}-{hi:.0%}  {n:>7}  {mp:>7.1%}  {wr:>7.1%}  {gap:>+7.1%}  "
              f"{cell('BOUNDED'):>14} {cell('ORBELOW'):>14} {cell('ORHIGHER'):>14}")
    print("-" * 78)
    if rows:
        print(f"  Brier: {brier_sum/len(rows):.4f}  (n={len(rows)})")


def by_horizon(rows):
    print("\n" + "=" * 60)
    print("BY HORIZON (days ahead)")
    print("=" * 60)
    print(f"{'days':>5}  {'n':>6}  {'mean_prob':>10}  {'win_rate':>9}  {'gap':>7}  {'Brier':>7}")
    print("-" * 60)
    buckets = defaultdict(list)
    for r in rows:
        buckets[r["horizon"]].append(r)
    for h in sorted(buckets.keys()):
        bk = buckets[h]
        n = len(bk)
        mp = mean(r["prob"] for r in bk)
        wr = sum(1 for r in bk if r["won"]) / n
        brier = sum((r["prob"] - (1 if r["won"] else 0))**2 for r in bk) / n
        print(f"  {h:>5}  {n:>6}  {mp:>10.1%}  {wr:>9.1%}  {wr-mp:>+7.1%}  {brier:>7.4f}")


def by_city(rows, top=15):
    print("\n" + "=" * 60)
    print(f"BY CITY (top {top} by sample size)")
    print("=" * 60)
    print(f"{'city':>15}  {'n':>5}  {'mean_p':>7}  {'win_r':>7}  {'gap':>7}")
    print("-" * 60)
    buckets = defaultdict(list)
    for r in rows:
        buckets[r["city"]].append(r)
    rows_out = []
    for city, bk in buckets.items():
        n = len(bk)
        mp = mean(r["prob"] for r in bk)
        wr = sum(1 for r in bk if r["won"]) / n
        rows_out.append((city, n, mp, wr))
    rows_out.sort(key=lambda x: -x[1])
    for city, n, mp, wr in rows_out[:top]:
        print(f"  {city:>15}  {n:>5}  {mp:>7.1%}  {wr:>7.1%}  {wr-mp:>+7.1%}")


def by_geometry(rows):
    print("\n" + "=" * 70)
    print("BY BUCKET GEOMETRY — the key open-ended tail question")
    print("=" * 70)
    print(f"{'geom':>10}  {'n':>6}  {'mean_p':>7}  {'win_r':>7}  {'gap':>7}  "
          f"{'mean_p|prob>20':>14}  {'win_r|prob>20':>14}")
    print("-" * 70)
    buckets = defaultdict(list)
    for r in rows:
        buckets[r["geom"]].append(r)
    for g, bk in sorted(buckets.items()):
        n = len(bk)
        mp = mean(r["prob"] for r in bk)
        wr = sum(1 for r in bk if r["won"]) / n
        hi = [r for r in bk if r["prob"] > 0.20]
        mp_h = mean(r["prob"] for r in hi) if hi else 0
        wr_h = sum(1 for r in hi if r["won"]) / len(hi) if hi else 0
        print(f"  {g:>10}  {n:>6}  {mp:>7.1%}  {wr:>7.1%}  {wr-mp:>+7.1%}  "
              f"{mp_h:>14.1%}  {wr_h:>14.1%}  (n_high={len(hi)})")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB)
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap scan rows read (for fast iteration)")
    p.add_argument("--save", default="/tmp/calibration_v2_rows.json",
                   help="Save matched rows for backtest harness")
    args = p.parse_args()

    print(f"Calibration v2 — {datetime.now()}")
    print(f"DB: {args.db}")
    print(f"Scan files: {len(SCAN_GLOB)}")

    gt = load_ground_truth(args.db)
    print(f"\nGround-truth (city, date) pairs: {len(gt)}")
    if not gt:
        print("ERROR: no ground truth available. Run backfill_ground_truth.py first.")
        sys.exit(1)

    rows, total, no_gt, bad = load_scans_joined(gt, SCAN_GLOB, args.max_rows)
    print(f"\nScan rows considered: {total:,}")
    print(f"  matched to ground truth: {len(rows):,} ({100*len(rows)/max(total,1):.1f}%)")
    print(f"  skipped (no ground truth): {no_gt:,}")
    print(f"  skipped (parse errors):    {bad:,}")
    if not rows:
        print("\nERROR: zero matched rows. Backfill ground truth or check date overlap.")
        sys.exit(1)

    reliability(rows)
    by_horizon(rows)
    by_geometry(rows)
    by_city(rows)

    # Save for downstream backtest harness
    if args.save:
        with open(args.save, "w") as f:
            json.dump(rows, f, indent=2, default=str)
        print(f"\nSaved {len(rows):,} matched rows → {args.save}")


if __name__ == "__main__":
    main()
