"""
validation_gate.py - Make-or-break validation gate for live trading.

LIVE TRADING IS LOCKED until this gate issues a validation certificate.
executor.py consults live_trading_authorized() at import time; without a
fresh, passing certificate the bot is forced into DRY_RUN regardless of
the DRY_RUN environment variable.

The make-or-break criteria (evaluate_paper_record) on resolved PAPER
trades:

  1. SAMPLE SIZE      n >= 100 resolved paper predictions. With n=10 (the
                      entire live history as of the June audit) luck and
                      skill are indistinguishable.
  2. CALIBRATION      Model Brier score must beat the market's implied
                      probabilities. If we can't out-forecast the book,
                      "edge" is noise and every trade is adverse selection.
  3. PROFITABILITY    Bootstrap one-sided 95% lower bound of mean P&L per
                      trade > 0. A single 52:1 lottery win (Paris) must not
                      be able to carry the record.
  4. DRAWDOWN         Max cumulative paper drawdown <= 20% of paper bankroll.

All four must pass. The certificate expires after 7 days so the edge has
to keep re-proving itself; a stale certificate re-locks live trading.

Usage:
  python3 validation_gate.py --check                # is live trading authorized?
  python3 validation_gate.py --evaluate positions.db  # score paper record, issue cert on pass
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

CERT_PATH = os.getenv(
    "VALIDATION_CERT_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "validation_certificate.json"),
)
CERT_SCHEMA = "ides-validation-v1"
CERT_TTL_DAYS = 7

MIN_RESOLVED_TRADES = 100
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 1503  # deterministic evaluation runs
MAX_DRAWDOWN_FRACTION = 0.20
DEFAULT_PAPER_BANKROLL = 100.0


# ── Statistics ──────────────────────────────────────────────────────────

def brier_score(probs: list[float], outcomes: list[int]) -> float:
    """Mean squared error of probability forecasts against 0/1 outcomes."""
    if not probs:
        return float("nan")
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def bootstrap_lower_bound(values: list[float],
                          iterations: int = BOOTSTRAP_ITERATIONS,
                          confidence: float = 0.95,
                          seed: int = BOOTSTRAP_SEED) -> float:
    """One-sided lower confidence bound of the mean via bootstrap resampling."""
    if not values:
        return float("-inf")
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iterations):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    idx = int((1.0 - confidence) * iterations)
    return means[idx]


def max_drawdown(pnls: list[float]) -> float:
    """Largest peak-to-trough fall of the cumulative P&L curve (positive number)."""
    peak = 0.0
    cumulative = 0.0
    worst = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
    return worst


# ── Make-or-break evaluation ────────────────────────────────────────────

def evaluate_paper_record(records: list[dict],
                          paper_bankroll: float = DEFAULT_PAPER_BANKROLL) -> dict:
    """Score a resolved paper-trading record against the make-or-break criteria.

    Each record: {"model_prob": float, "market_price": float,
                  "won": bool/int, "pnl": float}
    Returns a stats dict with "passed" plus per-criterion detail.
    """
    n = len(records)
    outcomes = [1 if r["won"] else 0 for r in records]
    model_probs = [float(r["model_prob"]) for r in records]
    market_probs = [float(r["market_price"]) for r in records]
    pnls = [float(r["pnl"]) for r in records]

    checks: dict[str, dict] = {}

    checks["sample_size"] = {
        "passed": n >= MIN_RESOLVED_TRADES,
        "n": n,
        "required": MIN_RESOLVED_TRADES,
    }

    b_model = brier_score(model_probs, outcomes)
    b_market = brier_score(market_probs, outcomes)
    checks["calibration_beats_market"] = {
        "passed": n > 0 and b_model < b_market,
        "brier_model": round(b_model, 6) if n else None,
        "brier_market": round(b_market, 6) if n else None,
    }

    lower = bootstrap_lower_bound(pnls)
    checks["profitability"] = {
        "passed": lower > 0.0,
        "mean_pnl": round(sum(pnls) / n, 4) if n else None,
        "bootstrap_lower_95": round(lower, 4) if n else None,
    }

    dd = max_drawdown(pnls)
    checks["drawdown"] = {
        "passed": dd <= paper_bankroll * MAX_DRAWDOWN_FRACTION,
        "max_drawdown": round(dd, 4),
        "limit": round(paper_bankroll * MAX_DRAWDOWN_FRACTION, 4),
    }

    return {
        "passed": all(c["passed"] for c in checks.values()),
        "n_resolved": n,
        "checks": checks,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def records_from_positions_db(db_path: str) -> list[dict]:
    """Load resolved positions as evaluation records.

    Uses forecast_prob (model), market_prob (entry price = market implied
    probability), pnl_usdc, and win/loss status.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT forecast_prob, market_prob, pnl_usdc, status
        FROM positions
        WHERE status IN ('resolved_won', 'resolved_lost')
          AND forecast_prob IS NOT NULL
          AND market_prob IS NOT NULL
          AND pnl_usdc IS NOT NULL
        """
    ).fetchall()
    conn.close()
    return [
        {
            "model_prob": row["forecast_prob"],
            "market_price": row["market_prob"],
            "won": row["status"] == "resolved_won",
            "pnl": row["pnl_usdc"],
        }
        for row in rows
    ]


# ── Certificate lifecycle ───────────────────────────────────────────────

def issue_certificate(stats: dict, path: str = CERT_PATH) -> dict:
    """Write a validation certificate. Refuses if the evaluation failed."""
    if not stats.get("passed"):
        raise ValueError("Refusing to issue certificate: evaluation did not pass")
    now = datetime.now(timezone.utc)
    cert = {
        "schema": CERT_SCHEMA,
        "passed": True,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(days=CERT_TTL_DAYS)).isoformat(),
        "stats": stats,
    }
    with open(path, "w") as f:
        json.dump(cert, f, indent=2)
    return cert


def live_trading_authorized(path: str = CERT_PATH,
                            now: datetime | None = None) -> tuple[bool, str]:
    """Check whether a fresh, passing validation certificate exists.

    Called by executor.py at import time. Any problem — missing file,
    parse error, wrong schema, failed evaluation, expiry — locks live
    trading (fail-safe).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if not os.path.exists(path):
        return False, (
            "no validation certificate — run "
            "'python3 validation_gate.py --evaluate positions.db' after the "
            f"paper campaign passes (n>={MIN_RESOLVED_TRADES} resolved)"
        )
    try:
        with open(path) as f:
            cert = json.load(f)
    except Exception as e:
        return False, f"unreadable validation certificate: {e}"
    if cert.get("schema") != CERT_SCHEMA:
        return False, f"unknown certificate schema: {cert.get('schema')!r}"
    if cert.get("passed") is not True:
        return False, "certificate does not record a passing evaluation"
    try:
        expires = datetime.fromisoformat(cert["expires_at"])
    except Exception:
        return False, "certificate has no valid expiry"
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now >= expires:
        return False, f"certificate expired {cert['expires_at']} — re-run evaluation"
    return True, f"certificate valid until {cert['expires_at']}"


# ── CLI ─────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--check", action="store_true",
                        help="Report whether live trading is currently authorized")
    parser.add_argument("--evaluate", metavar="POSITIONS_DB",
                        help="Evaluate the resolved paper record; issue certificate on pass")
    parser.add_argument("--bankroll", type=float, default=DEFAULT_PAPER_BANKROLL,
                        help="Paper bankroll for the drawdown criterion")
    args = parser.parse_args(argv)

    if args.check:
        ok, reason = live_trading_authorized()
        print(f"LIVE TRADING {'AUTHORIZED' if ok else 'LOCKED'}: {reason}")
        return 0 if ok else 1

    if args.evaluate:
        records = records_from_positions_db(args.evaluate)
        stats = evaluate_paper_record(records, paper_bankroll=args.bankroll)
        print(json.dumps(stats, indent=2))
        if stats["passed"]:
            cert = issue_certificate(stats)
            print(f"\nPASS — certificate issued, expires {cert['expires_at']}")
            return 0
        print("\nFAIL — live trading remains locked. The bot has not proven an edge.")
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
