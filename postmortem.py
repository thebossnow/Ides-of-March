#!/usr/bin/env python3
"""
postmortem.py — Post-Resolution Strategy Checker

Analyzes resolved positions to determine WHY they won or lost.
Classifies each outcome against per-model forecast snapshots stored at entry time.

Run:  python3 postmortem.py          (process all unresolved verdicts)
      python3 postmortem.py --all    (re-analyze all resolved positions)
      python3 postmortem.py --id 128 (analyze a specific position)
"""

import json
import math
import sqlite3
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("postmortem")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [postmortem] %(message)s",
)

DB_FILE = Path(__file__).resolve().parent / "positions.db"

# ── Classification thresholds ────────────────────────────────────────────
THIN_EDGE_THRESHOLD = 0.10       # edge below this = coin flip territory
MODEL_DISAGREEMENT_GAP = 0.20    # top-2 models differ by >20% prob = split
SIGMA_ANOMALY = 2.0              # actual temp >2σ from ensemble mean = anomaly

# Default model sigma (t-distribution scale parameter, in °C)
DEFAULT_SIGMA_C = 1.8


# ── Helpers ──────────────────────────────────────────────────────────────

def _get_conn():
    """SQLite connection in WAL mode."""
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def classify_resolution(position: sqlite3.Row) -> dict:
    """
    Analyze a resolved position and return verdict + report.

    Returns: {"verdict": str, "report": str}
    """
    outcome = position["outcome"]  # YES or NO
    actual_temp_c = position["actual_temp"]
    entry_price = position["entry_price"]
    edge = position["edge"]
    slug = position["slug"]
    city = position["city"]
    market_date = position["market_date"]
    unit = position["unit"]
    bucket_low = position["bucket_low"]
    bucket_high = position["bucket_high"]
    pnl = position["pnl_usdc"]
    snapshot_raw = position["model_snapshot"]

    # ── Determine whether actual temp fell in the traded bucket ─────────
    if actual_temp_c is None:
        return {
            "verdict": "no_ground_truth",
            "report": f"No actual temperature recorded for {city} {market_date}. "
                      f"Resolution outcome: {outcome}, PnL: ${pnl:.2f}",
        }

    # Convert actual temp to market unit
    if unit and unit.upper() == "F":
        actual_in_unit = actual_temp_c * 9 / 5 + 32
    else:
        actual_in_unit = actual_temp_c

    # Determine if actual temp fell in the traded bucket
    if outcome == "YES":
        in_bucket = True  # market resolved YES = temp was in bucket
    else:
        in_bucket = False  # market resolved NO = temp was outside bucket

    won = (pnl is not None and pnl > 0)

    # ── No model snapshot available ────────────────────────────────────
    if not snapshot_raw:
        return {
            "verdict": "no_snapshot",
            "report": (
                f"No model snapshot stored at entry time. "
                f"Cannot compare model predictions vs actual. "
                f"Actual: {actual_in_unit:.1f}°{unit if unit else 'C'}, "
                f"Outcome: {'WON' if won else 'LOST'} (${pnl:+.2f})"
            ),
        }

    # ── Parse model snapshot ───────────────────────────────────────────
    try:
        snap = json.loads(snapshot_raw)
    except (json.JSONDecodeError, TypeError):
        return {
            "verdict": "corrupt_snapshot",
            "report": f"Model snapshot JSON corrupted. Cannot analyze.",
        }

    if not snap:
        return {"verdict": "empty_snapshot", "report": "Snapshot exists but contains no model data."}

    # ── Compute ensemble stats ─────────────────────────────────────────
    temps_c = [d["temp_c"] for d in snap.values() if "temp_c" in d]
    probs = [d["prob"] for d in snap.values() if "prob" in d]

    if not temps_c:
        return {"verdict": "no_temp_data", "report": "Snapshot has no temperature data."}

    ensemble_mean_c = sum(temps_c) / len(temps_c)
    ensemble_mean_unit = ensemble_mean_c * 9 / 5 + 32 if (unit and unit.upper() == "F") else ensemble_mean_c

    # Standard deviation of model temps (sample σ)
    if len(temps_c) >= 2:
        variance = sum((t - ensemble_mean_c) ** 2 for t in temps_c) / (len(temps_c) - 1)
        model_sigma_c = math.sqrt(variance)
    else:
        model_sigma_c = DEFAULT_SIGMA_C

    # ── Per-model analysis ─────────────────────────────────────────────
    model_lines = []
    for label, d in sorted(snap.items()):
        tc = d.get("temp_c", 0)
        tf = tc * 9 / 5 + 32 if (unit and unit.upper() == "F") else tc
        prob = d.get("prob", 0)
        delta = actual_temp_c - tc
        model_lines.append(
            f"  {label}: {tc:.1f}°C / {tf:.1f}°{unit if unit else 'C'} "
            f"(prob={prob:.3f}, Δ={delta:+.1f}°C)"
        )

    # ── Determine that model closest to actual ──────────────────────────
    closest_model = min(snap.items(), key=lambda kv: abs(kv[1].get("temp_c", 0) - actual_temp_c))
    closest_label = closest_model[0]
    closest_temp = closest_model[1].get("temp_c", 0)
    closest_delta = actual_temp_c - closest_temp

    # ── Classify ───────────────────────────────────────────────────────
    dev_from_mean = abs(actual_temp_c - ensemble_mean_c)
    is_anomaly = dev_from_mean > (SIGMA_ANOMALY * max(model_sigma_c, DEFAULT_SIGMA_C))

    if probs and len(probs) >= 2:
        sorted_probs = sorted(probs, reverse=True)
        top2_gap = sorted_probs[0] - sorted_probs[1]
        model_disagreement = top2_gap > MODEL_DISAGREEMENT_GAP
    else:
        model_disagreement = False

    thin_edge = abs(edge) < THIN_EDGE_THRESHOLD if edge is not None else True

    all_models_wrong = all(
        abs(d.get("temp_c", 0) - actual_temp_c) > (2 * model_sigma_c)
        for d in snap.values()
    )

    # ── Verdict determination ──────────────────────────────────────────
    if is_anomaly:
        verdict = "weather_anomaly"
        reason = (
            f"Actual {actual_in_unit:.1f}°{unit if unit else 'C'} deviated "
            f"{dev_from_mean:.1f}°C from ensemble mean ({ensemble_mean_c:.1f}°C) — "
            f"exceeds {SIGMA_ANOMALY}σ threshold ({model_sigma_c:.1f}°C model σ). "
            f"Statistical outlier — no model could reasonably predict this."
        )
    elif all_models_wrong:
        verdict = "models_wrong"
        reason = (
            f"All models missed: actual {actual_in_unit:.1f}°{unit if unit else 'C'} "
            f"was >2σ from every model prediction. Ensemble mean: {ensemble_mean_unit:.1f}°{unit if unit else 'C'}. "
            f"Closest model ({closest_label}) was off by {abs(closest_delta):.1f}°C."
        )
    elif thin_edge:
        verdict = "thin_edge"
        reason = (
            f"Edge at entry was only {edge:.1%} — below {THIN_EDGE_THRESHOLD:.0%} threshold. "
            f"Outcome was essentially a coin flip."
        )
    elif model_disagreement:
        verdict = "model_disagreement"
        reason = (
            f"Top-2 models disagreed by {top2_gap:.1%} probability. "
            f"Ensemble was uncertain — high variance across models."
        )
    elif won:
        verdict = "models_correct"
        reason = (
            f"Ensemble correctly predicted the outcome. "
            f"Mean forecast: {ensemble_mean_unit:.1f}°{unit if unit else 'C'}, "
            f"actual: {actual_in_unit:.1f}°{unit if unit else 'C'}. "
            f"Closest model: {closest_label} (Δ={closest_delta:+.1f}°C)."
        )
    else:
        verdict = "within_range"
        reason = (
            f"Lost despite reasonable edge ({edge:.1%}) and model agreement. "
            f"Ensemble mean: {ensemble_mean_unit:.1f}°{unit if unit else 'C'}, "
            f"actual: {actual_in_unit:.1f}°{unit if unit else 'C'}. "
            f"Within normal forecast variance — bad luck within expected range."
        )

    # ── Build report ────────────────────────────────────────────────────
    bucket_desc = f"[{bucket_low},{bucket_high}]" if bucket_high is not None else f"[{bucket_low},∞)"
    verdict_emoji = {
        "models_correct": "\u2705",
        "models_wrong": "\u274c",
        "weather_anomaly": "\u26a1",
        "thin_edge": "\U0001fa99",
        "model_disagreement": "\U0001f500",
        "within_range": "\U0001f3b2",
    }

    report_lines = [
        f"{verdict_emoji.get(verdict, '?')} Post-Mortem: {slug}",
        f"  City: {city} | Date: {market_date} | Bucket: {bucket_desc}{unit if unit else 'C'}",
        f"  Outcome: {'WON' if won else 'LOST'} | PnL: ${pnl:+.2f} @ ${entry_price:.3f}",
        f"  Verdict: {verdict}",
        f"  Reason: {reason}",
        f"  Actual temp: {actual_in_unit:.1f}°{unit if unit else 'C'} ({actual_temp_c:.1f}°C)",
        f"  Ensemble mean: {ensemble_mean_unit:.1f}°{unit if unit else 'C'} "
        f"(σ={model_sigma_c:.1f}°C, deviation={dev_from_mean:.1f}°C)",
        f"  Edge at entry: {edge:.1%}" if edge is not None else "  Edge: unknown",
        "",
        "  Per-model forecasts:",
    ] + model_lines

    return {
        "verdict": verdict,
        "report": "\n".join(report_lines),
    }


# ── Main ─────────────────────────────────────────────────────────────────

def run_postmortem(position_ids=None, reprocess_all=False):
    """Analyze resolved positions and store verdicts."""
    conn = _get_conn()

    if position_ids:
        placeholders = ",".join("?" * len(position_ids))
        rows = conn.execute(
            f"SELECT * FROM positions WHERE id IN ({placeholders}) AND status LIKE 'resolved_%'",
            position_ids,
        ).fetchall()
    elif reprocess_all:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status LIKE 'resolved_%' AND actual_temp IS NOT NULL"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status LIKE 'resolved_%' "
            "AND actual_temp IS NOT NULL "
            "AND (resolution_verdict IS NULL OR resolution_verdict = '')"
        ).fetchall()

    if not rows:
        logger.info("No resolved positions to analyze.")
        conn.close()
        return

    logger.info(f"Analyzing {len(rows)} resolved position(s)...")

    analyzed = 0
    for row in rows:
        result = classify_resolution(row)
        conn.execute(
            "UPDATE positions SET resolution_verdict = ?, resolution_report = ? WHERE id = ?",
            (result["verdict"], result["report"], row["id"]),
        )
        analyzed += 1
        logger.info(f"  ID={row['id']} {row['slug'][:50]}... → {result['verdict']}")

    conn.commit()
    conn.close()

    logger.info(f"Done. {analyzed} position(s) classified.")

    # Print last report for quick visibility
    if analyzed > 0:
        print("\n" + "=" * 70)
        print(result["report"])
        print("=" * 70)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Post-Resolution Strategy Checker")
    parser.add_argument("--all", action="store_true", help="Re-analyze all resolved positions")
    parser.add_argument("--id", type=int, nargs="+", help="Analyze specific position ID(s)")
    args = parser.parse_args()

    run_postmortem(
        position_ids=args.id if args.id else None,
        reprocess_all=args.all,
    )
