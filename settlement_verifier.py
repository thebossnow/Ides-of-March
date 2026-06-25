#!/usr/bin/env python3
"""
settlement_verifier.py - Layer 2: Verifies delayed orders have settled on-chain.

Solves the Cape Town problem: bot places order, CLOB returns "delayed" status,
bot records provisional position with estimated amounts. This module queries
the CLOB API for the actual settlement status and updates the DB.

Called from position_monitor.monitor_positions() after the main exit checks.
"""

import logging
from datetime import datetime, timezone

from executor import DRY_RUN, get_client as get_clob_client

logger = logging.getLogger(__name__)

# Max age to keep checking a delayed order before giving up
MAX_DELAYED_AGE_HOURS = 12


def verify_delayed_settlements(notifier=None) -> dict:
    """
    Checks all positions that were recorded from 'delayed' orders and
    updates them with actual settlement data from the CLOB API.

    Returns:
        dict with summary: checked, updated, still_pending, abandoned, errors
    """
    from positions import get_open_positions

    if DRY_RUN:
        return {"checked": 0, "note": "dry_run"}

    # Find positions with exit_reason starting with "delayed_"
    all_positions = get_open_positions()
    delayed = [
        p for p in all_positions
        if (p.get("exit_reason") or "").startswith("delayed_")
        and p.get("order_id")
    ]

    if not delayed:
        return {"checked": 0, "updated": 0, "still_pending": 0, "abandoned": 0, "errors": 0}

    logger.info(f"Settlement verifier: checking {len(delayed)} delayed position(s)")

    client = get_clob_client()
    updated = 0
    still_pending = 0
    abandoned = 0
    errors = 0

    for pos in delayed:
        order_id = pos["order_id"]
        position_id = pos["id"]

        # Age-based give-up: if order is >12h old and still delayed, abandon
        try:
            entry_time = datetime.fromisoformat(
                pos["entry_time"].replace("Z", "+00:00")
                if "Z" in str(pos.get("entry_time", ""))
                else str(pos.get("entry_time", ""))
            )
        except (ValueError, TypeError):
            entry_time = None

        if entry_time:
            age_hours = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            if age_hours > MAX_DELAYED_AGE_HOURS:
                logger.warning(
                    f"Delayed order {order_id[:16]}... abandoned after "
                    f"{age_hours:.1f}h (position #{position_id})"
                )
                _mark_abandoned(position_id, f"delayed_timeout_{age_hours:.0f}h")
                abandoned += 1
                continue

        # Query CLOB for actual order status
        try:
            order = client.get_order(order_id)
        except Exception as e:
            logger.error(
                f"Failed to query order {order_id[:16]}...: {e}"
            )
            errors += 1
            continue

        status = (order.get("status") or "").lower()

        if status == "matched" or status == "filled":
            # Order settled — update with real amounts
            taking = order.get("takingAmount", "")
            making = order.get("makingAmount", "")
            try:
                actual_shares = float(taking) if taking else pos.get("shares", 0)
                actual_spent = float(making) if making else pos.get("size_usdc", 0)
            except (ValueError, TypeError):
                logger.warning(
                    f"Cannot parse amounts for {order_id[:16]}..., "
                    f"keeping estimates"
                )
                still_pending += 1
                continue

            actual_price = actual_spent / actual_shares if actual_shares > 0 else pos.get("entry_price", 0)

            _update_settled(
                position_id,
                actual_shares=round(actual_shares, 4),
                actual_spent=round(actual_spent, 4),
                actual_price=round(actual_price, 4),
            )

            logger.info(
                f"Delayed order SETTLED: {pos.get('city')} {pos.get('market_date')} "
                f"#{position_id} | {actual_shares:.2f} shares @ ${actual_price:.4f} "
                f"(${actual_spent:.2f})"
            )
            updated += 1

            if notifier:
                notifier.send_message(
                    f"<b>[✓] Delayed Order Settled</b>\n"
                    f"{pos.get('city')} {pos.get('market_date')}\n"
                    f"Est → Real: {pos.get('shares', 0):.1f}→{actual_shares:.1f} sh @ "
                    f"${pos.get('entry_price', 0):.4f}→${actual_price:.4f}\n"
                    f"${pos.get('size_usdc', 0):.2f}→${actual_spent:.2f} USDC"
                )

        elif status in ("cancelled", "expired", "killed", "failed"):
            logger.warning(
                f"Delayed order RESOLVED BAD: {pos.get('city')} {pos.get('market_date')} "
                f"#{position_id} status={status}"
            )
            _mark_abandoned(position_id, f"delayed_settled_as_{status}")
            abandoned += 1

        else:
            # Still "delayed" or unknown — wait
            still_pending += 1

    logger.info(
        f"Settlement verifier complete: {updated} settled, "
        f"{still_pending} pending, {abandoned} abandoned, {errors} errors"
    )

    return {
        "checked": len(delayed),
        "updated": updated,
        "still_pending": still_pending,
        "abandoned": abandoned,
        "errors": errors,
    }


def _update_settled(position_id: int, actual_shares: float, actual_spent: float, actual_price: float):
    """Update a delayed position with real settlement amounts."""
    import sqlite3

    db_path = "/root/weatherbot/positions.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """UPDATE positions
           SET shares = ?,
               size_usdc = ?,
               entry_price = ?,
               exit_reason = 'delayed_settled'
           WHERE id = ?""",
        (actual_shares, actual_spent, actual_price, position_id),
    )
    conn.commit()
    conn.close()


def _mark_abandoned(position_id: int, reason: str):
    """Mark a delayed position as abandoned (never settled or cancelled)."""
    import sqlite3

    db_path = "/root/weatherbot/positions.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """UPDATE positions
           SET status = 'abandoned',
               exit_reason = ?,
               exit_time = datetime('now')
           WHERE id = ?""",
        (reason, position_id),
    )
    conn.commit()
    conn.close()
