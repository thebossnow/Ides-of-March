"""
inventory_manager.py - Periodic merge of complementary YES+NO pairs.

When the sniper splits USDC.e into YES+NO and only sells one side, the
unsold leg accumulates. Likewise, directional buys on both YES and NO of
the same condition (rare but possible across multi-bracket events) can
form a complete set.

Any time we hold >= 1 share of BOTH outcomes for the same condition_id,
those shares are guaranteed-redeemable for $1 USDC.e per pair via
CTF.mergePositions — no need to wait for resolution. Merging immediately
recycles collateral into available bankroll for the next snipe.

Architecture mirrors sniper._forecast_refresh_loop:
    - Daemon thread, runs every MERGE_INTERVAL_S seconds.
    - On each tick, queries positions.get_mergeable_pairs() and dispatches
      ctf.merge_positions() for each row above MERGE_MIN_SHARES.
    - Records the merged shares as a synthetic 'exited' position update.

DRY_RUN inherits from ctf.DRY_RUN — no chain calls until explicitly enabled.
"""

import os
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# --- Config ---
MERGE_INTERVAL_S = int(os.getenv("MERGE_INTERVAL_S", "600"))  # 10 min default
MERGE_MIN_SHARES = float(os.getenv("MERGE_MIN_SHARES", "5.0"))  # match CLOB minimum
MERGE_MAX_PER_CYCLE = int(os.getenv("MERGE_MAX_PER_CYCLE", "5"))

_shutdown_flag = threading.Event()
_thread: Optional[threading.Thread] = None
_notifier = None


def _record_merge_exit(position_id: int, shares_merged: float) -> None:
    """
    Records a merge as an exit at $1.00/share. Mirrors record_exit but with
    exit_reason='merged' so calibration data can distinguish from same-day
    sells. P&L = (1.0 - entry_price) * shares_merged.
    """
    try:
        from positions import _get_conn
        from datetime import datetime, timezone
    except Exception as e:
        logger.error(f"InventoryMgr: positions import failed: {e}")
        return

    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT entry_price, shares FROM positions WHERE id = ?",
            (position_id,),
        ).fetchone()
        if not row:
            logger.warning(f"InventoryMgr: position {position_id} not found")
            return

        entry_price = float(row["entry_price"])
        existing_shares = float(row["shares"])

        if shares_merged >= existing_shares - 1e-6:
            # Full close
            pnl = (1.0 - entry_price) * shares_merged
            conn.execute(
                """UPDATE positions
                   SET status='exited', exit_price=1.0, exit_time=?,
                       exit_reason='merged', exit_order_id='CTF_MERGE',
                       pnl_usdc=?
                   WHERE id=?""",
                (datetime.now(timezone.utc).isoformat(), round(pnl, 4), position_id),
            )
        else:
            # Partial: shrink the position by merged shares; pnl proportional.
            remaining = existing_shares - shares_merged
            # Mark the merged portion as a new 'exited' sibling row so
            # historical P&L tracking stays clean.
            pnl_partial = (1.0 - entry_price) * shares_merged
            conn.execute(
                "UPDATE positions SET shares = ?, size_usdc = ? WHERE id = ?",
                (round(remaining, 6), round(remaining * entry_price, 4), position_id),
            )
            # Insert the merged-out portion as a closed exit row.
            src = conn.execute(
                """SELECT token_id, condition_id, slug, city, market_date,
                          bucket_low, bucket_high, unit, neg_risk, question,
                          forecast_prob, market_prob, edge, outcome, entry_method
                   FROM positions WHERE id = ?""",
                (position_id,),
            ).fetchone()
            conn.execute(
                """INSERT INTO positions (
                       token_id, condition_id, slug, city, market_date,
                       bucket_low, bucket_high, unit,
                       entry_price, shares, size_usdc, entry_time, order_id,
                       status, neg_risk, question, forecast_prob, market_prob, edge,
                       outcome, entry_method,
                       exit_price, exit_time, exit_reason, exit_order_id, pnl_usdc
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'exited', ?,
                             ?, ?, ?, ?, ?, ?, 1.0, ?, 'merged', 'CTF_MERGE', ?)""",
                (
                    src["token_id"], src["condition_id"], src["slug"], src["city"],
                    src["market_date"], src["bucket_low"], src["bucket_high"], src["unit"],
                    entry_price, shares_merged, round(shares_merged * entry_price, 4),
                    datetime.now(timezone.utc).isoformat(),
                    "CTF_MERGE_SOURCE",
                    src["neg_risk"], src["question"], src["forecast_prob"],
                    src["market_prob"], src["edge"], src["outcome"], src["entry_method"],
                    datetime.now(timezone.utc).isoformat(),
                    round(pnl_partial, 4),
                ),
            )
        conn.commit()
    except Exception as e:
        logger.error(f"InventoryMgr: failed to record merge exit for pos {position_id}: {e}")


def merge_complementary_pairs() -> dict:
    """
    Single-pass merge: finds all conditions with matched YES+NO and merges
    them. Returns a summary dict.
    """
    try:
        from positions import get_mergeable_pairs
        from ctf import merge_positions
    except Exception as e:
        logger.error(f"InventoryMgr: imports failed: {e}")
        return {"error": str(e)}

    pairs = get_mergeable_pairs(min_pair_size=MERGE_MIN_SHARES)
    if not pairs:
        logger.debug("InventoryMgr: no mergeable pairs found")
        return {"checked": 0, "merged": 0, "freed_usdc": 0.0}

    logger.info(f"InventoryMgr: {len(pairs)} mergeable condition(s) found")

    merged = 0
    freed = 0.0
    failed = 0

    for row in pairs[:MERGE_MAX_PER_CYCLE]:
        cid       = row["condition_id"]
        neg_risk  = bool(row["neg_risk"])
        amount    = float(row["mergeable_shares"])  # 1:1 share-to-USDC ratio for binary CTF
        amount    = round(amount, 6)

        if amount < MERGE_MIN_SHARES:
            continue

        result = merge_positions(cid, amount, neg_risk=neg_risk)
        status = result.get("status", "UNKNOWN")

        if status in ("SUCCESS", "SIMULATED"):
            merged += 1
            freed += amount
            _record_merge_exit(int(row["yes_position_id"]), amount)
            _record_merge_exit(int(row["no_position_id"]),  amount)
            logger.info(
                f"InventoryMgr: merged {amount:.2f} pair(s) for {row['city']} "
                f"{row['market_date']} cond={cid[:14]}... -> ${amount:.2f} freed "
                f"(status={status})"
            )
            if _notifier:
                try:
                    _notifier.send_message(
                        f"CTF MERGE: {row['city']} {row['market_date']}\n"
                        f"Pairs: {amount:.2f} | Freed: ${amount:.2f} USDC.e\n"
                        f"Tx: {result.get('transaction_hash', 'simulated')}\n"
                        f"Status: {status}"
                    )
                except Exception:
                    pass
        else:
            failed += 1
            logger.warning(
                f"InventoryMgr: merge FAILED for {cid[:14]}...: "
                f"{result.get('reason', status)}"
            )

    return {
        "checked":    len(pairs),
        "merged":     merged,
        "failed":     failed,
        "freed_usdc": round(freed, 2),
    }


def _merge_loop() -> None:
    logger.info(f"InventoryMgr: thread started (interval={MERGE_INTERVAL_S}s)")
    # Stagger first run by 60s so it doesn't fire during sniper boot
    _shutdown_flag.wait(timeout=60)
    while not _shutdown_flag.is_set():
        try:
            summary = merge_complementary_pairs()
            if summary.get("merged", 0) > 0 or summary.get("failed", 0) > 0:
                logger.info(f"InventoryMgr: cycle summary: {summary}")
        except Exception as e:
            logger.error(f"InventoryMgr: cycle crashed: {e}", exc_info=True)
        _shutdown_flag.wait(timeout=MERGE_INTERVAL_S)
    logger.info("InventoryMgr: thread stopped")


def start_inventory_manager(notifier=None) -> threading.Thread:
    """Starts the daemon thread. Idempotent."""
    global _thread, _notifier
    _notifier = notifier
    _shutdown_flag.clear()
    if _thread is not None and _thread.is_alive():
        logger.warning("InventoryMgr: already running")
        return _thread
    _thread = threading.Thread(target=_merge_loop, daemon=True, name="inventory-mgr")
    _thread.start()
    return _thread


def stop_inventory_manager() -> None:
    _shutdown_flag.set()
    if _thread is not None and _thread.is_alive():
        _thread.join(timeout=10)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    print("=" * 60)
    print("Inventory Manager Self-Test")
    print("=" * 60)
    print(f"MERGE_INTERVAL_S:    {MERGE_INTERVAL_S}")
    print(f"MERGE_MIN_SHARES:    {MERGE_MIN_SHARES}")
    print(f"MERGE_MAX_PER_CYCLE: {MERGE_MAX_PER_CYCLE}")
    print()

    # Single one-shot pass
    summary = merge_complementary_pairs()
    print(f"Pass result: {summary}")
