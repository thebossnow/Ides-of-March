#!/usr/bin/env python3
"""
daily_trade_review.py — Daily Trade Review & Telegram Report

Runs at 00:30 UTC (after the 00:05 UTC resolution cycle).
Queries all positions resolved in the last 24 hours, classifies wins/losses,
identifies failure patterns, and sends a concise Telegram summary.

Usage: python3 daily_trade_review.py [--dry-run]
"""

import os
import sys
import sqlite3
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
DB_FILE = Path(__file__).resolve().parent / "positions.db"
ENV_FILE = Path(__file__).resolve().parent / ".env"

# ── Load .env ──────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(str(ENV_FILE))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# ── Import Telegram notifier ───────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from notifier import TelegramNotifier
    notifier = TelegramNotifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID) if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else None
except Exception as e:
    print(f"[WARN] Telegram notifier unavailable: {e}")
    notifier = None

# ── Watch-only cities (must match strategy.py) ─────────────────────────────
WATCH_ONLY_CITIES = {
    "Hong Kong", "Seoul", "Miami", "San Francisco", "Seattle", "Chicago",
    "Denver", "Atlanta", "Panama City", "Helsinki", "Warsaw", "Wuhan",
    "Wellington", "NYC", "Lucknow", "Moscow", "Chongqing", "Mexico City",
}


def get_conn():
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn


def query_last_24h_resolved():
    """Get all positions resolved in the last 24 hours."""
    conn = get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = conn.execute(
        """
        SELECT id, city, market_date, entry_price, size_usdc, pnl_usdc,
               actual_temp, bucket_low, bucket_high, unit, exit_reason,
               forecast_prob, market_prob, edge, outcome, status,
               resolved_time, market_type
        FROM positions
        WHERE status IN ('resolved_won', 'resolved_lost')
          AND resolved_time >= ?
        ORDER BY resolved_time ASC
        """,
        (cutoff,),
    ).fetchall()
    conn.close()
    return rows


def query_open_positions():
    """Get current open positions for exposure summary."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, city, market_date, entry_price, size_usdc, pnl_usdc,
               forecast_prob, market_prob, edge, market_type
        FROM positions
        WHERE status = 'open'
        ORDER BY market_date ASC
        """,
    ).fetchall()
    conn.close()
    return rows


def classify_loss(row):
    """Classify WHY a trade was lost. Returns (category, detail)."""
    fp = row["forecast_prob"]
    mp = row["market_prob"]
    edge = row["edge"]
    city = row["city"]
    exit_reason = row["exit_reason"] or ""
    actual = row["actual_temp"]
    bucket_low = row["bucket_low"]
    bucket_high = row["bucket_high"]
    unit = row["unit"] or "F"

    # Category 1: Watch-only city leak
    if city in WATCH_ONLY_CITIES:
        return "WATCH_ONLY_LEAK", f"{city} is watch-only but was traded"

    # Category 2: Market closed / orderbook gone
    if "market_closed" in exit_reason or "orderbook_gone" in exit_reason:
        return "MARKET_CLOSED", f"Market closed before exit"

    # Category 3: WU prob cap false edge
    if fp is not None and mp is not None and fp >= 0.35 and mp < 0.15:
        return "WU_FALSE_EDGE", f"forecast={fp:.0%} vs market={mp:.0%} — cap inflated edge"

    # Category 4: Low market prob (market knew)
    if mp is not None and mp < 0.05:
        return "MARKET_KNEW", f"market priced at {mp:.1%} — outcome was unlikely"

    # Category 5: Edge collapse (was good, went bad)
    if "edge_collapse" in exit_reason:
        return "EDGE_COLLAPSE", f"Edge collapsed before exit"

    # Category 6: Forecast was wrong
    if actual is not None and bucket_low is not None and bucket_high is not None:
        if unit.upper() == "F":
            # Convert actual from C to F for comparison
            actual_f = actual * 9 / 5 + 32
        else:
            actual_f = actual
        if bucket_low is not None and actual_f < bucket_low:
            return "FORECAST_HIGH", f"actual={actual_f:.1f}{unit} below bucket [{bucket_low},{bucket_high}]"
        if bucket_high is not None and actual_f > bucket_high:
            return "FORECAST_LOW", f"actual={actual_f:.1f}{unit} above bucket [{bucket_low},{bucket_high}]"

    # Category 7: Delayed exit
    if "delayed" in exit_reason.lower():
        return "DELAYED_EXIT", f"Exit was delayed"

    return "OTHER", exit_reason[:60] if exit_reason else "No exit reason"


def build_report(resolved, open_positions):
    """Build the Telegram report message."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"📊 **Daily Trade Review** — {now}"]
    lines.append("")

    if not resolved:
        lines.append("No trades resolved in the last 24h.")
        if open_positions:
            lines.append(f"Open positions: {len(open_positions)}")
        return "\n".join(lines)

    # ── Summary stats ──────────────────────────────────────────────────
    wins = [r for r in resolved if r["status"] == "resolved_won" and (r["pnl_usdc"] or 0) > 0]
    losses = [r for r in resolved if r["status"] == "resolved_lost" and (r["pnl_usdc"] or 0) < 0]
    total_pnl = sum(r["pnl_usdc"] or 0 for r in resolved)
    win_count = len(wins)
    loss_count = len(losses)
    total_count = win_count + loss_count
    wr = f"{win_count}/{total_count}" if total_count > 0 else "N/A"

    lines.append(f"**24h Summary:** {total_count} resolved | WR {wr} | PnL {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")
    lines.append("")

    # ── Wins ───────────────────────────────────────────────────────────
    if wins:
        lines.append(f"✅ **Wins ({win_count}):**")
        for r in wins:
            pnl = r["pnl_usdc"] or 0
            lines.append(f"  • {r['city']} {r['market_date']} | +${pnl:.2f} | entry={r['entry_price']:.3f} | edge={r['edge']:.1%}" if r['edge'] is not None else f"  • {r['city']} {r['market_date']} | +${pnl:.2f}")
        lines.append("")

    # ── Losses with classification ─────────────────────────────────────
    if losses:
        lines.append(f"❌ **Losses ({loss_count}):**")
        loss_categories = {}
        for r in losses:
            cat, detail = classify_loss(r)
            if cat not in loss_categories:
                loss_categories[cat] = []
            loss_categories[cat].append((r, detail))

        for cat, items in loss_categories.items():
            cat_total = sum(r["pnl_usdc"] or 0 for r, _ in items)
            lines.append(f"  📌 **{cat}** ({len(items)} trades, ${cat_total:.2f}):")
            for r, detail in items:
                pnl = r["pnl_usdc"] or 0
                lines.append(f"    • {r['city']} {r['market_date']} | ${pnl:.2f} | {detail}")
        lines.append("")

    # ── Open positions ─────────────────────────────────────────────────
    if open_positions:
        open_exposure = sum(r["size_usdc"] or 0 for r in open_positions)
        lines.append(f"📋 **Open Positions ({len(open_positions)}):** ${open_exposure:.2f} deployed")
        for r in open_positions[:10]:  # Show first 10
            fp_str = f"{r['forecast_prob']:.0%}" if r['forecast_prob'] is not None else "N/A"
            lines.append(f"  • {r['city']} {r['market_date']} | ${r['size_usdc']:.2f} | prob={fp_str}")
        if len(open_positions) > 10:
            lines.append(f"  ... and {len(open_positions) - 10} more")
        lines.append("")

    # ── Flags ──────────────────────────────────────────────────────────
    flags = []
    for r in losses:
        cat, _ = classify_loss(r)
        if cat == "WATCH_ONLY_LEAK":
            flags.append(f"🚨 WATCH_ONLY_LEAK: {r['city']} traded despite watch-only status")
        elif cat == "WU_FALSE_EDGE":
            flags.append(f"⚠️ WU_FALSE_EDGE: {r['city']} {r['market_date']} — cap inflated edge vs market")

    if flags:
        lines.append("**Flags:**")
        for f in flags:
            lines.append(f"  {f}")

    return "\n".join(lines)


def main():
    dry_run = "--dry-run" in sys.argv

    resolved = query_last_24h_resolved()
    open_positions = query_open_positions()

    report = build_report(resolved, open_positions)

    if dry_run:
        print(report)
        return

    if notifier:
        try:
            notifier.send_message(report)
            print(f"[OK] Daily trade review sent to Telegram ({len(resolved)} resolved trades)")
        except Exception as e:
            print(f"[ERROR] Failed to send Telegram message: {e}")
            print(report)
    else:
        print("[WARN] No Telegram notifier available. Printing to stdout:")
        print(report)


if __name__ == "__main__":
    main()
