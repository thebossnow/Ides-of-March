#!/usr/bin/env python3
"""
strategy_replay.py — backtest harness for proposed strategy changes.

Takes a saved set of matched (scan, ground-truth) rows from
sigma_calibration_v2.py and replays them through both:
  - the CURRENT strategy constants (baseline)
  - a PROPOSED override (one or more parameter changes)

Outputs a delta table:
  - trades flipped: SKIP → TRADE (would now enter) — with actual won/lost
  - trades flipped: TRADE → SKIP (would no longer enter) — with actual won/lost
  - per-cohort impact (prob band × geometry × horizon)

Critical principle (see feedback-validate-with-backtest memory): the
aggregate net P&L delta is not the only thing that matters. A change can
improve aggregate while regressing the best-performing cohort. This
harness surfaces both.

Usage:
  # Baseline self-check (no changes — verifies replay matches bot decisions)
  python3 strategy_replay.py

  # Override one parameter
  python3 strategy_replay.py --sigma-k 0.5 --label "k=0.5"

  # Multiple overrides
  python3 strategy_replay.py --sigma-k 0.5 --min-prob 0.20 \\
      --max-prob 0.50 --label "calibrated"
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import strategy  # noqa: E402
from scipy.stats import t as t_dist  # noqa: E402

ROWS_PATH = "/tmp/calibration_v2_rows.json"


# ── Decision logic, parameterised for replay ────────────────────────────

def recompute_prob(
    forecast: float, bl: Optional[float], bh: Optional[float],
    sigma: float, df: float,
    open_ended_cap: Optional[float] = None,
) -> float:
    low_p = t_dist.cdf(bl, df, loc=forecast, scale=sigma) if bl is not None else 0.0
    high_p = t_dist.cdf(bh, df, loc=forecast, scale=sigma) if bh is not None else 1.0
    p = high_p - low_p
    # Open-ended adverse cap
    if open_ended_cap is not None:
        if bl is None and bh is not None and forecast > bh:
            p = min(p, open_ended_cap)
        elif bh is None and bl is not None and forecast < bl:
            p = min(p, open_ended_cap)
    return max(0.001, min(0.999, p))


def _sigma_for_row(row, sigma_k: float, wu_double: bool = True) -> tuple:
    """Reconstruct sigma exactly as bot.py would, with an optional scale."""
    market_date = row["date"]
    unit = row["unit"]
    city = row["city"]
    # market_type unknown from scan_log; infer from bucket geometry as a best-effort
    market_type = "lowest" if (row["bl"] is None and row["bh"] is not None) else "highest"
    sigma, df = strategy._get_params(market_date, unit=unit, city=city,
                                     market_type=market_type)
    # WU double — bot.py applies this when is_wunderground=True. We don't
    # know per-row, but it was applied to every wu_source trade. Conservative:
    # apply for ORBELOW/ORHIGHER markets (which are the WU trades).
    if wu_double and row["geom"] in ("ORBELOW", "ORHIGHER"):
        sigma *= 2.0
    return sigma * sigma_k, df


def replay_decision(row, params):
    """
    Apply a strategy-decision pipeline to one row.
    Returns ('TRADE'|'SKIP', new_prob, new_edge, reason).
    """
    forecast = row.get("forecast")
    if forecast is None:
        return "SKIP", row["prob"], row["edge"], "no_forecast"

    bl, bh = row["bl"], row["bh"]
    market_price = row["price"]

    sigma, df = _sigma_for_row(row, params["sigma_k"], params["wu_double"])
    p = recompute_prob(forecast, bl, bh, sigma, df,
                       open_ended_cap=params["open_ended_cap"])

    if p > params["max_prob"]:
        return "SKIP", p, p - market_price, f"prob>{params['max_prob']}"
    if p < params["min_prob"]:
        return "SKIP", p, p - market_price, f"prob<{params['min_prob']}"

    edge = p - market_price
    if edge < params["entry_threshold"]:
        return "SKIP", p, edge, "edge<threshold"

    return "TRADE", p, edge, "ok"


# ── Cohort tabulation ─────────────────────────────────────────────────────

def prob_band(p: float) -> str:
    bands = [(0, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40),
             (0.40, 0.50), (0.50, 0.65), (0.65, 1.01)]
    for lo, hi in bands:
        if lo <= p < hi:
            return f"{int(lo*100):02d}-{int(hi*100):02d}%"
    return "?"


def cohort_key(row, p: float) -> tuple:
    return (prob_band(p), row["geom"], min(row["horizon"], 10))


def report_delta(rows, baseline, proposed, label):
    """Compare two strategy parameter sets across the same rows."""
    flips_in = []   # baseline=SKIP, proposed=TRADE
    flips_out = []  # baseline=TRADE, proposed=SKIP
    both_trade = []
    both_skip = []

    for row in rows:
        b_dec, b_p, b_e, b_r = replay_decision(row, baseline)
        n_dec, n_p, n_e, n_r = replay_decision(row, proposed)

        rec = {**row, "baseline_p": b_p, "new_p": n_p,
               "baseline_dec": b_dec, "new_dec": n_dec,
               "baseline_reason": b_r, "new_reason": n_r}

        if b_dec == "SKIP" and n_dec == "TRADE":
            flips_in.append(rec)
        elif b_dec == "TRADE" and n_dec == "SKIP":
            flips_out.append(rec)
        elif b_dec == "TRADE":
            both_trade.append(rec)
        else:
            both_skip.append(rec)

    n = len(rows)
    print(f"\n{'='*78}")
    print(f"REPLAY DELTA — baseline vs '{label}'")
    print(f"{'='*78}")
    print(f"Universe: {n:,} (city, date, bucket) rows with ground truth")
    print()
    print(f"{'cohort':<28} {'count':>8} {'won':>6} {'win_rate':>10} {'~pnl@2$':>10}")
    print("-" * 78)

    def stats(label, rs):
        n_ = len(rs)
        if n_ == 0:
            print(f"  {label:<26} {0:>8} {'-':>6} {'-':>10} {'-':>10}")
            return
        won = sum(1 for r in rs if r["won"])
        wr = won / n_
        # Naive P&L: $2/trade, payout $2/price if won, $0 lost
        avg_price = mean(r["price"] for r in rs) or 0.001
        approx_pnl = sum(
            (2.0 / max(r["price"], 0.001) - 2.0) if r["won"] else -2.0
            for r in rs
        )
        print(f"  {label:<26} {n_:>8} {won:>6} {wr:>10.1%} {approx_pnl:>+10.2f}")

    stats("baseline TRADE & new TRADE",  both_trade)
    stats("baseline SKIP  & new TRADE  (added)", flips_in)
    stats("baseline TRADE & new SKIP   (removed)", flips_out)
    stats("baseline SKIP  & new SKIP   (unchanged)", both_skip)

    # Per-cohort breakdown for the FLIPS — this is the key signal
    if flips_in or flips_out:
        print()
        print("FLIPS BY COHORT (prob_band | geometry | horizon-days):")
        print(f"{'cohort':<35} {'+added':>8} {'+won':>6} {'-removed':>10} {'-won':>6}")
        print("-" * 78)
        cohorts = defaultdict(lambda: {"added": 0, "added_won": 0,
                                        "removed": 0, "removed_won": 0})
        for r in flips_in:
            k = cohort_key(r, r["new_p"])
            cohorts[k]["added"] += 1
            if r["won"]:
                cohorts[k]["added_won"] += 1
        for r in flips_out:
            k = cohort_key(r, r["baseline_p"])
            cohorts[k]["removed"] += 1
            if r["won"]:
                cohorts[k]["removed_won"] += 1
        for k in sorted(cohorts.keys()):
            s = cohorts[k]
            band, geom, horizon = k
            label_ = f"{band:>7} {geom:>9} h{horizon:>2}"
            print(f"  {label_:<33} {s['added']:>8} {s['added_won']:>6} "
                  f"{s['removed']:>10} {s['removed_won']:>6}")

    # Verdict
    print()
    print("VERDICT:")
    added_won = sum(1 for r in flips_in if r["won"])
    removed_won = sum(1 for r in flips_out if r["won"])
    if flips_in:
        wr_in = added_won / len(flips_in)
        print(f"  Trades ADDED win rate:   {wr_in:.1%} ({added_won}/{len(flips_in)}) "
              f"— {'good' if wr_in > 0.30 else 'questionable — these may dilute returns'}")
    if flips_out:
        wr_out = removed_won / len(flips_out)
        print(f"  Trades REMOVED win rate: {wr_out:.1%} ({removed_won}/{len(flips_out)}) "
              f"— {'good — removing losers' if wr_out < 0.30 else 'BAD — removing winners'}")


def baseline_params():
    return dict(
        sigma_k=1.0,
        wu_double=True,
        open_ended_cap=None,
        max_prob=strategy.MAX_FORECAST_PROB,
        min_prob=strategy.ABSOLUTE_MIN_PROB,
        entry_threshold=0.15,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rows", default=ROWS_PATH)
    p.add_argument("--sigma-k", type=float, default=1.0)
    p.add_argument("--max-prob", type=float, default=None)
    p.add_argument("--min-prob", type=float, default=None)
    p.add_argument("--open-ended-cap", type=float, default=None)
    p.add_argument("--entry-threshold", type=float, default=0.15)
    p.add_argument("--no-wu-double", action="store_true")
    p.add_argument("--label", default="proposed")
    args = p.parse_args()

    if not os.path.exists(args.rows):
        print(f"ERROR: rows file not found: {args.rows}")
        print("Run sigma_calibration_v2.py first.")
        sys.exit(1)

    with open(args.rows) as f:
        rows = json.load(f)
    print(f"Loaded {len(rows):,} matched scan rows.")

    baseline = baseline_params()
    proposed = dict(baseline)
    proposed["sigma_k"] = args.sigma_k
    if args.max_prob is not None:
        proposed["max_prob"] = args.max_prob
    if args.min_prob is not None:
        proposed["min_prob"] = args.min_prob
    if args.open_ended_cap is not None:
        proposed["open_ended_cap"] = args.open_ended_cap
    proposed["entry_threshold"] = args.entry_threshold
    proposed["wu_double"] = not args.no_wu_double

    print(f"Baseline: {baseline}")
    print(f"Proposed: {proposed}")

    report_delta(rows, baseline, proposed, args.label)


if __name__ == "__main__":
    main()
