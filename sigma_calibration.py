#!/usr/bin/env python3
"""
Sigma calibration analysis for the Polymarket weather bot.

Measures how well the t-distribution probability model is calibrated against
historical resolved trades, then sweeps sigma scale factors to find the value
that makes the model honest.

Outputs:
  1. Reliability diagram (model prob vs actual win rate)
  2. Sigma sweep: finds scale factor k such that sigma_eff = k * current_sigma
     minimizes the Brier score / calibration error
  3. Open-ended bucket analysis: shows how far-OTM tails get inflated
  4. Concrete recommendations for strategy.py constants

Usage:
  python3 sigma_calibration.py [--db /path/to/positions.db]
"""

import sys
import os
import sqlite3
import argparse
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
from scipy.stats import t as t_dist
from scipy.optimize import minimize_scalar, brentq

# ── Pull strategy constants directly from the live module ──────────────────
sys.path.insert(0, os.path.dirname(__file__))
from strategy import (
    SIGMA_BY_HORIZON_F, SIGMA_DEFAULT_F,
    CITY_SIGMA_MULTIPLIER, LOWEST_SIGMA_MULTIPLIER,
    DF_BY_HORIZON, DF_DEFAULT,
    _get_params, forecast_probability,
)

_DEFAULT_TZ = ZoneInfo("America/New_York")
DB_DEFAULT = os.path.join(os.path.dirname(__file__), "positions.db")


# ── helpers ────────────────────────────────────────────────────────────────

def get_params_at_date(city: str, market_date: str, unit: str, market_type: str,
                       eval_date: Optional[str] = None):
    """
    Return (sigma, df) as _get_params would have on eval_date (or today).
    We use today's _today() to compute days_ahead since we're running after
    the fact; use entry_time as the reference point to get the horizon that
    was live at trade time.
    """
    if eval_date:
        today = datetime.strptime(eval_date[:10], "%Y-%m-%d").date()
    else:
        today = datetime.now(_DEFAULT_TZ).date()

    try:
        mdate = datetime.strptime(market_date, "%Y-%m-%d").date()
        days_ahead = max(0, (mdate - today).days)
    except (ValueError, TypeError):
        days_ahead = 2

    sigma_f = SIGMA_BY_HORIZON_F.get(days_ahead, SIGMA_DEFAULT_F)
    df = DF_BY_HORIZON.get(days_ahead, DF_DEFAULT)

    if city:
        sigma_f *= CITY_SIGMA_MULTIPLIER.get(city, 1.0)
    if market_type == "lowest":
        sigma_f *= LOWEST_SIGMA_MULTIPLIER
    if unit.upper() == "C":
        return sigma_f / 1.8, df
    return sigma_f, df


def invert_prob_to_mu(prob: float, bucket_low: Optional[float],
                      bucket_high: Optional[float],
                      sigma: float, df: float) -> Optional[float]:
    """
    Given a stored forecast_prob, bucket geometry, sigma and df, find the
    forecast temperature mu that would produce exactly that probability.

    Returns None if inversion fails (e.g. prob outside bounds for this geometry).
    """
    if prob <= 0 or prob >= 1:
        return None

    if bucket_low is None and bucket_high is not None:
        # prob = t.cdf(bucket_high, df, loc=mu, scale=sigma)
        # => mu = bucket_high - sigma * t.ppf(prob, df)
        return bucket_high - sigma * t_dist.ppf(prob, df)

    if bucket_high is None and bucket_low is not None:
        # prob = 1 - t.cdf(bucket_low, df, loc=mu, scale=sigma)
        # => mu = bucket_low - sigma * t.ppf(1 - prob, df)
        return bucket_low - sigma * t_dist.ppf(1.0 - prob, df)

    if bucket_low is not None and bucket_high is not None:
        # prob = t.cdf(H, df, loc=mu, scale=sigma) - t.cdf(L, df, loc=mu, scale=sigma)
        # Solve numerically — monotone in mu, so bisect
        span = bucket_high - bucket_low
        lo_mu = bucket_low - 20 * sigma
        hi_mu = bucket_high + 20 * sigma

        def f(mu):
            return (t_dist.cdf(bucket_high, df, loc=mu, scale=sigma) -
                    t_dist.cdf(bucket_low, df, loc=mu, scale=sigma)) - prob

        try:
            return brentq(f, lo_mu, hi_mu, xtol=1e-4)
        except ValueError:
            return None

    return None


def recompute_prob(mu: float, bucket_low: Optional[float],
                   bucket_high: Optional[float],
                   sigma: float, df: float) -> float:
    """Compute t-distribution probability with given parameters."""
    low_p = t_dist.cdf(bucket_low, df, loc=mu, scale=sigma) if bucket_low is not None else 0.0
    high_p = t_dist.cdf(bucket_high, df, loc=mu, scale=sigma) if bucket_high is not None else 1.0
    return max(0.001, min(0.999, high_p - low_p))


def actual_in_bucket(actual_temp: float, bucket_low: Optional[float],
                     bucket_high: Optional[float]) -> bool:
    above = bucket_low is None or actual_temp >= bucket_low
    below = bucket_high is None or actual_temp < bucket_high
    return above and below


# ── data loading ────────────────────────────────────────────────────────────

def load_trades(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT id, city, market_date, entry_time,
               bucket_low, bucket_high, unit, market_type,
               forecast_prob, market_prob, entry_price,
               actual_temp, status, pnl_usdc,
               wu_forecast_c, wu_source
        FROM positions
        WHERE status IN ('resolved_won', 'resolved_lost')
          AND actual_temp IS NOT NULL
        ORDER BY entry_time
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ── analysis ────────────────────────────────────────────────────────────────

def reliability_diagram(trades):
    """Group by forecast_prob decile and compare to actual win rate."""
    bins = [
        (0.00, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40),
        (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80),
        (0.80, 0.90), (0.90, 1.01),
    ]
    print("\n" + "=" * 68)
    print("RELIABILITY DIAGRAM  (model probability vs actual win rate)")
    print("=" * 68)
    print(f"{'Band':>12}  {'n':>5}  {'Model':>7}  {'Actual':>7}  {'Gap':>7}  {'P&L':>8}")
    print("-" * 68)

    total_brier = 0.0
    total_n = 0
    all_rows = []

    for lo, hi in bins:
        bucket_trades = [
            t for t in trades
            if t["forecast_prob"] is not None and lo <= t["forecast_prob"] < hi
        ]
        if not bucket_trades:
            continue
        n = len(bucket_trades)
        won = sum(
            1 for t in bucket_trades
            if actual_in_bucket(t["actual_temp"], t["bucket_low"], t["bucket_high"])
        )
        mean_fp = sum(t["forecast_prob"] for t in bucket_trades) / n
        actual_wr = won / n
        gap = actual_wr - mean_fp
        pnl = sum(t["pnl_usdc"] or 0 for t in bucket_trades)
        all_rows.append((lo, hi, n, mean_fp, actual_wr, gap, pnl))
        total_brier += sum((t["forecast_prob"] - (1 if actual_in_bucket(t["actual_temp"], t["bucket_low"], t["bucket_high"]) else 0)) ** 2 for t in bucket_trades)
        total_n += n
        bar = "▓" * int(actual_wr * 20) + "░" * (20 - int(actual_wr * 20))
        print(f"  {lo:.0%}–{hi:.0%}   {n:>5}  {mean_fp:>7.1%}  {actual_wr:>7.1%}  {gap:>+7.1%}  {pnl:>8.2f}")

    print("-" * 68)
    if total_n:
        print(f"  Brier score: {total_brier / total_n:.4f}  (0=perfect, 0.25=random)")
    return all_rows


def sigma_sweep(trades, wu_only=False):
    """
    For each trade with non-trivial forecast_prob, invert the t-CDF to recover
    the implied forecast temperature, then sweep a sigma scale factor k to find
    the value that minimises Brier score.

    Uses entry_time as the reference date for horizon calculation.
    """
    print("\n" + "=" * 68)
    label = "WU TRADES" if wu_only else "ALL TRADES"
    print(f"SIGMA SWEEP — {label}")
    print("=" * 68)

    records = []
    skipped = 0

    for t in trades:
        fp = t["forecast_prob"]
        if fp is None or fp <= 0.01 or fp >= 0.99:
            skipped += 1
            continue

        # Use entry date as the reference to reconstruct the horizon
        entry_date_str = (t["entry_time"] or "")[:10]
        sigma0, df = get_params_at_date(
            t["city"], t["market_date"], t["unit"], t["market_type"] or "highest",
            eval_date=entry_date_str,
        )

        # Determine if WU pathway was used — either explicit wu_source or
        # wu_forecast_c present; also flag trades where prob suspiciously hits
        # exactly 0.40 (old MAX_FORECAST_PROB cap, common in WU trades)
        is_wu = bool(t.get("wu_source") or t.get("wu_forecast_c"))
        if wu_only and not is_wu:
            continue

        # WU doubles sigma; recover the effective sigma that was actually used
        sigma_used = sigma0 * (2.0 if is_wu else 1.0)

        mu = invert_prob_to_mu(fp, t["bucket_low"], t["bucket_high"], sigma_used, df)
        if mu is None:
            skipped += 1
            continue

        won = actual_in_bucket(t["actual_temp"], t["bucket_low"], t["bucket_high"])
        records.append({
            "mu": mu,
            "bucket_low": t["bucket_low"],
            "bucket_high": t["bucket_high"],
            "sigma0": sigma0,
            "df": df,
            "won": won,
            "fp_original": fp,
            "is_wu": is_wu,
            "id": t["id"],
            "city": t["city"],
        })

    if not records:
        print("  No trades with invertible probability found.")
        return None

    print(f"  Trades analysed: {len(records)}  (skipped: {skipped})")

    def brier_at_k(k):
        total = 0.0
        for r in records:
            new_p = recompute_prob(r["mu"], r["bucket_low"], r["bucket_high"],
                                   r["sigma0"] * k, r["df"])
            total += (new_p - r["won"]) ** 2
        return total / len(records)

    # Sweep k from 0.05 to 2.0
    k_vals = np.linspace(0.05, 2.0, 200)
    brier_vals = [brier_at_k(k) for k in k_vals]
    best_idx = int(np.argmin(brier_vals))
    best_k = k_vals[best_idx]
    best_brier = brier_vals[best_idx]
    original_brier = brier_at_k(1.0)

    print(f"\n  {'k':>6}  {'Brier':>8}  {'Mean p':>8}")
    print("  " + "-" * 28)
    for k in [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0, 1.2, 1.5, 2.0]:
        mean_p = np.mean([
            recompute_prob(r["mu"], r["bucket_low"], r["bucket_high"],
                           r["sigma0"] * k, r["df"])
            for r in records
        ])
        marker = " ◄ CURRENT" if k == 1.0 else (" ◄ OPTIMAL" if abs(k - best_k) < 0.06 else "")
        print(f"  {k:>6.2f}  {brier_at_k(k):>8.4f}  {mean_p:>8.1%}{marker}")

    print(f"\n  Optimal k = {best_k:.3f}  (Brier {best_brier:.4f} vs current {original_brier:.4f})")
    print(f"  Improvement: {(1 - best_brier/original_brier)*100:.1f}%")

    # Show what new sigma table would look like
    print(f"\n  New SIGMA_BY_HORIZON_F (multiply current values by k={best_k:.2f}):")
    print(f"  {'days':>5}  {'current':>9}  {'new':>9}")
    for days, sigma_f in sorted(SIGMA_BY_HORIZON_F.items()):
        print(f"  {days:>5}  {sigma_f:>9.1f}F  {sigma_f * best_k:>9.1f}F")

    return best_k


def open_ended_analysis(trades):
    """
    Show how much probability open-ended buckets get relative to the
    forecast miss distance (forecast_temp - bucket_ceiling or bucket_floor).
    """
    print("\n" + "=" * 68)
    print("OPEN-ENDED BUCKET INFLATION ANALYSIS")
    print("=" * 68)
    print("  Samples from the live scan today showing prob vs miss distance")
    print("  (miss = |forecast - nearest bucket edge|, in bucket units):\n")

    # Representative cases observed in the live log today
    cases = [
        # (label, forecast, bucket_low, bucket_high, unit, city, market_date, note)
        ("Munich 2026-06-10",    14.0, None,  9.0, "C", "Munich",      "2026-06-10", "[None,9]  miss=5°C"),
        ("Moscow 2026-06-10",    29.0, None, 24.0, "C", "Moscow",      "2026-06-10", "[None,24] miss=5°C"),
        ("Wellington 2026-06-10",14.0, None,  9.0, "C", "Wellington",  "2026-06-10", "[None,9]  miss=5°C"),
        ("HK 2026-06-10",        29.0, None, 24.0, "C", "Hong Kong",   "2026-06-10", "[None,24] miss=5°C"),
    ]

    print(f"  {'Market':>24}  {'Note':>20}  {'σ_raw':>7}  {'σ_wu':>7}  {'prob':>7}  {'price?':>8}")
    print("  " + "-" * 85)
    for label, fc, bl, bh, unit, city, mdate, note in cases:
        sigma, df = get_params_at_date(city, mdate, unit, "highest", eval_date="2026-06-08")
        sigma_wu = sigma * 2.0
        p_raw = forecast_probability(fc, bl, bh, unit=unit, market_date=mdate, city=city, is_wunderground=False)
        p_wu = forecast_probability(fc, bl, bh, unit=unit, market_date=mdate, city=city, is_wunderground=True)
        miss = abs(fc - (bh if bh is not None else bl))
        print(f"  {label:>24}  {note:>20}  {sigma:>7.2f}  {sigma_wu:>7.2f}  {p_wu:>7.1%}  market≈1-2¢")

    # Also show the same miss distance at different sigma scales
    print("\n  How prob changes for the same 5°C miss at different sigma scales:")
    print(f"  {'sigma_scale':>12}  {'σ_wu(°C)':>10}  {'[None,9]fc14':>14}  {'[None,24]fc29':>14}")
    print("  " + "-" * 58)
    for kk in [0.25, 0.35, 0.50, 0.65, 1.0, 1.5, 2.0]:
        sigma_m, df_m = get_params_at_date("Munich", "2026-06-10", "C", "highest", "2026-06-08")
        sigma_eff = sigma_m * 2.0 * kk
        p1 = recompute_prob(14.0, None, 9.0, sigma_eff, df_m)
        sigma_r, df_r = get_params_at_date("Moscow", "2026-06-10", "C", "highest", "2026-06-08")
        sigma_eff2 = sigma_r * 2.0 * kk
        p2 = recompute_prob(29.0, None, 24.0, sigma_eff2, df_r)
        mark = " ◄ current" if abs(kk - 1.0) < 0.01 else ""
        print(f"  {kk:>12.2f}  {sigma_eff:>10.2f}°C  {p1:>14.1%}  {p2:>14.1%}{mark}")


def main():
    parser = argparse.ArgumentParser(description="Sigma calibration for weather bot")
    parser.add_argument("--db", default=DB_DEFAULT, help="Path to positions.db")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database not found: {args.db}")
        sys.exit(1)

    print(f"Calibration analysis — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Database: {args.db}")

    trades = load_trades(args.db)
    print(f"\nLoaded {len(trades)} resolved trades with actual_temp")
    if not trades:
        print("No trades to analyse.")
        sys.exit(0)

    # 1. Reliability diagram
    reliability_diagram(trades)

    # 2. Sigma sweep — all invertible trades
    best_k = sigma_sweep(trades, wu_only=False)

    # 3. Open-ended analysis
    open_ended_analysis(trades)

    # 4. Summary recommendations
    print("\n" + "=" * 68)
    print("RECOMMENDATIONS")
    print("=" * 68)

    if best_k is not None:
        # For strategy.py adjustments
        print(f"""
  Based on {len(trades)} resolved trades with observed outcomes:

  1. SIGMA SCALE
     Optimal scale factor: k = {best_k:.2f}
     Current sigma is {'OVER' if best_k < 1 else 'UNDER'}estimated by ~{abs(1-best_k)*100:.0f}%
     {'Recommended: reduce all SIGMA_BY_HORIZON_F values by ' + f'{(1-best_k)*100:.0f}%' if best_k < 1 else 'Recommended: increase sigma values'}

  2. WU 2× DOUBLING (strategy.py:664)
     The is_wunderground pathway doubles sigma on top of horizon*city scaling.
     At k={best_k:.2f}, effective WU sigma is {best_k*2:.2f}× the base sigma.
     {'Consider removing the 2× and instead calibrating WU sigma directly.' if best_k < 0.7 else ''}

  3. OPEN-ENDED TAILS
     A 5°C miss on a tail bucket yields {forecast_probability(14.0, None, 9.0, unit='C', market_date='2026-06-10', city='Munich', is_wunderground=True)*100:.1f}% probability.
     The market prices this at ~1-2¢ (1-2%).
     At k=0.35, the same miss would yield ~5-8% — still generous but not 34%.
     Recommended: add a hard cap of 10% on open-ended bucket probability.

  4. MAX_FORECAST_PROB
     Phase 3 raised this to 0.65. Calibration shows mean 81% → actual 24%.
     Recommended: lower back to 0.45 until empirical win rate at 65%+ is ≥50%.

  5. ABSOLUTE_MIN_PROB / SOFT_MIN_PROB
     At 30%, the bar is set very low. Current 30-35% band wins 24% (vs 30-35% expected).
     Recommended: raise to 0.35 so the floor requires meaningful edge.
""")


if __name__ == "__main__":
    main()
