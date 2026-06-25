"""
ctf.py - Splits/merges Polymarket conditional token positions.

Mirrors redeemer.py's pattern: encodes CTF (or NegRiskAdapter) calldata and
submits it through the Polymarket builder relayer as a SafeTransaction
executed by the user's Safe proxy. Gasless from the EOA's perspective.

Public API:
    split_position(condition_id, usdc_amount, neg_risk=False) -> dict
        Mints (usdc_amount) of YES + (usdc_amount) of NO shares from
        usdc_amount USDC.e collateral. 1 USDC.e -> 1 YES + 1 NO.

    merge_positions(condition_id, usdc_amount, neg_risk=False) -> dict
        Burns equal YES+NO pairs back to USDC.e. Requires holding
        >= usdc_amount of BOTH outcome tokens.

    ensure_collateral_allowances() -> dict
        One-shot ERC-20 approve(MAX) from the Safe proxy to the CTF
        and NegRiskAdapter contracts. Required before split_position
        can pull USDC.e on the proxy's behalf. Idempotent.

Function selectors (verified via Web3.keccak on 2026-04-26):
    CTF.splitPosition(address,bytes32,bytes32,uint256[],uint256)  = 0x72ce4275
    CTF.mergePositions(address,bytes32,bytes32,uint256[],uint256) = 0x9e7212ad
    NRA.splitPosition(bytes32,uint256)                             = 0xa3d7da1d
    NRA.mergePositions(bytes32,uint256)                            = 0xb10c5c17
    ERC20.approve(address,uint256)                                 = 0x095ea7b3

Contract addresses (Polygon Mainnet):
    CTF              0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
    NegRiskAdapter   0xC5d563A36AE78145C45a50134d48A1215220f80a
    USDC.e           0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
"""

import os
import logging
from typing import Optional

from dotenv import load_dotenv
from eth_abi import encode

load_dotenv()
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Polygon contract addresses
# -----------------------------------------------------------------------
CTF_ADDRESS       = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER  = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_E_ADDRESS    = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PARENT_COLLECTION = bytes.fromhex("00" * 32)  # zero bytes32 root collection

RELAYER_URL = "https://relayer.polymarket.com"
CHAIN_ID    = 137

# -----------------------------------------------------------------------
# Function selectors (verified via Web3.keccak)
# -----------------------------------------------------------------------
SEL_SPLIT_CTF      = bytes.fromhex("72ce4275")
SEL_MERGE_CTF      = bytes.fromhex("9e7212ad")
SEL_SPLIT_NEGRISK  = bytes.fromhex("a3d7da1d")
SEL_MERGE_NEGRISK  = bytes.fromhex("b10c5c17")
SEL_ERC20_APPROVE  = bytes.fromhex("095ea7b3")

# Binary YES/NO partition: outcome 0 -> bitmask 1 (YES), outcome 1 -> bitmask 2 (NO)
BINARY_PARTITION = [1, 2]

# USDC.e has 6 decimals
USDC_DECIMALS = 6
MAX_UINT256   = (1 << 256) - 1

# -----------------------------------------------------------------------
# Safety: mirrors executor.DRY_RUN. Default True until manually flipped.
# -----------------------------------------------------------------------
DRY_RUN = os.getenv("CTF_DRY_RUN", "true").lower() not in ("false", "0", "no")


# =======================================================================
# Calldata builders
# =======================================================================

def _normalize_condition_id(condition_id: str) -> bytes:
    cid = condition_id.lower()
    if cid.startswith("0x"):
        cid = cid[2:]
    if len(cid) != 64:
        raise ValueError(f"condition_id must be 32 bytes hex, got {len(cid)} chars")
    return bytes.fromhex(cid)


def _to_usdc_units(amount_usdc: float) -> int:
    if amount_usdc <= 0:
        raise ValueError(f"amount_usdc must be positive, got {amount_usdc}")
    return int(round(amount_usdc * (10 ** USDC_DECIMALS)))


def _build_split_calldata(condition_id: str, amount_usdc: float, neg_risk: bool) -> str:
    """
    Encodes splitPosition() calldata for either the CTF or the NegRiskAdapter.

    CTF signature:
        splitPosition(IERC20 collateralToken, bytes32 parentCollectionId,
                      bytes32 conditionId, uint256[] partition, uint256 amount)

    NegRiskAdapter signature:
        splitPosition(bytes32 conditionId, uint256 amount)
    """
    cid = _normalize_condition_id(condition_id)
    amount_units = _to_usdc_units(amount_usdc)

    if neg_risk:
        encoded = encode(["bytes32", "uint256"], [cid, amount_units])
        return "0x" + SEL_SPLIT_NEGRISK.hex() + encoded.hex()

    encoded = encode(
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [USDC_E_ADDRESS, PARENT_COLLECTION, cid, BINARY_PARTITION, amount_units],
    )
    return "0x" + SEL_SPLIT_CTF.hex() + encoded.hex()


def _build_merge_calldata(condition_id: str, amount_usdc: float, neg_risk: bool) -> str:
    cid = _normalize_condition_id(condition_id)
    amount_units = _to_usdc_units(amount_usdc)

    if neg_risk:
        encoded = encode(["bytes32", "uint256"], [cid, amount_units])
        return "0x" + SEL_MERGE_NEGRISK.hex() + encoded.hex()

    encoded = encode(
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [USDC_E_ADDRESS, PARENT_COLLECTION, cid, BINARY_PARTITION, amount_units],
    )
    return "0x" + SEL_MERGE_CTF.hex() + encoded.hex()


def _build_erc20_approve_calldata(spender: str, amount: int = MAX_UINT256) -> str:
    """ERC-20 approve(spender, amount). Used to pre-authorize CTF/NRA to pull USDC.e."""
    spender_clean = spender.lower()
    if spender_clean.startswith("0x"):
        spender_clean = spender_clean[2:]
    encoded = encode(["address", "uint256"], ["0x" + spender_clean, amount])
    return "0x" + SEL_ERC20_APPROVE.hex() + encoded.hex()


# =======================================================================
# Relayer client (shared with redeemer.py — same creds)
# =======================================================================

def _get_relay_client():
    """Returns a configured RelayClient or None on failure."""
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
    except ImportError:
        logger.error(
            "py-builder-relayer-client not installed. "
            "Run: pip install py-builder-relayer-client"
        )
        return None

    pk        = os.getenv("POLYMARKET_PRIVATE_KEY")
    bk        = os.getenv("POLYMARKET_BUILDER_KEY")
    bs        = os.getenv("POLYMARKET_BUILDER_SECRET")
    bp        = os.getenv("POLYMARKET_BUILDER_PASSPHRASE")

    if not pk:
        logger.error("CTF: POLYMARKET_PRIVATE_KEY not set")
        return None
    if not all([bk, bs, bp]):
        logger.error("CTF: builder API credentials not set in .env")
        return None

    try:
        builder_config = BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(key=bk, secret=bs, passphrase=bp)
        )
        return RelayClient(
            relayer_url=RELAYER_URL,
            chain_id=CHAIN_ID,
            private_key=pk,
            builder_config=builder_config,
        )
    except Exception as e:
        logger.error(f"CTF: failed to create RelayClient: {e}")
        return None


def _submit_safe_tx(target: str, calldata: str, op_label: str,
                    poll_timeout_s: int = 60) -> dict:
    """Submits a single SafeTransaction via the relayer, polls to terminal state."""
    client = _get_relay_client()
    if client is None:
        return {"status": "FAILED", "reason": "no_relay_client", "op": op_label}

    try:
        from py_builder_relayer_client.models import SafeTransaction, OperationType

        txn = SafeTransaction(
            to=target,
            operation=OperationType.CALL,
            data=calldata,
            value="0",
        )

        response = client.execute([txn])
        logger.info(
            f"CTF {op_label} submitted: target={target} | "
            f"tx_id={response.transactionID} | tx_hash={response.transactionHash}"
        )

        try:
            final_state = response.poll_until_state(
                target_states=["CONFIRMED", "FAILED", "REVERTED"],
                timeout=poll_timeout_s,
            )
        except Exception as poll_err:
            logger.warning(f"CTF {op_label}: poll failed: {poll_err}")
            final_state = "SUBMITTED"

        success = final_state not in ("FAILED", "REVERTED")
        return {
            "status": "SUCCESS" if success else "FAILED",
            "op": op_label,
            "transaction_id":   response.transactionID,
            "transaction_hash": response.transactionHash,
            "final_state":      final_state,
        }

    except Exception as e:
        msg = str(e)
        logger.error(f"CTF {op_label} failed: {msg}")
        if "429" in msg or "quota" in msg.lower() or "rate" in msg.lower():
            return {"status": "RATE_LIMITED", "reason": msg, "op": op_label}
        return {"status": "FAILED", "reason": msg, "op": op_label}


# =======================================================================
# Public API
# =======================================================================

def split_position(condition_id: str, usdc_amount: float,
                   neg_risk: bool = False) -> dict:
    """
    Splits usdc_amount of USDC.e into equal YES + NO conditional shares.

    Result: proxy gains usdc_amount of YES tokens AND usdc_amount of NO tokens,
    proxy loses usdc_amount of USDC.e. The CTF (or NegRiskAdapter) must already
    have ERC-20 allowance from the proxy — call ensure_collateral_allowances()
    once at startup.
    """
    if usdc_amount < 1.0:
        return {"status": "REJECTED", "reason": "amount_below_1_usdc"}

    target = NEG_RISK_ADAPTER if neg_risk else CTF_ADDRESS

    if DRY_RUN:
        logger.info(
            f"[CTF DRY RUN] split_position: {usdc_amount:.2f} USDC.e -> "
            f"{usdc_amount:.2f} YES + {usdc_amount:.2f} NO | "
            f"cond={condition_id[:18]}... | target={target} | neg_risk={neg_risk}"
        )
        return {
            "status": "SIMULATED",
            "op": "split",
            "condition_id": condition_id,
            "amount_usdc":  usdc_amount,
            "neg_risk":     neg_risk,
        }

    try:
        calldata = _build_split_calldata(condition_id, usdc_amount, neg_risk)
    except Exception as e:
        logger.error(f"CTF split: calldata build failed: {e}")
        return {"status": "FAILED", "reason": f"calldata: {e}"}

    result = _submit_safe_tx(target, calldata, op_label=f"split(${usdc_amount:.2f})")
    result["condition_id"] = condition_id
    result["amount_usdc"]  = usdc_amount
    result["neg_risk"]     = neg_risk
    return result


def merge_positions(condition_id: str, usdc_amount: float,
                    neg_risk: bool = False) -> dict:
    """
    Burns usdc_amount of YES + usdc_amount of NO shares back to usdc_amount USDC.e.
    Requires holding >= usdc_amount of BOTH outcome tokens. The CTF/NRA contracts
    burn the ERC-1155s directly — no allowance needed for the burn itself, but
    setApprovalForAll(CTF, true) is required if using the NegRiskAdapter wrapper.
    """
    if usdc_amount < 1.0:
        return {"status": "REJECTED", "reason": "amount_below_1_usdc"}

    target = NEG_RISK_ADAPTER if neg_risk else CTF_ADDRESS

    if DRY_RUN:
        logger.info(
            f"[CTF DRY RUN] merge_positions: {usdc_amount:.2f} YES + {usdc_amount:.2f} NO -> "
            f"{usdc_amount:.2f} USDC.e | cond={condition_id[:18]}... | "
            f"target={target} | neg_risk={neg_risk}"
        )
        return {
            "status": "SIMULATED",
            "op": "merge",
            "condition_id": condition_id,
            "amount_usdc":  usdc_amount,
            "neg_risk":     neg_risk,
        }

    try:
        calldata = _build_merge_calldata(condition_id, usdc_amount, neg_risk)
    except Exception as e:
        logger.error(f"CTF merge: calldata build failed: {e}")
        return {"status": "FAILED", "reason": f"calldata: {e}"}

    result = _submit_safe_tx(target, calldata, op_label=f"merge(${usdc_amount:.2f})")
    result["condition_id"] = condition_id
    result["amount_usdc"]  = usdc_amount
    result["neg_risk"]     = neg_risk
    return result


def ensure_collateral_allowances() -> dict:
    """
    Submits two ERC-20 approve(MAX) calls from the Safe proxy:
       1. USDC.e -> CTF
       2. USDC.e -> NegRiskAdapter

    Idempotent at the contract level (an existing MAX allowance is a no-op
    in terms of trade behavior; we'll just re-set it). Run once on startup
    before the first split. Has no effect if approvals are already MAX.

    Future improvement: read current allowance via web3 and skip the tx
    if already MAX. For now we just submit both unconditionally on startup —
    cheap (gasless via relayer) and safer than caching.
    """
    if DRY_RUN:
        logger.info("[CTF DRY RUN] ensure_collateral_allowances: would approve(MAX) USDC.e -> CTF and -> NegRiskAdapter")
        return {"status": "SIMULATED", "approved": [CTF_ADDRESS, NEG_RISK_ADAPTER]}

    results = {}
    for spender in (CTF_ADDRESS, NEG_RISK_ADAPTER):
        calldata = _build_erc20_approve_calldata(spender, MAX_UINT256)
        # USDC.e is the *target* contract for an ERC-20 approve;
        # the spender is encoded into calldata.
        r = _submit_safe_tx(USDC_E_ADDRESS, calldata,
                            op_label=f"approve(USDC.e->{spender[:10]}...)")
        results[spender] = r

    ok = all(v.get("status") == "SUCCESS" for v in results.values())
    return {
        "status": "SUCCESS" if ok else "PARTIAL",
        "approvals": results,
    }


# =======================================================================
# Self-test
# =======================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    print("=" * 60)
    print("CTF Module Self-Test")
    print("=" * 60)
    print(f"DRY_RUN:          {DRY_RUN}")
    print(f"CTF address:      {CTF_ADDRESS}")
    print(f"NegRiskAdapter:   {NEG_RISK_ADAPTER}")
    print(f"USDC.e:           {USDC_E_ADDRESS}")
    print(f"Relayer URL:      {RELAYER_URL}")
    print()

    # Calldata smoke tests
    print("--- Calldata encoding smoke tests ---")
    test_cid = "0x" + "ab" * 32

    cd_split_ctf = _build_split_calldata(test_cid, 10.0, neg_risk=False)
    assert cd_split_ctf.startswith("0x72ce4275"), f"split CTF selector mismatch: {cd_split_ctf[:10]}"
    print(f"  splitPosition(CTF):       OK  ({len(cd_split_ctf)} chars)")

    cd_split_nra = _build_split_calldata(test_cid, 10.0, neg_risk=True)
    assert cd_split_nra.startswith("0xa3d7da1d"), f"split NRA selector mismatch: {cd_split_nra[:10]}"
    print(f"  splitPosition(NRA):       OK  ({len(cd_split_nra)} chars)")

    cd_merge_ctf = _build_merge_calldata(test_cid, 10.0, neg_risk=False)
    assert cd_merge_ctf.startswith("0x9e7212ad"), f"merge CTF selector mismatch: {cd_merge_ctf[:10]}"
    print(f"  mergePositions(CTF):      OK  ({len(cd_merge_ctf)} chars)")

    cd_merge_nra = _build_merge_calldata(test_cid, 10.0, neg_risk=True)
    assert cd_merge_nra.startswith("0xb10c5c17"), f"merge NRA selector mismatch: {cd_merge_nra[:10]}"
    print(f"  mergePositions(NRA):      OK  ({len(cd_merge_nra)} chars)")

    cd_approve = _build_erc20_approve_calldata(CTF_ADDRESS)
    assert cd_approve.startswith("0x095ea7b3"), f"approve selector mismatch: {cd_approve[:10]}"
    print(f"  ERC20.approve:            OK  ({len(cd_approve)} chars)")

    print()
    print("--- USDC unit conversion ---")
    assert _to_usdc_units(1.0)    == 1_000_000
    assert _to_usdc_units(0.5)    == 500_000
    assert _to_usdc_units(25.0)   == 25_000_000
    assert _to_usdc_units(0.001)  == 1_000
    print("  All USDC unit conversions OK")

    print()
    print("--- DRY-RUN public API ---")
    r1 = split_position(test_cid, 5.0, neg_risk=False)
    print(f"  split (CTF):     {r1['status']}")
    r2 = split_position(test_cid, 5.0, neg_risk=True)
    print(f"  split (NRA):     {r2['status']}")
    r3 = merge_positions(test_cid, 5.0, neg_risk=False)
    print(f"  merge (CTF):     {r3['status']}")
    r4 = ensure_collateral_allowances()
    print(f"  approvals:       {r4['status']}")

    print()
    print("Self-test PASSED. Set CTF_DRY_RUN=false in .env to enable live calls.")
