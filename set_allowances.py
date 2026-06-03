"""
set_allowances.py - ONE-TIME setup script.
Grants the Polymarket exchange contract permission to spend your USDC.e.
Must be run once before the bot can trade.

Notes:
  - COLLATERAL (USDC.e) approval is ERC-20 and requires no token_id.
  - CONDITIONAL token approval is ERC-1155 and is per-token; the bot
    handles this automatically before placing each live order.

Requires: ~1-2 POL on the proxy wallet for Polygon gas fees.

Run with: python3 set_allowances.py
"""

import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

load_dotenv()

HOST     = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon Mainnet

# Signature type: matches the bot's POLYMARKET_SIG_TYPE env var (default: 2)
SIG_TYPE = int(os.getenv("POLYMARKET_SIG_TYPE", "2"))


def main():
    pk     = os.getenv("POLYMARKET_PRIVATE_KEY")
    funder = os.getenv("POLYMARKET_FUNDER")

    if not pk or not funder:
        print("ERROR: POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER must be set in .env")
        return

    print("=" * 60)
    print("Polymarket Token Allowance Setup")
    print("=" * 60)
    print(f"Funder (proxy wallet): {funder}")
    print(f"CLOB host: {HOST}")
    print(f"Chain ID: {CHAIN_ID} (Polygon Mainnet)")
    print()
    print("This will submit an on-chain USDC.e approval transaction.")
    print("You will need ~1-2 POL on the proxy wallet for gas.")
    print()

    confirm = input("Proceed? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return

    print("\nInitializing CLOB client with signature_type=1 (proxy wallet)...")
    client = ClobClient(
        HOST,
        key=pk,
        chain_id=CHAIN_ID,
        signature_type=SIG_TYPE,
        funder=funder,
    )

    print("Setting API credentials...")
    try:
        client.set_api_creds(client.create_or_derive_api_creds())
        print("API credentials set successfully.")
    except Exception as e:
        print(f"WARNING: API credential error: {e}")
        print("Continuing...")

    collateral_params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=SIG_TYPE,
    )

    # Check current collateral balance + allowance
    print("\nChecking current USDC.e allowance...")
    try:
        result = client.get_balance_allowance(params=collateral_params)
        print(f"  Current: {result}")
    except Exception as e:
        print(f"  Could not read current allowance (non-fatal): {e}")

    # Set USDC.e collateral allowance
    print("\nSetting USDC.e (collateral) allowance...")
    try:
        resp = client.update_balance_allowance(params=collateral_params)
        print(f"  Collateral allowance set: {resp if resp else 'OK (empty response is normal)'}")
    except Exception as e:
        print(f"  ERROR setting collateral allowance: {e}")
        _print_common_errors()
        return

    # Verify
    print("\nVerifying USDC.e allowance...")
    try:
        result = client.get_balance_allowance(params=collateral_params)
        print(f"  Verified: {result}")
    except Exception as e:
        print(f"  Could not verify (non-fatal): {e}")

    print()
    print("=" * 60)
    print("CLOB Exchange USDC.e allowance set.")
    print()
    print("NOTE: Conditional token (ERC-1155) allowances are set per-market")
    print("      automatically by the bot before each live order.")
    print("=" * 60)

    # ------------------------------------------------------------------
    # Extra approvals required for CTF split/merge (sniper.py strategy)
    # ------------------------------------------------------------------
    # py-clob-client's update_balance_allowance() only authorizes the CLOB
    # Exchange contract. splitPosition pulls USDC.e from the proxy via the
    # CTF (or NegRiskAdapter) directly, which needs separate ERC-20 approvals.
    # We submit both via the same Safe-relayer pattern used by redeemer.py.
    print()
    print("Setting CTF + NegRiskAdapter USDC.e allowances (for split/merge)...")
    try:
        from ctf import ensure_collateral_allowances, DRY_RUN as CTF_DRY_RUN
        if CTF_DRY_RUN:
            print("  CTF_DRY_RUN=true in env -> skipping live approvals.")
            print("  Set CTF_DRY_RUN=false in .env, re-run, then flip back to true.")
        else:
            result = ensure_collateral_allowances()
            print(f"  Approvals result: {result.get('status')}")
            for spender, r in result.get("approvals", {}).items():
                print(f"    {spender[:14]}...: {r.get('status')} "
                      f"tx={r.get('transaction_hash', 'n/a')}")
    except ImportError as e:
        print(f"  Could not import ctf module: {e}")
        print("  (run from sniperweatherbot/ so ctf.py is on the path)")
    except Exception as e:
        print(f"  CTF approvals failed: {e}")

    print()
    print("Next step: python3 bot.py  (or sniper.py)")
    print("=" * 60)


def set_conditional_allowance(client: ClobClient, token_id: str) -> bool:
    """
    Sets the conditional token allowance for a specific outcome token.
    Called by executor.py before placing a live order on a new market.
    Returns True on success, False on failure.
    """
    params = BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL,
        token_id=token_id,
        signature_type=SIG_TYPE,
    )
    try:
        client.update_balance_allowance(params=params)
        return True
    except Exception as e:
        print(f"  WARNING: Could not set conditional allowance for {token_id[:16]}...: {e}")
        return False


def _print_common_errors():
    print()
    print("Common causes:")
    print("  1. Insufficient POL for gas on the proxy wallet")
    print("  2. Wrong POLYMARKET_PRIVATE_KEY")
    print("  3. Wrong POLYMARKET_FUNDER (must be proxy wallet, not MetaMask address)")


if __name__ == "__main__":
    main()
