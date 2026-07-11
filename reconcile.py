#!/usr/bin/env python3
"""
reconcile.py — Position reconciliation service.

Runs every 2h via systemd timer. Independent of the main bot.
1. Fixes zombie positions (status='open' but exit_reason set)
2. Checks Polymarket CLOB for actual token balances / resolution status
3. Updates status to 'exited', 'resolved_lost', or 'resolved_won'
4. Reports true exposure vs bot-reported exposure
5. Sends Telegram alert on discrepancies

Usage:
    python reconcile.py                        # Full run
    python reconcile.py --dry-run              # Preview only
    python reconcile.py --fix-zombies-only     # Only fix status/exited mismatch
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from positions import (
    get_open_positions,
    get_total_open_exposure,
    get_position_by_id,
    record_exit,
    record_resolution,
    get_pnl_summary,
    _get_conn,
)

# ── Config ────────────────────────────────────────────────────────
LOG_FILE = Path(__file__).resolve().parent / "logs" / "reconciler.log"
DUMP_FILE = Path("/opt/botdash/reconciler_status.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [reconciler] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE)),
    ],
)
logger = logging.getLogger("reconciler")


# ── Telegram alert ─────────────────────────────────────────────────
def _send_telegram(message: str) -> bool:
    """Send alert via Telegram. Returns True on success."""
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": int(chat_id),
            "text": f"⚙️ Reconcile\n{message}",
            "parse_mode": "HTML",
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")
        return False


# ── Phase 1: Fix zombie positions ──────────────────────────────────
def fix_zombie_positions(dry_run: bool = False) -> int:
    """
    Positions with status='open' but exit_reason set got stuck.
    Update status to 'exited'.
    Returns count of fixed positions.
    """
    conn = _get_conn()
    zombies = conn.execute(
        "SELECT id, slug, exit_reason, size_usdc FROM positions "
        "WHERE status = 'open' AND exit_reason IS NOT NULL"
    ).fetchall()

    if not zombies:
        logger.info("No zombie positions found.")
        return 0

    logger.info(f"Found {len(zombies)} zombie position(s) (status=open, exit_reason set)")
    for z in zombies:
        logger.info(f"  #{z['id']} {z['slug'][:60]} exit={z['exit_reason'][:50]} size=${z['size_usdc']:.2f}")

    if dry_run:
        logger.info(f"DRY_RUN — would fix {len(zombies)} zombie(s)")
        return 0

    fixed = 0
    for z in zombies:
        try:
            conn.execute(
                "UPDATE positions SET status = 'exited' WHERE id = ?",
                (z["id"],)
            )
            fixed += 1
        except Exception as e:
            logger.error(f"Failed to fix zombie #{z['id']}: {e}")

    conn.commit()
    logger.info(f"Fixed {fixed}/{len(zombies)} zombie positions → status='exited'")
    return fixed


# ── Phase 2: Check CLOB for token resolution ───────────────────────
def _check_token_resolution(token_id: str) -> Optional[dict]:
    """
    Query CLOB for token resolution status.
    Returns dict with 'resolved' (bool), 'winner' (bool), 'price' (float)
    or None if unreachable.
    """
    import requests
    try:
        # Last trade price — if 0 or 1, market is resolved
        resp = requests.get(
            f"https://clob.polymarket.com/last-trade-price?token_id={token_id}",
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        price = float(data.get("price", 0.5))

        if price in (0.0, 1.0):
            return {
                "resolved": True,
                "winner": price == 1.0,
                "price": price,
            }
        elif price == 0.5:
            return {
                "resolved": False,
                "winner": None,
                "price": price,
            }
        else:
            # Partial resolution or stale — market is resolving
            return {
                "resolved": price not in (0.0, 0.5, 1.0),
                "winner": price > 0.5 if price not in (0.0, 0.5, 1.0) else None,
                "price": price,
            }
    except Exception as e:
        logger.debug(f"CLOB check failed for {token_id[:20]}: {e}")
        return None


# ── Phase 3: Full reconciliation ───────────────────────────────────
def reconcile_positions(dry_run: bool = False) -> dict:
    """
    Full reconciliation pass:
    1. Get all truly open positions (status='open', exit_reason IS NULL)
    2. Check each against CLOB
    3. Update resolved positions
    Returns summary dict.
    """
    open_positions = get_open_positions()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Filter to only truly active (no exit_reason, size > 0, past market date)
    active = [p for p in open_positions
              if p.get("exit_reason") is None
              and p.get("size_usdc", 0) > 0
              and p.get("market_date", "2099-01-01") < today]

    logger.info(f"Truly open positions: {len(active)} (filtered from {len(open_positions)} total)")

    if not active:
        logger.info("No active positions to reconcile.")
        return {"checked": 0, "resolved": 0, "won": 0, "lost": 0, "zombies_fixed": 0}

    resolved_won = 0
    resolved_lost = 0
    checked = 0

    for pos in active:
        token_id = pos.get("token_id", "")
        if not token_id:
            continue

        checked += 1
        status = _check_token_resolution(token_id)

        if status and status["resolved"]:
            won = status["winner"]
            pos_id = pos["id"]
            slug = pos.get("slug", "unknown")

            if dry_run:
                logger.info(
                    f"DRY_RUN would resolve #{pos_id} {slug[:50]}: "
                    f"{'WON' if won else 'LOST'} (CLOB price={status['price']})"
                )
                if won:
                    resolved_won += 1
                else:
                    resolved_lost += 1
                continue

            try:
                record_resolution(
                    position_id=pos_id,
                    won=won,
                    actual_temp=None,
                    actual_temp_source="clob_reconciler",
                )
                if won:
                    resolved_won += 1
                    logger.info(f"#{pos_id} {slug[:50]} → RESOLVED WON (CLOB)")
                else:
                    resolved_lost += 1
                    logger.info(f"#{pos_id} {slug[:50]} → RESOLVED LOST (CLOB)")
            except Exception as e:
                logger.error(f"Failed to record resolution for #{pos_id}: {e}")
        else:
            logger.debug(
                f"#{pos['id']} {pos.get('slug', '?')[:50]} "
                f"still open (price={status['price'] if status else 'unknown'})"
            )

    return {
        "checked": checked,
        "resolved": resolved_won + resolved_lost,
        "won": resolved_won,
        "lost": resolved_lost,
    }


# ── Main ──────────────────────────────────────────────────────────
def main(dry_run: bool = False, fix_zombies_only: bool = False):
    start_time = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("Reconciliation run started")

    # Phase 1: Fix zombies
    zombies_fixed = fix_zombie_positions(dry_run=dry_run)

    if fix_zombies_only:
        _write_dump(start_time, zombies_fixed=zombies_fixed, positions_checked=0,
                    resolved=0, won=0, lost=0)
        exposure = get_total_open_exposure()
        logger.info(f"Post-fix exposure: ${exposure:.2f}")
        return

    # Phase 2: Full CLOB reconciliation
    results = reconcile_positions(dry_run=dry_run)
    results["zombies_fixed"] = zombies_fixed

    # Phase 3: Report
    exposure = get_total_open_exposure()
    pnl = get_pnl_summary()

    logger.info(
        f"Reconciliation complete: {results['checked']} checked, "
        f"{results['resolved']} resolved ({results['won']}W/{results['lost']}L), "
        f"{zombies_fixed} zombies fixed"
    )
    logger.info(f"Current exposure: ${exposure:.2f}")
    logger.info(f"PnL: won={pnl.get('won_count',0)} lost={pnl.get('lost_count',0)} "
                f"open={pnl.get('open_count',0)} total_pnl=${pnl.get('total_pnl',0):.2f}")

    # Alert if anything changed
    changes = []
    if zombies_fixed > 0:
        changes.append(f"{zombies_fixed} zombie positions fixed → exited")
    if results["resolved"] > 0:
        changes.append(f"{results['won']} won, {results['lost']} lost resolved via CLOB")

    if changes:
        msg = "\n".join(changes)
        msg += f"\n\nOpen exposure: ${exposure:.2f}"
        msg += f"\nPnL: {pnl.get('won_count',0)}W/{pnl.get('lost_count',0)}L " \
               f"(${pnl.get('total_pnl',0):+.2f})"
        _send_telegram(msg)

    _write_dump(start_time, **results)
    logger.info("Reconciliation run complete")


def _write_dump(start_time: datetime, **kwargs):
    """Write JSON status dump for dashboard."""
    try:
        dump = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": (datetime.now(timezone.utc) - start_time).total_seconds(),
            **kwargs,
        }
        DUMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(DUMP_FILE, "w") as f:
            json.dump(dump, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write dump: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Position reconciliation service")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only, no DB writes")
    parser.add_argument("--fix-zombies-only", action="store_true",
                        help="Only fix zombie positions (status=open but exit_reason set)")
    args = parser.parse_args()

    main(dry_run=args.dry_run, fix_zombies_only=args.fix_zombies_only)
