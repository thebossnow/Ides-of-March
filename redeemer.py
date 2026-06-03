"""
redeemer.py - Redeems winning positions on resolved Polymarket markets.

After a market resolves, winning YES shares can be redeemed for $1.00 USDC
each. This module handles that redemption using the official Polymarket
py-builder-relayer-client which submits gasless transactions through the
Polymarket relayer infrastructure.

Architecture:
  1. Encode a redeemPositions() call to the Conditional Tokens Framework (CTF)
     contract on Polygon.
  2. Wrap it in a SafeTransaction and submit via RelayClient.execute().
  3. The relayer executes the transaction gaslessly through the user's Safe proxy.

Requirements:
  - py-builder-relayer-client (pip install py-builder-relayer-client)
  - Builder API credentials (apiKey, secret, passphrase) in .env
  - POLYMARKET_PRIVATE_KEY in .env

Contract addresses (Polygon Mainnet):
  - CTF: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
  - NegRiskAdapter: 0xC5d563A36AE78145C45a50134d48A1215220f80a
  - USDC.e (collateral): 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
"""

import os
import logging
import time
from typing import Optional

from dotenv import load_dotenv
from eth_abi import encode

load_dotenv()
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Polygon contract addresses
# -----------------------------------------------------------------------
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PARENT_COLLECTION_ID = "0x" + "00" * 32  # Zero bytes32 for root collection

# Relayer URL
RELAYER_URL = "https://relayer.polymarket.com"
CHAIN_ID = 137  # Polygon Mainnet

# Rate limiting
REDEEM_DELAY_S = 5.0
MAX_REDEMPTIONS_PER_CYCLE = 10

# redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)
# Function selector: 0x01b7037c
REDEEM_SELECTOR = bytes.fromhex("01b7037c")


def _build_redeem_calldata(condition_id: str, index_sets: list[int]) -> str:
    """
    Encodes the redeemPositions() calldata for the CTF contract.

    Args:
        condition_id: The market's condition ID (hex string, 0x-prefixed or not)
        index_sets: Which outcome slots to redeem. For binary YES/NO markets:
                    [1, 2] redeems both outcomes (1 = YES, 2 = NO).
                    Typically pass [1, 2] to redeem all positions.

    Returns:
        Hex-encoded calldata string (0x-prefixed)
    """
    # Normalize condition_id
    cid = condition_id.lower()
    if cid.startswith("0x"):
        cid = cid[2:]
    condition_bytes = bytes.fromhex(cid)

    # Encode parameters: (address, bytes32, bytes32, uint256[])
    encoded = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            USDC_E_ADDRESS,                      # collateralToken
            bytes.fromhex("00" * 32),            # parentCollectionId (zero)
            condition_bytes,                      # conditionId
            index_sets,                           # indexSets
        ],
    )

    return "0x" + REDEEM_SELECTOR.hex() + encoded.hex()


def _get_relay_client():
    """
    Creates a RelayClient with builder API credentials.
    Returns the client or None on failure.
    """
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
    except ImportError:
        logger.error(
            "py-builder-relayer-client not installed. "
            "Run: pip install py-builder-relayer-client"
        )
        return None

    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    builder_key = os.getenv("POLYMARKET_BUILDER_KEY")
    builder_secret = os.getenv("POLYMARKET_BUILDER_SECRET")
    builder_passphrase = os.getenv("POLYMARKET_BUILDER_PASSPHRASE")

    if not pk:
        logger.error("POLYMARKET_PRIVATE_KEY not set")
        return None

    if not all([builder_key, builder_secret, builder_passphrase]):
        logger.error(
            "Builder API credentials not set. Need POLYMARKET_BUILDER_KEY, "
            "POLYMARKET_BUILDER_SECRET, POLYMARKET_BUILDER_PASSPHRASE in .env"
        )
        return None

    try:
        builder_config = BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=builder_key,
                secret=builder_secret,
                passphrase=builder_passphrase,
            )
        )

        client = RelayClient(
            relayer_url=RELAYER_URL,
            chain_id=CHAIN_ID,
            private_key=pk,
            builder_config=builder_config,
        )

        return client

    except Exception as e:
        logger.error(f"Failed to create RelayClient: {e}")
        return None


def redeem_position(
    condition_id: str,
    neg_risk: bool = False,
) -> dict:
    """
    Redeems a single resolved position via the relayer.

    Args:
        condition_id: Market condition ID
        neg_risk: Whether market uses negative risk adapter

    Returns:
        dict with status and details
    """
    client = _get_relay_client()
    if client is None:
        return {"status": "FAILED", "reason": "no_relay_client"}

    try:
        from py_builder_relayer_client.models import SafeTransaction, OperationType

        # Build the redeemPositions calldata
        # Index sets [1, 2] redeems both YES (index 0 -> bitmask 1) and NO (index 1 -> bitmask 2)
        calldata = _build_redeem_calldata(condition_id, [1, 2])

        # Target contract: CTF for normal markets, NegRiskAdapter for neg_risk
        target = NEG_RISK_ADAPTER if neg_risk else CTF_ADDRESS

        txn = SafeTransaction(
            to=target,
            operation=OperationType.CALL,
            data=calldata,
            value="0",
        )

        # Submit via relayer
        response = client.execute([txn])

        logger.info(
            f"Redemption submitted: condition={condition_id[:16]}... | "
            f"txn_id={response.transactionID} | txn_hash={response.transactionHash}"
        )

        # Poll for completion (optional, with timeout)
        try:
            final_state = response.poll_until_state(
                target_states=["CONFIRMED", "FAILED", "REVERTED"],
                timeout=60,
            )
            logger.info(f"Redemption final state: {final_state}")
        except Exception as poll_err:
            logger.warning(f"Could not poll redemption status: {poll_err}")
            final_state = "SUBMITTED"

        return {
            "status": "SUCCESS" if final_state != "FAILED" and final_state != "REVERTED" else "FAILED",
            "condition_id": condition_id,
            "transaction_id": response.transactionID,
            "transaction_hash": response.transactionHash,
            "final_state": final_state,
        }

    except Exception as e:
        error_str = str(e)
        logger.error(f"Redemption failed: condition={condition_id[:16]}... | {error_str}")

        if "429" in error_str or "quota" in error_str.lower() or "rate" in error_str.lower():
            return {"status": "RATE_LIMITED", "reason": error_str}

        return {"status": "FAILED", "reason": error_str}


def redeem_all_winners(notifier=None) -> dict:
    """
    Finds all resolved_won positions and redeems them.
    Called daily from bot.py.

    Args:
        notifier: TelegramNotifier instance for alerts (optional)

    Returns:
        dict with summary:
            total, redeemed, failed, rate_limited, total_payout
    """
    from positions import get_unredeemed_winners, record_redemption, get_position_by_id

    winners = get_unredeemed_winners()
    if not winners:
        logger.debug("No unredeemed winning positions found")
        return {"total": 0, "redeemed": 0, "failed": 0, "rate_limited": 0, "total_payout": 0.0}

    logger.info(f"Found {len(winners)} unredeemed winning position(s)")

    redeemed = 0
    failed = 0
    rate_limited = 0
    total_payout = 0.0

    for i, pos in enumerate(winners[:MAX_REDEMPTIONS_PER_CYCLE]):
        condition_id = pos.get("condition_id")
        if not condition_id:
            logger.warning(f"Position {pos['id']} has no condition_id, skipping redemption")
            failed += 1
            continue

        # Skip positions with no shares — nothing to redeem on-chain
        shares = pos.get("shares", 0.0)
        if not shares or shares < 0.001:
            logger.info(
                f"Position {pos['id']} ({pos['city']} {pos['market_date']}): "
                f"shares={shares} — skipping redemption (no shares to redeem)"
            )
            failed += 1
            continue

        result = redeem_position(
            condition_id=condition_id,
            neg_risk=bool(pos.get("neg_risk", 0)),
        )

        if result["status"] == "SUCCESS":
            record_redemption(pos["id"])
            redeemed += 1
            payout = pos["shares"]  # $1 per winning share
            total_payout += payout

            if notifier:
                notifier.send_message(
                    f"<b>[$$] Position Redeemed</b>\n"
                    f"{pos['city']} {pos['market_date']}\n"
                    f"Shares: {pos['shares']:.1f} | Payout: ${payout:.2f}\n"
                    f"Original cost: ${pos['size_usdc']:.2f} | "
                    f"P&L: ${pos.get('pnl_usdc', 0):+.2f}\n"
                    f"TxID: {result.get('transaction_id', 'N/A')}"
                )

        elif result["status"] == "RATE_LIMITED":
            rate_limited += 1
            logger.warning(
                f"Rate limited during redemption. Stopping. "
                f"Redeemed {redeemed}/{len(winners)} so far."
            )
            if notifier:
                notifier.notify_error(
                    "Redemption",
                    f"Rate limited after {redeemed} redemption(s). "
                    f"{len(winners) - redeemed - rate_limited} remaining. "
                    f"Will retry next cycle."
                )
            break

        else:
            failed += 1
            logger.error(f"Redemption failed for position {pos['id']}: {result}")

        # Rate limit delay between redemptions
        if i < len(winners) - 1:
            time.sleep(REDEEM_DELAY_S)

    summary = {
        "total": len(winners),
        "redeemed": redeemed,
        "failed": failed,
        "rate_limited": rate_limited,
        "total_payout": round(total_payout, 2),
    }

    if redeemed > 0:
        logger.info(
            f"Redemption complete: {redeemed}/{len(winners)} redeemed, "
            f"${total_payout:.2f} collected"
        )

    return summary


if __name__ == "__main__":
    print("Redeemer Module (py-builder-relayer-client)")
    print("=" * 50)
    print(f"CTF contract: {CTF_ADDRESS}")
    print(f"NegRisk adapter: {NEG_RISK_ADAPTER}")
    print(f"Relayer URL: {RELAYER_URL}")
    print(f"Rate limit delay: {REDEEM_DELAY_S}s between calls")
    print(f"Max per cycle: {MAX_REDEMPTIONS_PER_CYCLE}")
    print()

    # Check dependencies
    try:
        from py_builder_relayer_client.client import RelayClient
        print("py-builder-relayer-client: installed")
    except ImportError:
        print("py-builder-relayer-client: NOT installed")

    try:
        from eth_abi import encode
        print("eth-abi: installed")
    except ImportError:
        print("eth-abi: NOT installed (run: pip install eth-abi)")

    # Check env vars
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    bk = os.getenv("POLYMARKET_BUILDER_KEY")
    print(f"POLYMARKET_PRIVATE_KEY: {'set' if pk else 'NOT SET'}")
    print(f"Builder API creds: {'set' if bk else 'NOT SET'}")

    # Test calldata encoding
    print("\n--- Calldata encoding test ---")
    test_cid = "0x" + "ab" * 32
    calldata = _build_redeem_calldata(test_cid, [1, 2])
    print(f"  Condition ID: {test_cid[:20]}...")
    print(f"  Calldata length: {len(calldata)} chars")
    print(f"  Selector: {calldata[:10]}")
    print(f"  Expected: 0x01b7037c")
    assert calldata[:10] == "0x01b7037c", "Selector mismatch!"
    print("  Encoding test PASSED")
