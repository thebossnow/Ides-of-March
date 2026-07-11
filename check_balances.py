#!/usr/bin/env python3
"""
check_balances.py - Check actual on-chain conditional token balances
for all markets the bot has traded.

Approach:
1. Parse trade_log.csv for all unique token_ids from successful orders
2. Use the Polymarket data API to fetch current positions for our wallet
3. Cross-reference with the Polymarket history CSV if provided

Run on VPS:  python3 check_balances.py
"""

import os
import sys
import csv
import json
import time
from dotenv import load_dotenv

load_dotenv()

# Try to import requests; fall back to urllib if unavailable
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    HAS_REQUESTS = False


FUNDER = os.getenv("POLYMARKET_FUNDER", "").strip().lower()
DATA_API = "https://data-api.polymarket.com"


def http_get(url: str, timeout: int = 15) -> dict | list | None:
    """Simple GET that works with or without requests."""
    try:
        if HAS_REQUESTS:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()
        else:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  HTTP error for {url[:80]}...: {e}")
        return None


def fetch_positions_from_data_api() -> list[dict]:
    """
    Fetch all positions for our wallet from the Polymarket data API.
    Endpoint: GET /positions?user=<address>
    """
    if not FUNDER:
        print("  POLYMARKET_FUNDER not set in .env")
        return []

    url = f"{DATA_API}/positions?user={FUNDER}"
    print(f"  Fetching positions from data API for {FUNDER[:20]}...")
    data = http_get(url)

    if data is None:
        print("  Failed to fetch from data API.")
        return []

    if isinstance(data, list):
        return data
    elif isinstance(data, dict) and "positions" in data:
        return data["positions"]
    else:
        print(f"  Unexpected response type: {type(data)}")
        # Try with different endpoint patterns
        for endpoint in [
            f"{DATA_API}/positions?address={FUNDER}",
            f"{DATA_API}/user-positions?user={FUNDER}",
        ]:
            data = http_get(endpoint)
            if isinstance(data, list) and len(data) > 0:
                return data
        return []


def fetch_positions_from_clob() -> list[dict]:
    """
    Fetch positions using the CLOB client's balance checking.
    This requires token_ids, so we get them from the trade log.
    """
    try:
        from executor import get_client, get_conditional_token_balance
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
    except ImportError as e:
        print(f"  Cannot import executor: {e}")
        return []

    # Get unique token_ids from trade_log.csv
    token_ids = get_token_ids_from_trade_log()
    if not token_ids:
        print("  No token IDs found in trade log.")
        return []

    client = get_client()
    results = []

    print(f"  Checking {len(token_ids)} unique token(s)...")
    for i, (token_id, info) in enumerate(token_ids.items()):
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=1,  # sig_type 1 works for proxy wallet
            )
            result = client.get_balance_allowance(params)

            if isinstance(result, dict) and "balance" in result:
                raw_balance = result["balance"]
                # Conditional tokens: check if raw or micro-units
                balance = float(raw_balance)
                # If balance looks like micro-units (very large), divide
                if balance > 1_000_000:
                    balance = balance / 1_000_000

                if balance > 0.01:
                    results.append({
                        "token_id": token_id,
                        "balance": balance,
                        "info": info,
                    })
                    print(f"  [{i+1}/{len(token_ids)}] HOLDING: {balance:>10.2f} tokens | {info}")
                else:
                    print(f"  [{i+1}/{len(token_ids)}] empty   : {info[:60]}")
            else:
                print(f"  [{i+1}/{len(token_ids)}] no data : {info[:60]}")

            # Rate limit: don't hammer the API
            if (i + 1) % 5 == 0:
                time.sleep(1)

        except Exception as e:
            print(f"  [{i+1}/{len(token_ids)}] ERROR   : {info[:40]}... | {e}")
            time.sleep(1)

    return results


def get_token_ids_from_trade_log() -> dict:
    """
    Extract unique token_ids from the bot's scan_log.csv or trade_log.csv.
    The trade_log doesn't store token_ids directly, but the bot's log file might.
    We need to find them from the CLOB client using market slugs.
    """
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_log.csv")
    if not os.path.exists(log_file):
        return {}

    # The trade log has: timestamp, slug, city, date, ... order_id, status, question
    # We need token_ids. Get slugs from successful trades, then look up token_ids.
    slugs = {}
    with open(log_file, "r") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 17:
                continue
            slug = row[1]
            order_id = row[14]
            status = row[15]
            city = row[2]
            date_str = row[3]
            question = row[16] if len(row) > 16 else ""

            if order_id in ("order_id", "N/A", "DRY_RUN", "") or status == "FAILED":
                continue

            if slug not in slugs:
                slugs[slug] = f"{city} {date_str} | {question[:50]}"

    if not slugs:
        return {}

    print(f"  Found {len(slugs)} unique slugs from successful trades. Looking up token IDs...")

    # Look up token_ids from slugs via the markets API
    token_ids = {}
    try:
        from markets import get_weather_markets
    except ImportError:
        pass

    # Use CLOB client to look up each slug
    try:
        from py_clob_client_v2 import ClobClient

        client = ClobClient(
            "https://clob.polymarket.com",
            137,
            os.getenv("POLYMARKET_PRIVATE_KEY"),
            signature_type=int(os.getenv("POLYMARKET_SIG_TYPE", "2")),
            funder=FUNDER,
        )
        creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)

        for i, (slug, info) in enumerate(slugs.items()):
            try:
                # The CLOB API lets you fetch market by slug (condition_id)
                # Try the gamma API for slug lookups
                url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
                data = http_get(url)
                if data and isinstance(data, list) and len(data) > 0:
                    market = data[0]
                    tokens = json.loads(market.get("clobTokenIds", "[]"))
                    if tokens:
                        # YES token is typically index 0
                        token_ids[tokens[0]] = info
                elif data and isinstance(data, dict):
                    tokens = json.loads(data.get("clobTokenIds", "[]"))
                    if tokens:
                        token_ids[tokens[0]] = info

                if (i + 1) % 10 == 0:
                    print(f"    ...looked up {i+1}/{len(slugs)} slugs")
                    time.sleep(1)

            except Exception as e:
                print(f"    Slug lookup failed for {slug[:40]}: {e}")

    except Exception as e:
        print(f"  CLOB client error: {e}")

    print(f"  Resolved {len(token_ids)} token IDs from {len(slugs)} slugs.")
    return token_ids


def check_usdc_balance():
    """Check USDC balance."""
    print("=" * 70)
    print("USDC BALANCE")
    print("=" * 70)
    try:
        from py_clob_client_v2 import ClobClient, BalanceAllowanceParams, AssetType

        client = ClobClient(
            "https://clob.polymarket.com",
            137,
            os.getenv("POLYMARKET_PRIVATE_KEY"),
            signature_type=int(os.getenv("POLYMARKET_SIG_TYPE", "2")),
            funder=FUNDER,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=1,
        )
        result = client.get_balance_allowance(params)
        if isinstance(result, dict) and "balance" in result:
            balance = int(result["balance"]) / 1_000_000
            print(f"  USDC Balance: ${balance:.2f}")
            return balance
    except Exception as e:
        print(f"  Error: {e}")
    return 0.0


if __name__ == "__main__":
    print("=" * 70)
    print("POLYMARKET TOKEN BALANCE CHECKER")
    print(f"Wallet: {FUNDER}")
    print("=" * 70)
    print()

    usdc = check_usdc_balance()
    print()

    # Method 1: Try the data API first (fastest, no rate limiting)
    print("=" * 70)
    print("METHOD 1: Polymarket Data API")
    print("=" * 70)
    api_positions = fetch_positions_from_data_api()
    if api_positions:
        print(f"\n  Found {len(api_positions)} position(s) from data API:")
        total_value = 0
        for p in api_positions:
            size = float(p.get("size", p.get("amount", 0)))
            avg_price = float(p.get("avgPrice", p.get("avg_price", 0)))
            cur_price = float(p.get("curPrice", p.get("current_price", 0)))
            market = p.get("title", p.get("market", p.get("slug", "?")))
            token = p.get("asset", p.get("token_id", "?"))
            value = size * cur_price if cur_price > 0 else size * avg_price
            total_value += value
            outcome = p.get("outcome", p.get("side", "?"))
            print(
                f"    {size:>8.2f} shares | avg ${avg_price:.4f} | "
                f"cur ${cur_price:.4f} | val ${value:.2f} | "
                f"{outcome} | {str(market)[:55]}"
            )
        print(f"\n  Estimated portfolio value: ${total_value:.2f}")
        print(f"  + USDC balance: ${usdc:.2f}")
        print(f"  = Total: ${total_value + usdc:.2f}")
    else:
        print("  No positions found via data API (or API unavailable).")

    print()

    # Method 2: Check token balances via CLOB client (slower but authoritative)
    print("=" * 70)
    print("METHOD 2: CLOB Client Token Balance Check")
    print("=" * 70)
    clob_positions = fetch_positions_from_clob()
    if clob_positions:
        print(f"\n  Summary: {len(clob_positions)} token(s) with non-zero balance")
        for p in clob_positions:
            print(f"    {p['balance']:>10.2f} tokens | {p['info']}")
    else:
        print("  No positions found via CLOB balance check.")

    print()
    print("=" * 70)
    print("Done.")
