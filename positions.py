"""
positions.py - SQLite position tracker for the weather bot.

Tracks all open, exited, and resolved positions with full metadata.
This is the backbone for:
  - Same-day exit signals (position_monitor.py)
  - Profit-taking exits
  - Resolution validation (observed_temps.py)
  - Redemption of winning positions (redeemer.py)
  - P&L tracking and model calibration

Schema designed for queryability: find all open same-day positions,
all resolved-but-unredeemed winners, all positions by city/date, etc.
"""

import sqlite3
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# DB path: uses POSITIONS_DB_PATH env var if set, otherwise same directory as this script.
# On VPS this will be ~/weatherbot/positions.db alongside the other bot files.
DB_FILE = os.getenv(
    "POSITIONS_DB_PATH",
    str(Path(__file__).parent / "positions.db"),
)

# Thread-local storage for connections (SQLite connections are not thread-safe)
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """
    Returns a thread-local SQLite connection with WAL mode for concurrency.
    Automatically initializes the schema on first connection per thread.
    """
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_FILE, timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
        # Initialize schema on first connection
        _init_schema(_local.conn)
    return _local.conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """Creates the positions table and indexes if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,

            -- Market identification
            token_id        TEXT NOT NULL,
            condition_id    TEXT,
            slug            TEXT NOT NULL,
            city            TEXT NOT NULL,
            market_date     TEXT NOT NULL,

            -- Bucket definition
            bucket_low      REAL,
            bucket_high     REAL,
            unit            TEXT NOT NULL DEFAULT 'F',

            -- Entry details
            entry_price     REAL NOT NULL,
            shares          REAL NOT NULL,
            size_usdc       REAL NOT NULL,
            entry_time      TEXT NOT NULL,
            order_id        TEXT,

            -- Current state
            status          TEXT NOT NULL DEFAULT 'open',
            -- Statuses: open, exited, resolved_won, resolved_lost, redeemed

            -- Exit details (filled when exited or resolved)
            exit_price      REAL,
            exit_time       TEXT,
            exit_reason     TEXT,
            exit_order_id   TEXT,

            -- Resolution details (filled by observed_temps checker)
            actual_temp     REAL,
            actual_temp_source TEXT,
            resolved_time   TEXT,

            -- P&L
            pnl_usdc        REAL,

            -- Negative risk flag (needed for redemption)
            neg_risk        INTEGER DEFAULT 0,

            -- Market direction (highest = daily MAX, lowest = daily MIN)
            market_type     TEXT NOT NULL DEFAULT 'highest',

            -- Metadata
            question        TEXT,
            forecast_prob   REAL,
            market_prob     REAL,
            edge            REAL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ------------------------------------------------------------------
    # Migration: add columns for split/merge (CTF strategy) bookkeeping.
    # Safe to re-run — IF NOT EXISTS guards via try/except on each ALTER.
    # outcome:      'YES' or 'NO' (default 'YES' for legacy rows since the
    #               sniper has historically only bought YES tokens).
    # entry_method: 'buy' (CLOB FOK/GTC), 'split' (CTF.splitPosition),
    #               'sweep' (FOK book sweep). Default 'buy' for legacy rows.
    # ------------------------------------------------------------------
    for ddl in (
        "ALTER TABLE positions ADD COLUMN outcome TEXT NOT NULL DEFAULT 'YES'",
        "ALTER TABLE positions ADD COLUMN entry_method TEXT NOT NULL DEFAULT 'buy'",
        "ALTER TABLE positions ADD COLUMN market_type TEXT NOT NULL DEFAULT 'highest'",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise

    # Indexes for common queries
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_positions_status
        ON positions(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_positions_condition_outcome
        ON positions(condition_id, outcome, status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_positions_token_id
        ON positions(token_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_positions_market_date
        ON positions(market_date)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_positions_city_date
        ON positions(city, market_date)
    """)

    conn.commit()
    logger.info(f"Positions database initialized: {DB_FILE}")


def record_entry(
    token_id: str,
    condition_id: str,
    slug: str,
    city: str,
    market_date: str,
    bucket_low: Optional[float],
    bucket_high: Optional[float],
    unit: str,
    entry_price: float,
    shares: float,
    size_usdc: float,
    order_id: str = None,
    neg_risk: bool = False,
    question: str = "",
    forecast_prob: float = 0.0,
    market_prob: float = 0.0,
    edge: float = 0.0,
    outcome: str = "YES",
    entry_method: str = "buy",
    market_type: str = "highest",
) -> int:
    """
    Records a new position entry. Returns the row ID.
    Called immediately after a successful order placement.

    outcome:      'YES' or 'NO'. Defaults to 'YES' for backward compatibility
                  with all directional-buy callers.
    entry_method: 'buy' | 'split' | 'sweep'. Tracks how the position was
                  acquired (CLOB order vs CTF.splitPosition vs FOK book sweep).
    market_type:  'highest' (daily MAX) or 'lowest' (daily MIN). Critical for
                  correct resolution — lowest markets must resolve against MIN temp.
    """
    conn = _get_conn()
    cursor = conn.execute(
        """
        INSERT INTO positions (
            token_id, condition_id, slug, city, market_date,
            bucket_low, bucket_high, unit,
            entry_price, shares, size_usdc, entry_time, order_id,
            status, neg_risk,
            question, forecast_prob, market_prob, edge,
            outcome, entry_method, market_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            token_id, condition_id, slug, city, market_date,
            bucket_low, bucket_high, unit,
            entry_price, shares, size_usdc,
            datetime.now(timezone.utc).isoformat(),
            order_id,
            1 if neg_risk else 0,
            question[:500] if question else "",
            forecast_prob, market_prob, edge,
            outcome.upper(),
            entry_method.lower(),
            market_type.lower(),
        ),
    )
    conn.commit()
    row_id = cursor.lastrowid
    logger.info(
        f"Position recorded: id={row_id} | {city} {market_date} | "
        f"{outcome} {shares:.2f} shares @ {entry_price:.3f} = ${size_usdc:.2f} "
        f"(method={entry_method})"
    )
    return row_id


def record_exit(
    position_id: int,
    exit_price: float,
    exit_reason: str,
    exit_order_id: str = None,
) -> None:
    """
    Records a position exit (sold before resolution).
    Calculates P&L from entry vs exit price.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT entry_price, shares, size_usdc FROM positions WHERE id = ?",
        (position_id,),
    ).fetchone()

    if not row:
        logger.warning(f"Cannot record exit: position {position_id} not found")
        return

    # P&L: (exit_price - entry_price) * shares
    pnl = (exit_price - row["entry_price"]) * row["shares"]

    conn.execute(
        """
        UPDATE positions SET
            status = 'exited',
            exit_price = ?,
            exit_time = ?,
            exit_reason = ?,
            exit_order_id = ?,
            pnl_usdc = ?
        WHERE id = ?
        """,
        (
            exit_price,
            datetime.now(timezone.utc).isoformat(),
            exit_reason,
            exit_order_id,
            round(pnl, 4),
            position_id,
        ),
    )
    conn.commit()
    logger.info(
        f"Position exited: id={position_id} | reason={exit_reason} | "
        f"pnl=${pnl:+.2f}"
    )


def record_resolution(
    position_id: int,
    won: bool,
    actual_temp: float = None,
    actual_temp_source: str = None,
) -> None:
    """
    Records the resolution outcome for a position.
    Won: payout = shares * $1.00. Lost: payout = $0.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT entry_price, shares, size_usdc FROM positions WHERE id = ?",
        (position_id,),
    ).fetchone()

    if not row:
        logger.warning(f"Cannot record resolution: position {position_id} not found")
        return

    if won:
        # Winning: receive $1 per share, paid entry_price per share
        pnl = row["shares"] * (1.0 - row["entry_price"])
        status = "resolved_won"
    else:
        # Losing: shares are worthless, lost entire investment
        pnl = -row["size_usdc"]
        status = "resolved_lost"

    conn.execute(
        """
        UPDATE positions SET
            status = ?,
            actual_temp = ?,
            actual_temp_source = ?,
            resolved_time = ?,
            pnl_usdc = ?
        WHERE id = ?
        """,
        (
            status,
            actual_temp,
            actual_temp_source,
            datetime.now(timezone.utc).isoformat(),
            round(pnl, 4),
            position_id,
        ),
    )
    conn.commit()

    result_str = "WON" if won else "LOST"
    logger.info(
        f"Position resolved: id={position_id} | {result_str} | "
        f"actual={actual_temp} | pnl=${pnl:+.2f}"
    )


def record_redemption(position_id: int) -> None:
    """Marks a resolved_won position as redeemed (payout claimed)."""
    conn = _get_conn()
    conn.execute(
        """
        UPDATE positions SET status = 'redeemed'
        WHERE id = ? AND status = 'resolved_won'
        """,
        (position_id,),
    )
    conn.commit()
    logger.info(f"Position redeemed: id={position_id}")


# -----------------------------------------------------------------------
# Query helpers
# -----------------------------------------------------------------------

def get_open_positions() -> list[dict]:
    """Returns all positions with status='open'."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'open' ORDER BY market_date"
    ).fetchall()
    return [dict(r) for r in rows]


def get_total_open_exposure() -> float:
    """Returns total USDC exposure of all open positions."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(size_usdc), 0.0) as total FROM positions WHERE status = 'open'"
    ).fetchone()
    return float(row["total"] or 0.0)


def get_open_positions_for_date(market_date: str) -> list[dict]:
    """Returns open positions for a specific market date."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'open' AND market_date = ?",
        (market_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_positions_by_status(status: str) -> list[dict]:
    """Returns all positions with the given status."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status = ? ORDER BY market_date",
        (status,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_unredeemed_winners() -> list[dict]:
    """Returns positions that resolved as wins but haven't been redeemed."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'resolved_won' ORDER BY resolved_time"
    ).fetchall()
    return [dict(r) for r in rows]


def get_unresolved_past_positions() -> list[dict]:
    """
    Returns open positions where market_date is in the past.
    These need resolution checking.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'open' AND market_date < ?",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def is_token_traded_today(token_id: str) -> bool:
    """Checks if we already have an open position for this token."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM positions WHERE token_id = ? AND status = 'open'",
        (token_id,),
    ).fetchone()
    return row["cnt"] > 0


def get_position_by_id(position_id: int) -> Optional[dict]:
    """Returns a single position by ID."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM positions WHERE id = ?", (position_id,)
    ).fetchone()
    return dict(row) if row else None


def get_pnl_summary() -> dict:
    """Returns aggregate P&L stats across all resolved/exited positions."""
    conn = _get_conn()
    row = conn.execute("""
        SELECT
            COUNT(*) as total_positions,
            SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_count,
            SUM(CASE WHEN status = 'exited' THEN 1 ELSE 0 END) as exited_count,
            SUM(CASE WHEN status = 'resolved_won' THEN 1 ELSE 0 END) as won_count,
            SUM(CASE WHEN status = 'resolved_lost' THEN 1 ELSE 0 END) as lost_count,
            SUM(CASE WHEN status = 'redeemed' THEN 1 ELSE 0 END) as redeemed_count,
            SUM(CASE WHEN pnl_usdc IS NOT NULL THEN pnl_usdc ELSE 0 END) as total_pnl,
            SUM(size_usdc) as total_invested,
            AVG(CASE WHEN pnl_usdc IS NOT NULL THEN pnl_usdc END) as avg_pnl
        FROM positions
    """).fetchone()
    return dict(row)


def get_mergeable_pairs(min_pair_size: float = 1.0) -> list[dict]:
    """
    Returns conditions where we hold both YES and NO open shares (a "complete
    set"), with at least `min_pair_size` shares on each side. These pairs can
    be merged back to USDC.e via ctf.merge_positions to free up collateral
    without waiting for resolution.

    Output rows:
        condition_id, neg_risk, mergeable_shares, slug, city, market_date,
        yes_shares, no_shares, yes_position_id, no_position_id

    mergeable_shares = min(yes_shares, no_shares).
    """
    conn = _get_conn()
    rows = conn.execute(
        """
        WITH yes_legs AS (
            SELECT condition_id, neg_risk, slug, city, market_date,
                   SUM(shares) AS yes_shares,
                   MIN(id)     AS yes_position_id
            FROM positions
            WHERE status = 'open' AND outcome = 'YES' AND condition_id <> ''
            GROUP BY condition_id
        ),
        no_legs AS (
            SELECT condition_id,
                   SUM(shares) AS no_shares,
                   MIN(id)     AS no_position_id
            FROM positions
            WHERE status = 'open' AND outcome = 'NO' AND condition_id <> ''
            GROUP BY condition_id
        )
        SELECT y.condition_id, y.neg_risk, y.slug, y.city, y.market_date,
               y.yes_shares, n.no_shares,
               y.yes_position_id, n.no_position_id,
               MIN(y.yes_shares, n.no_shares) AS mergeable_shares
        FROM yes_legs y
        JOIN no_legs n USING (condition_id)
        WHERE MIN(y.yes_shares, n.no_shares) >= ?
        ORDER BY mergeable_shares DESC
        """,
        (min_pair_size,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_calibration_data() -> list[dict]:
    """
    Returns resolved positions with forecast vs actual data.
    Used for model calibration and sigma/df tuning.
    """
    conn = _get_conn()
    rows = conn.execute("""
        SELECT
            city, market_date, bucket_low, bucket_high, unit,
            forecast_prob, market_prob, edge,
            actual_temp, actual_temp_source,
            status, pnl_usdc, entry_price, shares
        FROM positions
        WHERE status IN ('resolved_won', 'resolved_lost', 'redeemed')
            AND actual_temp IS NOT NULL
        ORDER BY market_date
    """).fetchall()
    return [dict(r) for r in rows]


# -----------------------------------------------------------------------
# Module init (deferred to first use or explicit call)
# -----------------------------------------------------------------------
def init_db() -> None:
    """Public entry point: forces DB initialization. Safe to call multiple times."""
    _get_conn()  # triggers _init_schema via _get_conn


if __name__ == "__main__":
    print(f"Positions database: {DB_FILE}")
    print(f"Tables initialized.")

    # Quick smoke test
    print("\n--- Smoke test: insert and query ---")
    test_id = record_entry(
        token_id="TEST_TOKEN_123",
        condition_id="TEST_COND_456",
        slug="test-market-slug",
        city="NYC",
        market_date="2026-04-04",
        bucket_low=52.0,
        bucket_high=54.0,
        unit="F",
        entry_price=0.25,
        shares=40.0,
        size_usdc=10.0,
        order_id="TEST_ORDER",
        neg_risk=False,
        question="Will NYC high temp be 52-54F on April 4?",
        forecast_prob=0.38,
        market_prob=0.22,
        edge=0.16,
    )
    print(f"Inserted position id={test_id}")

    positions = get_open_positions()
    print(f"Open positions: {len(positions)}")
    for p in positions:
        print(f"  id={p['id']} | {p['city']} {p['market_date']} | "
              f"[{p['bucket_low']},{p['bucket_high']}]{p['unit']} | "
              f"status={p['status']}")

    # Test resolution
    record_resolution(test_id, won=True, actual_temp=53.1, actual_temp_source="open-meteo")
    resolved = get_positions_by_status("resolved_won")
    print(f"\nResolved winners: {len(resolved)}")
    for p in resolved:
        print(f"  id={p['id']} | pnl=${p['pnl_usdc']:+.2f} | actual={p['actual_temp']}")

    # Test redemption
    record_redemption(test_id)
    redeemed = get_positions_by_status("redeemed")
    print(f"\nRedeemed: {len(redeemed)}")

    # P&L summary
    summary = get_pnl_summary()
    print(f"\nP&L Summary: {summary}")

    # Cleanup test data
    conn = _get_conn()
    conn.execute("DELETE FROM positions WHERE token_id = 'TEST_TOKEN_123'")
    conn.commit()
    print("\nTest data cleaned up.")
