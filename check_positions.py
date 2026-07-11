#!/usr/bin/env python3
"""
check_positions.py - Verify on-chain positions and balances.

Run on VPS:  python3 check_positions.py
"""

import os
import sys
import csv
from dotenv import load_dotenv

load_dotenv()

from py_clob_client_v2 import ClobClient, BalanceAllowanceParams, AssetType

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
SIG_TYPE = int(os.getenv("POLYMARKET_SIG_TYPE", "2"))


def get_authenticated_client() -> ClobClient:
    client = ClobClient(
        host=CLOB_HOST,
        key=os.getenv("POLYMARKET_PRIVATE_KEY"),
        chain_id=CHAIN_ID,
        signature_type=SIG_TYPE,
        funder=os.getenv("POLYMARKET_FUNDER").strip().lower(),
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client


def check_balance(client: ClobClient) -> None:
    print("=" * 60)
    print("USDC BALANCE")
    print("=" * 60)
    for sig_type in [1, 2]:
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=sig_type,
            )
            result = client.get_balance_allowance(params)
            if isinstance(result, dict) and "balance" in result:
                balance = int(result["balance"]) / 1_000_000
                print(f"  sig_type={sig_type}: ${balance:.2f} USDC")
            else:
                print(f"  sig_type={sig_type}: response={result}")
        except Exception as e:
            print(f"  sig_type={sig_type}: ERROR: {e}")


def check_open_orders(client: ClobClient) -> None:
    print("\n" + "=" * 60)
    print("OPEN ORDERS")
    print("=" * 60)
    try:
        orders = client.get_orders()
        if not orders:
            print("  No open orders.")
        else:
            for o in orders:
                oid = o.get("id", o.get("orderID", "?"))
                status = o.get("status", "?")
                side = o.get("side", "?")
                price = o.get("price", "?")
                size = o.get("original_size", o.get("size", "?"))
                token = o.get("asset_id", o.get("token_id", "?"))
                print(f"  {oid[:20]}... | {side} | ${price} x {size} | status={status}")
    except Exception as e:
        print(f"  ERROR: {e}")


def check_conditional_balances(client: ClobClient, token_ids: list[str]) -> None:
    print("\n" + "=" * 60)
    print("CONDITIONAL TOKEN BALANCES (your actual share holdings)")
    print("=" * 60)
    if not token_ids:
        print("  No token IDs to check.")
        return
    for token_id in token_ids:
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=1,  # sig_type 1 works for proxy wallet
            )
            result = client.get_balance_allowance(params)
            if isinstance(result, dict) and "balance" in result:
                shares = int(result["balance"]) / 1_000_000
                print(f"  {token_id[:30]}... = {shares:.4f} shares")
            else:
                print(f"  {token_id[:30]}... = response: {result}")
        except Exception as e:
            print(f"  {token_id[:30]}... = ERROR: {e}")


def get_traded_token_ids() -> list[str]:
    """Extract unique token IDs from trade_log.csv where order was not FAILED."""
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_log.csv")
    token_ids = set()
    if not os.path.exists(log_file):
        print(f"  trade_log.csv not found at {log_file}")
        return []
    with open(log_file, "r") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 16:
                continue
            order_id = row[14]
            status = row[15]
            # Skip header, failed, and dry run entries
            if order_id in ("order_id", "N/A", "DRY_RUN") or status == "FAILED":
                continue
            # The token_id is not directly in the CSV. We need the order_id
            # to verify. But we can get the slug and look it up.
            # For now, just report the successful order IDs.
            print(f"  Successful order: {order_id[:40]}... status={status} | {row[2]} {row[3]}")
    return list(token_ids)


def check_known_order_ids() -> None:
    """List all successful (non-FAILED, non-DRY_RUN) orders from trade log."""
    print("\n" + "=" * 60)
    print("SUCCESSFUL ORDERS FROM TRADE LOG")
    print("=" * 60)
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_log.csv")
    if not os.path.exists(log_file):
        print("  trade_log.csv not found.")
        return

    successful = []
    with open(log_file, "r") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 17:
                continue
            order_id = row[14]
            status = row[15]
            if order_id in ("order_id", "N/A", "DRY_RUN", "") or status == "FAILED":
                continue
            successful.append({
                "time": row[0],
                "city": row[2],
                "date": row[3],
                "market_price": row[10],
                "size_usdc": row[12],
                "order_id": order_id,
                "status": status,
                "question": row[16] if len(row) > 16 else "",
            })

    if not successful:
        print("  No successful orders found in trade log.")
    else:
        for s in successful:
            print(f"  {s['time'][:19]} | {s['city']} {s['date']} | ${s['size_usdc']} "
                  f"@ ${s['market_price']} | {s['status']} | {s['order_id'][:40]}...")


def check_positions_db() -> None:
    """Check what's in the positions SQLite database."""
    import sqlite3
    print("\n" + "=" * 60)
    print("POSITIONS DATABASE")
    print("=" * 60)
    db_path = os.getenv(
        "POSITIONS_DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.db"),
    )
    if not os.path.exists(db_path):
        print(f"  Database not found: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, city, market_date, status, entry_price, shares, size_usdc, order_id "
        "FROM positions ORDER BY id DESC LIMIT 20"
    ).fetchall()

    if not rows:
        print("  Database is empty (no positions recorded).")
    else:
        for r in rows:
            print(f"  id={r['id']} | {r['city']} {r['market_date']} | "
                  f"status={r['status']} | ${r['size_usdc']:.2f} @ {r['entry_price']:.4f}")

    # Summary counts
    summary = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM positions GROUP BY status"
    ).fetchall()
    if summary:
        print("\n  Status counts:")
        for s in summary:
            print(f"    {s['status']}: {s['cnt']}")

    conn.close()


if __name__ == "__main__":
    print("Polymarket Position Checker")
    print(f"Funder: {os.getenv('POLYMARKET_FUNDER', 'NOT SET')}")
    print()

    client = get_authenticated_client()

    check_balance(client)
    check_open_orders(client)
    check_known_order_ids()
    check_positions_db()

    # If you want to check specific token balances, add them here:
    # These are the YES token IDs from your two successful orders.
    # You can find them in the market metadata or bot logs.
    # check_conditional_balances(client, ["TOKEN_ID_1", "TOKEN_ID_2"])

    print("\n" + "=" * 60)
    print("Done.")
