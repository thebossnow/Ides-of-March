"""
logger.py - Trade decision logging to CSV.
Every scan cycle logs what the bot saw and what it decided, regardless
of whether a trade was placed. This is your audit trail.
"""

import csv
import os
import logging
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE  = os.path.join(_DIR, "trade_log.csv")
SCAN_FILE = os.path.join(_DIR, "scan_log.csv")

module_logger = logging.getLogger(__name__)

TRADE_HEADERS = [
    "timestamp",
    "market_slug",
    "city",
    "date",
    "forecast_temp_c",
    "forecast_temp_market_unit",
    "market_unit",
    "bucket_low",
    "bucket_high",
    "forecast_prob",
    "market_price",
    "edge",
    "size_usdc",
    "dry_run",
    "order_id",
    "order_status",
    "question",
]

SCAN_HEADERS = [
    "timestamp",
    "market_slug",
    "city",
    "date",
    "forecast_prob",
    "market_price",
    "edge",
    "decision",
    "reason",
    "market_ask",
    "max_bid",
    "forecast_temp",
    "market_unit",
    "bucket_low",
    "bucket_high",
]


def _ensure_headers(filepath: str, headers: list) -> None:
    if not os.path.isfile(filepath):
        with open(filepath, "w", newline="") as f:
            csv.writer(f).writerow(headers)


def log_trade(
    market_slug: str,
    city: str,
    date: str,
    forecast_temp_c: float,
    forecast_temp_market_unit: float,
    market_unit: str,
    bucket_low,
    bucket_high,
    forecast_prob: float,
    market_price: float,
    edge: float,
    size_usdc: float,
    dry_run: bool,
    order_response: dict,
    question: str = "",
) -> None:
    """Appends a trade execution record to trade_log.csv."""
    _ensure_headers(LOG_FILE, TRADE_HEADERS)

    # CLOB API returns "orderID" (not "id")
    order_id     = order_response.get("orderID", order_response.get("id", "N/A")) if order_response else "N/A"
    order_status = order_response.get("status", "FAILED") if order_response else "FAILED"

    row = [
        datetime.now().isoformat(),
        market_slug,
        city,
        date,
        round(forecast_temp_c, 2),
        round(forecast_temp_market_unit, 2),
        market_unit,
        bucket_low if bucket_low is not None else "none",
        bucket_high if bucket_high is not None else "none",
        round(forecast_prob, 4),
        round(market_price, 4),
        round(edge, 4),
        round(size_usdc, 2),
        dry_run,
        order_id,
        order_status,
        question[:200],  # Truncate very long questions
    ]

    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow(row)

    module_logger.info(
        f"Trade logged: {market_slug} | edge={edge:.1%} | "
        f"size=${size_usdc:.2f} | order={order_id} | status={order_status}"
    )


def log_scan(
    market_slug: str,
    city: str,
    date: str,
    forecast_prob: float,
    market_price: float,
    edge: float,
    decision: str,
    reason: str = "",
    market_ask: float = None,
    max_bid: float = None,
    forecast_temp: float = None,
    market_unit: str = "",
    bucket_low=None,
    bucket_high=None,
) -> None:
    """Appends a scan decision record to scan_log.csv (every market checked)."""
    _ensure_headers(SCAN_FILE, SCAN_HEADERS)

    row = [
        datetime.now().isoformat(),
        market_slug,
        city,
        date,
        round(forecast_prob, 4),
        round(market_price, 4),
        round(edge, 4),
        decision,
        reason[:200],
        round(market_ask, 4) if market_ask is not None else "",
        round(max_bid, 4) if max_bid is not None else "",
        round(forecast_temp, 2) if forecast_temp is not None else "",
        market_unit or "",
        bucket_low if bucket_low is not None else "",
        bucket_high if bucket_high is not None else "",
    ]

    with open(SCAN_FILE, "a", newline="") as f:
        csv.writer(f).writerow(row)


def get_recent_trades(n: int = 10) -> list:
    """Returns the last n rows from trade_log.csv as a list of dicts."""
    if not os.path.isfile(LOG_FILE):
        return []
    with open(LOG_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return rows[-n:]


if __name__ == "__main__":
    print("Logger module test - writing a sample row to trade_log.csv")
    log_trade(
        market_slug="test-market-slug",
        city="NYC",
        date="2026-03-25",
        forecast_temp_c=18.3,
        forecast_temp_market_unit=64.9,
        market_unit="F",
        bucket_low=60.0,
        bucket_high=None,
        forecast_prob=0.72,
        market_price=0.45,
        edge=0.27,
        size_usdc=10.0,
        dry_run=True,
        order_response={"id": "TEST_123", "status": "simulated"},
        question="Will NYC high temp exceed 60F on March 25?",
    )
    print("Done. Check trade_log.csv")
