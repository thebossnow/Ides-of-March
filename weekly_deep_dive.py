#!/usr/bin/env python3
"""
weekly_deep_dive.py — Weekly Trade Pattern Analysis & Telegram Report

Runs every Sunday at 02:00 UTC.
Aggregates 7 days of resolved trade data:
  - Win rate by city, forecast source, edge bucket
  - Calibration analysis (forecast_prob vs actual win rate)
  - City performance ranking (tradable → watch-only → blocked candidates)
  - WU pathway analysis (cap effectiveness, false edge rate)
  - Recommendations for strategy adjustments

Usage: python3 weekly_deep_dive.py [--dry-run]
"""

import os
import sys
import sqlite3
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
import html

# ── Paths ──────────────────────────────────────────────────────────────────
DB_FILE = Path(__file__).resolve().parent / "positions.db"
ENV_FILE = Path(__file__).resolve().parent / ".env"

from dotenv import load_dotenv
load_dotenv(str(ENV_FILE))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from notifier import TelegramNotifier
    notifier = TelegramNotifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID) if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else None
except Exception as e:
    print(f"[WARN] Telegram notifier unavailable: {e}")
    notifier = None

WATCH_ONLY_CITIES = {
    "Hong Kong", "Seoul", "Miami", "San Francisco", "Seattle", "Chicago",
    "Denver", "Atlanta", "Panama City", "Helsinki", "Warsaw", "Wuhan",
    "Wellington", "NYC", "Lucknow", "Moscow", "Chongqing", "Mexico City",
}


def get_conn():
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn


def query_last_7d_resolved():
    conn = get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = conn.execute(
        """
        SELECT id, city, market_date, entry_price, size_usdc, pnl_usdc,
               actual_temp, bucket_low, bucket_high, unit, exit_reason,
               forecast_prob, market_prob, edge, outcome, status,
               resolved_time, resolution_verdict, market_type
        FROM positions
        WHERE status IN ('resolved_won', 'resolved_lost')
          AND resolved_time >= ?
        ORDER BY resolved_time ASC
        """,
        (cutoff,),
    ).fetchall()
    conn.close()
    return rows


def query_all_time_by_city():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT city,
               COUNT(*) as total,
               SUM(CASE WHEN status='resolved_won' AND pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN status='resolved_lost' AND pnl_usdc < 0 THEN 1 ELSE 0 END) as losses,
               SUM(pnl_usdc) as total_pnl,
               AVG(pnl_usdc) as avg_pnl
        FROM positions
        WHERE status IN ('resolved_won', 'resolved_lost')
          AND pnl_usdc IS NOT NULL
        GROUP BY city
        HAVING total >= 3
        ORDER BY total_pnl ASC
        """,
    ).fetchall()
    conn.close()
    return rows


def calibration_analysis(resolved):
    """Bin trades by forecast_prob and compare to actual win rate."""
    bins = {
        "20-30%": (0.20, 0.30),
        "30-40%": (0.30, 0.40),
        "40-50%": (0.40, 0.50),
        "50-60%": (0.50, 0.60),
        "60-70%": (0.60, 0.70),
        "70-80%": (0.70, 0.80),
        "80%+":   (0.80, 1.01),
    }
    result = {}
    for label, (lo, hi) in bins.items():
        bucket = [r for r in resolved if r["forecast_prob"] is not None and lo <= r["forecast_prob"] < hi]
        if not bucket:
            continue
        wins = sum(1 for r in bucket if r["status"] == "resolved_won" and (r["pnl_usdc"] or 0) > 0)
        total = len(bucket)
        actual_wr = wins / total if total > 0 else 0
        avg_forecast = sum(r["forecast_prob"] for r in bucket) / total
        result[label] = {
            "count": total,
            "wins": wins,
            "actual_wr": actual_wr,
            "avg_forecast": avg_forecast,
            "calibration_gap": avg_forecast - actual_wr,
        }
    return result


def wu_analysis(resolved):
    """Analyze WU pathway trades (forecast_prob capped at 0.40)."""
    wu_trades = [
        r for r in resolved
        if r["forecast_prob"] is not None
        and 0.35 <= r["forecast_prob"] <= 0.45  # Capped range
        and r["market_prob"] is not None
        and r["market_prob"] < 0.20  # Low market price = market disagrees
    ]
    if not wu_trades:
        return None

    wins = sum(1 for r in wu_trades if r["status"] == "resolved_won" and (r["pnl_usdc"] or 0) > 0)
    total = len(wu_trades)
    total_pnl = sum(r["pnl_usdc"] or 0 for r in wu_trades)
    return {
        "count": total,
        "wins": wins,
        "win_rate": wins / total if total > 0 else 0,
        "total_pnl": total_pnl,
        "avg_entry": sum(r["market_prob"] or 0 for r in wu_trades) / total,
    }


def edge_bucket_analysis(resolved):
    """Analyze win rate by edge bucket."""
    buckets = {
        "under 10%":  (0, 0.10),
        "10-20%": (0.10, 0.20),
        "20-30%": (0.20, 0.30),
        "30-50%": (0.30, 0.50),
        "50%+":   (0.50, 1.01),
    }
    result = {}
    for label, (lo, hi) in buckets.items():
        bucket = [r for r in resolved if r["edge"] is not None and lo <= r["edge"] < hi]
        if not bucket:
            continue
        wins = sum(1 for r in bucket if r["status"] == "resolved_won" and (r["pnl_usdc"] or 0) > 0)
        total = len(bucket)
        pnl = sum(r["pnl_usdc"] or 0 for r in bucket)
        result[label] = {"count": total, "wins": wins, "pnl": pnl}
    return result


def city_performance_ranking(all_time):
    """Identify cities for status changes."""
    block_candidates = []
    watch_candidates = []
    promote_candidates = []

    for r in all_time:
        total = r["total"]
        wins = r["wins"]
        losses = r["losses"]
        pnl = r["total_pnl"] or 0
        wr = wins / (wins + losses) if (wins + losses) > 0 else 0
        city = r["city"]

        if city in WATCH_ONLY_CITIES:
            continue  # Already watch-only

        # Block candidate: ≥5 trades, WR <20%, PnL < -$15
        if total >= 5 and wr < 0.20 and pnl < -15:
            block_candidates.append((city, total, wr, pnl))

        # Watch candidate: ≥5 trades, WR <33%, PnL < -$10
        elif total >= 5 and wr < 0.33 and pnl < -10:
            watch_candidates.append((city, total, wr, pnl))

        # Promote candidate: ≥5 trades, WR >50%, PnL > $20
        elif total >= 5 and wr > 0.50 and pnl > 20:
            promote_candidates.append((city, total, wr, pnl))

    return block_candidates, watch_candidates, promote_candidates


def build_report(resolved, all_time):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    week_start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    lines = [f"📈 **Weekly Deep Dive** — {week_start} to {now}"]
    lines.append("")

    if not resolved:
        lines.append("No trades resolved in the last 7 days.")
        return "\n".join(lines)

    # ── Overall stats ──────────────────────────────────────────────────
    wins = [r for r in resolved if r["status"] == "resolved_won" and (r["pnl_usdc"] or 0) > 0]
    losses = [r for r in resolved if r["status"] == "resolved_lost" and (r["pnl_usdc"] or 0) < 0]
    total_pnl = sum(r["pnl_usdc"] or 0 for r in resolved)
    total_count = len(wins) + len(losses)
    wr = f"{len(wins)}/{total_count}" if total_count > 0 else "N/A"

    lines.append(f"**7d Summary:** {total_count} resolved | WR {wr} | PnL {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")
    lines.append("")

    # ── Calibration ────────────────────────────────────────────────────
    cal = calibration_analysis(resolved)
    if cal:
        lines.append("**Calibration (forecast vs actual WR):**")
        for label, data in cal.items():
            gap = data["calibration_gap"]
            gap_str = f"+{gap:.0%}" if gap >= 0 else f"{gap:.0%}"
            emoji = "🔴" if gap > 0.15 else "🟡" if gap > 0.05 else "🟢"
            lines.append(f"  {emoji} {label}: forecast={data['avg_forecast']:.0%} actual={data['actual_wr']:.0%} (gap={gap_str}, n={data['count']})")
        lines.append("")

    # ── WU pathway analysis ────────────────────────────────────────────
    wu = wu_analysis(resolved)
    if wu:
        lines.append(f"**WU Pathway (capped prob, low market price):**")
        lines.append(f"  Trades: {wu['count']} | WR: {wu['win_rate']:.0%} | PnL: ${wu['total_pnl']:.2f}")
        lines.append(f"  Avg entry: {wu['avg_entry']:.1%}")
        if wu['win_rate'] < 0.30:
            lines.append(f"  🔴 WU cap is creating false edges — consider lowering WU_MAX_PROB or adding market_prob floor")
        lines.append("")

    # ── Edge bucket analysis ───────────────────────────────────────────
    edge_data = edge_bucket_analysis(resolved)
    if edge_data:
        lines.append("**Edge Bucket Performance:**")
        for label, data in edge_data.items():
            wr_str = f"{data['wins']}/{data['count']}"
            pnl_str = f"+${data['pnl']:.2f}" if data['pnl'] >= 0 else f"-${abs(data['pnl']):.2f}"
            lines.append(f"  {label}: {data['count']} trades, WR {wr_str}, PnL {pnl_str}")
        lines.append("")

    # ── City performance ───────────────────────────────────────────────
    block, watch, promote = city_performance_ranking(all_time)

    if block:
        lines.append("🔴 **Block Candidates** (WR<20%, PnL<-$15, ≥5 trades):")
        for city, total, wr_rate, pnl in block:
            lines.append(f"  • {city}: {total} trades, WR {wr_rate:.0%}, PnL ${pnl:.2f}")
        lines.append("")

    if watch:
        lines.append("🟡 **Watch-Only Candidates** (WR<33%, PnL<-$10, ≥5 trades):")
        for city, total, wr_rate, pnl in watch:
            lines.append(f"  • {city}: {total} trades, WR {wr_rate:.0%}, PnL ${pnl:.2f}")
        lines.append("")

    if promote:
        lines.append("🟢 **Promote Candidates** (WR>50%, PnL>$20, ≥5 trades):")
        for city, total, wr_rate, pnl in promote:
            lines.append(f"  • {city}: {total} trades, WR {wr_rate:.0%}, PnL ${pnl:.2f}")
        lines.append("")

    # ── Top losers ─────────────────────────────────────────────────────
    worst_cities = sorted(all_time, key=lambda r: r["total_pnl"] or 0)[:5]
    lines.append("**Worst Cities (all-time, ≥3 trades):**")
    for r in worst_cities:
        wr_rate = r["wins"] / (r["wins"] + r["losses"]) if (r["wins"] + r["losses"]) > 0 else 0
        lines.append(f"  • {r['city']}: ${r['total_pnl']:.2f} | WR {wr_rate:.0%} | {r['total']} trades")
    lines.append("")

    # ── Top winners ────────────────────────────────────────────────────
    best_cities = sorted(all_time, key=lambda r: r["total_pnl"] or 0, reverse=True)[:5]
    lines.append("**Best Cities (all-time, ≥3 trades):**")
    for r in best_cities:
        wr_rate = r["wins"] / (r["wins"] + r["losses"]) if (r["wins"] + r["losses"]) > 0 else 0
        lines.append(f"  • {r['city']}: ${r['total_pnl']:.2f} | WR {wr_rate:.0%} | {r['total']} trades")

    return "\n".join(lines)


def main():
    dry_run = "--dry-run" in sys.argv

    resolved = query_last_7d_resolved()
    all_time = query_all_time_by_city()

    report = build_report(resolved, all_time)

    if dry_run:
        print(report)
        return

    if notifier:
        try:
            # Split long reports into multiple messages (Telegram limit ~4096 chars)
            MAX_LEN = 4000
            if len(report) <= MAX_LEN:
                notifier.send_message(html.escape(report, quote=False))
            else:
                # Split on double newlines (sections)
                sections = report.split("\n\n")
                chunk = ""
                for section in sections:
                    if len(chunk) + len(section) + 2 > MAX_LEN:
                        notifier.send_message(html.escape(chunk, quote=False))
                        chunk = section
                    else:
                        chunk = chunk + "\n\n" + section if chunk else section
                if chunk:
                    notifier.send_message(chunk)
            print(f"[OK] Weekly deep dive sent ({len(resolved)} trades, {len(all_time)} cities)")
        except Exception as e:
            print(f"[ERROR] Failed to send: {e}")
            print(report[:2000])
    else:
        print("[WARN] No Telegram notifier. Printing:")
        print(report[:3000])


if __name__ == "__main__":
    main()
