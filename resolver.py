#!/usr/bin/env python3
"""
resolver.py — Standalone position resolution service.

Runs independently of the main bot. Called hourly by systemd timer.
1. Finds all open positions with past market_date
2. Fetches actual observed temperatures from Open-Meteo Archive API
3. Determines win/loss for each position
4. Calculates PnL and updates positions.db
5. Sends Telegram alerts for each resolved position
6. Writes JSON status dump for dashboard

Usage:
    python resolver.py                          # Resolve all past positions
    python resolver.py --dry-run                # Preview only, no DB writes
    python resolver.py --city "Tokyo,Seoul"     # Filter by cities
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure the bot's venv packages are available
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from positions import get_unresolved_past_positions, record_resolution, get_pnl_summary
from observed_temps import resolve_positions
from notifier import TelegramNotifier
from weather import CITIES

# Positions with these characteristics cannot be resolved:
# - city = "OnChainDetect" (chain reconciliation noise rows)
# - market_date = "1970-01-01" (placeholder)
# - No valid bucket (both bucket_low and bucket_high are None)
def _is_resolvable(pos: dict) -> bool:
    city = pos.get("city", "")
    if city == "OnChainDetect" or city not in CITIES:
        return False
    if pos.get("market_date", "") in ("1970-01-01", ""):
        return False
    if pos.get("bucket_low") is None and pos.get("bucket_high") is None:
        return False
    return True

# ── Config ────────────────────────────────────────────────────────
LOG_FILE = Path(__file__).resolve().parent / "logs" / "resolver.log"
DUMP_FILE = Path("/opt/botdash/resolver_status.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [resolver] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE)),
    ],
)
logger = logging.getLogger("resolver")

# ── Notifier factory ──────────────────────────────────────────────
def _make_notifier() -> Optional[TelegramNotifier]:
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        try:
            return TelegramNotifier(token=token, chat_id=int(chat_id))
        except Exception as e:
            logger.error("Failed to create Telegram notifier: %s", e)
    return None


# ── Main ──────────────────────────────────────────────────────────
def main(dry_run: bool = False, city_filter: Optional[set] = None):
    start_time = datetime.now(timezone.utc)

    # 1. Get unresolved past positions
    positions = get_unresolved_past_positions()
    
    # Filter out unresolvable noise rows
    skipped = [p for p in positions if not _is_resolvable(p)]
    positions = [p for p in positions if _is_resolvable(p)]
    if skipped:
        logger.info("Skipping %d unresolvable position(s) (OnChainDetect, no city, no bucket)",
                    len(skipped))
    
    if not positions:
        logger.info("No unresolved past positions — nothing to do.")
        _write_dump(start_time, checked=0, resolved=0, won=0, lost=0,
                    boundary=0, errors=0, pnl=None)
        return

    # Filter by city if requested
    if city_filter:
        positions = [p for p in positions if p.get("city", "OnChainDetect") in city_filter]
        if not positions:
            logger.info("No matching positions after city filter.")
            return

    logger.info("Found %d unresolved past position(s)", len(positions))
    for p in positions:
        logger.info("  #%d %s %s bucket=[%s,%s]%s",
                    p["id"], p["city"], p["market_date"],
                    p.get("bucket_low"), p.get("bucket_high"),
                    p.get("unit", "F"))

    if dry_run:
        logger.info("DRY_RUN — would resolve %d position(s) but skipping DB writes.", len(positions))
        _write_dump(start_time, checked=len(positions), resolved=0, won=0, lost=0,
                    boundary=0, errors=0, pnl=None, dry_run=True)
        return

    # 2. Resolve positions against actual temps
    try:
        results = resolve_positions(positions)
    except Exception as e:
        logger.error("Temperature resolution failed: %s", e, exc_info=True)
        _write_dump(start_time, checked=len(positions), resolved=0, won=0, lost=0,
                    boundary=0, errors=len(positions), pnl=None)
        sys.exit(1)

    # 3. Record each result and notify
    notifier = _make_notifier()
    won = 0
    lost = 0
    boundary = 0
    errors = 0

    for result in results:
        try:
            record_resolution(
                position_id=result["position_id"],
                won=result["won"],
                actual_temp=result["actual_temp_unit"],
                actual_temp_source=result["source"],
            )
        except Exception as e:
            logger.error("Failed to record resolution for position %d: %s",
                         result["position_id"], e)
            errors += 1
            continue

        if result["won"]:
            won += 1
        else:
            lost += 1

        if result["boundary_flag"]:
            boundary += 1

        # Telegram notification
        if notifier:
            icon = "+" if result["won"] else "x"
            try:
                notifier.send_message(
                    f"<b>[{icon}] Market Resolved</b>\n"
                    f"{result['city']} {result['market_date']}\n"
                    f"Actual: {result['actual_temp_unit']:.1f}{result['unit']} | "
                    f"Bucket: [{result['bucket_low']},{result['bucket_high']}]\n"
                    f"{'WON' if result['won'] else 'LOST'}"
                )
                notifier.record_settlement(won=result["won"])
            except Exception as e:
                logger.warning("Telegram notify failed for #%d: %s",
                              result["position_id"], e)

    # 4. Summary
    logger.info("Resolution complete: %d won, %d lost, %d boundary, %d errors",
                won, lost, boundary, errors)

    # 5. Get PnL summary
    pnl_data = None
    try:
        pnl = get_pnl_summary()
        pnl_data = {
            "total_pnl": round(pnl["total_pnl"], 2),
            "open_count": pnl["open_count"],
            "won_count": pnl["won_count"],
            "lost_count": pnl["lost_count"],
            "exited_count": pnl["exited_count"],
        }
        logger.info("PnL summary: total=$%.2f | won=%d lost=%d open=%d",
                    pnl_data["total_pnl"], pnl_data["won_count"],
                    pnl_data["lost_count"], pnl_data["open_count"])
    except Exception as e:
        logger.error("PnL summary failed: %s", e)

    _write_dump(start_time, checked=len(positions), resolved=len(results),
                won=won, lost=lost, boundary=boundary, errors=errors,
                pnl=pnl_data)


def _write_dump(start_time, checked, resolved, won, lost, boundary, errors,
                pnl, dry_run=False):
    """Write JSON status dump for dashboard."""
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(
            (datetime.now(timezone.utc) - start_time).total_seconds(), 1
        ),
        "checked": checked,
        "resolved": resolved,
        "won": won,
        "lost": lost,
        "boundary_flags": boundary,
        "errors": errors,
        "dry_run": dry_run,
        "pnl": pnl,
    }
    try:
        DUMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        DUMP_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning("Failed to write dump file: %s", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve past weather positions")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only, no DB writes")
    parser.add_argument("--city", type=str, default="",
                        help="Comma-separated city filter")
    args = parser.parse_args()

    city_filter = None
    if args.city:
        city_filter = {c.strip() for c in args.city.split(",") if c.strip()}

    main(dry_run=args.dry_run, city_filter=city_filter)
