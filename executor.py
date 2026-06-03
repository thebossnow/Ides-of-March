"""
executor.py - Order placement via Polymarket global CLOB API.
Uses py-clob-client-v2 with configurable signature_type (default: 2 for Gnosis Safe proxy).
MIGRATED: 2026-04-28 CLOB V1 -> V2

IMPORTANT: DRY_RUN = True until paper trading validates your edge.
           Only set DRY_RUN = False after 7+ days of paper-trade verification.
"""

import os
import time
import logging
import concurrent.futures
from dotenv import load_dotenv
# MIGRATION: Updated imports for CLOB V2 SDK
from py_clob_client_v2 import ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions, MarketOrderArgsV2
from py_clob_client_v2 import BalanceAllowanceParams, AssetType
from py_clob_client_v2.order_builder.constants import BUY, SELL
from py_clob_client_v2.constants import BYTES32_ZERO
from set_allowances import set_conditional_allowance

load_dotenv()
logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID  = 137  # Polygon Mainnet

# Signature type: 1 = EOA/Magic.link proxy, 2 = Gnosis Safe-style proxy
# Set POLYMARKET_SIG_TYPE=1 in .env to override (default: 2)
SIG_TYPE  = int(os.getenv("POLYMARKET_SIG_TYPE", "2"))

# CLOB V2 Builder attribution code (optional, bytes32 format)
# Get your builder code from: https://polymarket.com/settings?tab=builder
BUILDER_CODE = os.getenv("POLYMARKET_BUILDER_CODE", BYTES32_ZERO)

# -----------------------------------------------------------------------
# SAFETY FLAG - Controlled via .env: DRY_RUN=true to simulate, DRY_RUN=false for live.
# -----------------------------------------------------------------------
# LIVE TRADING ENABLED 2026-06-01 — Boss directive
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Module-level singleton client.  Credentials are derived ONCE at import time
# and reused for every order.  Re-deriving on each call was causing
# "invalid signature" errors from the CLOB API.
# ---------------------------------------------------------------------------
def _init_client() -> ClobClient:
    pk     = os.getenv("POLYMARKET_PRIVATE_KEY")
    funder = os.getenv("POLYMARKET_FUNDER")
    if not pk or not funder:
        raise EnvironmentError(
            "POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER must be set in .env"
        )
    # MIGRATION: CLOB V2 constructor signature: ClobClient(host, chain_id, key, ...)
    # Note: V2 docs incorrectly stated options object - SDK still uses positional args
    c = ClobClient(
        CLOB_HOST,      # positional
        CHAIN_ID,       # positional (chain_id)
        pk,             # positional (key) - was keyword in V1
        signature_type=SIG_TYPE,
        funder=funder.lower().strip(),
    )
    c.set_api_creds(c.create_or_derive_api_key())  # V2: renamed method
    logger.info("ClobClient initialized (singleton). sig_type=%s", SIG_TYPE)
    return c

_client: ClobClient = _init_client()

# ---------------------------------------------------------------------------
# Pre-cache for snipe latency optimization.
# Caches tick_size, neg_risk, and conditional token allowances so that
# place_gtc_order() can skip the 3 slowest calls (2 GETs + 1 blockchain tx).
# Target: reduce snipe latency from ~1-2s to <300ms (sign + POST only).
# ---------------------------------------------------------------------------
_tick_cache: dict[str, str] = {}        # token_id -> tick_size
_neg_risk_cache: dict[str, bool] = {}   # token_id -> neg_risk
_allowance_cache: set[str] = set()      # token_ids with allowance already set


def get_client() -> ClobClient:
    """Returns the module-level singleton ClobClient."""
    return _client


def precache_token_metadata(token_id: str) -> None:
    """
    Pre-fetches tick_size, neg_risk, and sets conditional allowance for a token.
    Called BEFORE order execution so that place_gtc_order() can skip these steps.

    This is the key latency optimization: these 3 calls take ~800-1500ms combined.
    By doing them during bracket evaluation (before the order hot path), the actual
    order placement is reduced to just sign + POST (~100-200ms).
    """
    client = get_client()

    # Pre-fetch tick_size and neg_risk in parallel
    if token_id not in _tick_cache or token_id not in _neg_risk_cache:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_tick = pool.submit(client.get_tick_size, token_id)
            fut_neg = pool.submit(client.get_neg_risk, token_id)
            _tick_cache[token_id] = fut_tick.result()
            _neg_risk_cache[token_id] = fut_neg.result()
        logger.debug(f"Precached tick/neg_risk for {token_id[:16]}...")

    # Pre-set conditional allowance (blockchain tx, slowest call)
    if token_id not in _allowance_cache:
        try:
            set_conditional_allowance(client, token_id)
            _allowance_cache.add(token_id)
            logger.debug(f"Precached allowance for {token_id[:16]}...")
        except Exception as e:
            logger.warning(f"Precache allowance failed for {token_id[:16]}: {e}")


def place_buy_order(token_id: str, price: float, size_usdc: float) -> dict:
    """
    Places a FOK (Fill or Kill) BUY order for the YES outcome of a market.

    Args:
        token_id:   outcome token ID from market metadata (yes_token_id)
        price:      limit price as probability (0.01 to 0.99)
        size_usdc:  USDC amount to spend

    Returns:
        dict with order details (or simulated response in DRY_RUN mode)
    """
    if not (0.01 <= price <= 0.99):
        logger.error(f"Invalid price {price} - must be between 0.01 and 0.99")
        return {"status": "REJECTED", "reason": "invalid_price"}

    # CLOB V2 precision: maker amount (USDC) ≤ 2 decimals,
    # taker amount (shares) ≤ 4 decimals.  Raw float division
    # produces 15+ decimal places → 400 "invalid amounts" rejection.
    size_usdc = round(size_usdc, 2)
    num_shares = round(size_usdc / price, 4)

    if num_shares < 5:
        msg = f"Order too small: {num_shares:.2f} shares (minimum is 5)"
        logger.warning(msg)
        return {"status": "REJECTED", "reason": "below_minimum_shares"}

    if DRY_RUN:
        msg = (
            f"[DRY RUN] Would BUY {num_shares:.2f} shares "
            f"@ ${price:.4f} = ${size_usdc:.2f} USDC "
            f"| token={token_id[:16]}..."
        )
        logger.info(msg)
        print(msg)
        return {
            "orderID": "DRY_RUN",
            "status":  "simulated",
            "token_id": token_id,
            "price":    price,
            "size":     num_shares,
            "size_usdc": size_usdc,
        }

    # Live trading
    client = get_client()
    try:
        # Ensure conditional token allowance is set for this specific token.
        # Uses module-level cache to skip redundant on-chain txs for tokens
        # already approved this session (set by precache_token_metadata or prior orders).
        if token_id not in _allowance_cache:
            set_conditional_allowance(client, token_id)
            _allowance_cache.add(token_id)

        # get_tick_size and get_neg_risk are independent read-only GET
        # requests. Fetch them in parallel to cut pre-order latency roughly
        # in half. Both use the same client object; py-clob-client GET calls
        # are stateless so concurrent reads are safe.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_tick = pool.submit(client.get_tick_size, token_id)
            fut_neg  = pool.submit(client.get_neg_risk,  token_id)
            tick_size = fut_tick.result()
            neg_risk  = fut_neg.result()

        # CLOB V2 precision fix: use MarketOrderArgsV2 (amount=USDC, not size=shares).
        # Limit orders internally compute maker_amount = taker * price which
        # can reintroduce >2 decimal places, causing API 400 rejection.
        # Market orders round the USDC amount to 2 decimals FIRST, then derive
        # shares — naturally satisfying the API's 2-decimal maker-amount constraint.
        order_args = MarketOrderArgsV2(
            token_id=token_id,
            amount=size_usdc,           # USDC to spend (rounded to 2dp above)
            side=BUY,
            price=price,
            builder_code=BUILDER_CODE,  # CLOB V2: builder attribution
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        signed_order = client.create_market_order(order_args, options)
        resp = client.post_order(signed_order, OrderType.FOK)

        logger.info(
            f"BUY order response (full): {resp}"
        )
        logger.info(
            f"Order placed: id={resp.get('orderID')} | "
            f"token={token_id[:16]}... | "
            f"{num_shares:.2f} shares @ ${price:.4f} | "
            f"tick={tick_size} neg_risk={neg_risk} | "
            f"type=FOK | status={resp.get('status')}"
        )
        return resp

    except Exception as e:
        logger.error(f"BUY order failed for token {token_id[:16]}...: {e}")
        return {"status": "FAILED", "reason": str(e)}


def get_conditional_token_balance(token_id: str, client: ClobClient = None) -> float:
    """
    Returns the number of conditional token shares held for a given token.
    IMPORTANT: Sell orders check conditional token balance, NOT USDC balance.
    Returns 0.0 on failure.
    """
    if DRY_RUN:
        return 999.0  # Simulate holding shares in dry run

    try:
        if client is None:
            client = get_client()

        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
            signature_type=SIG_TYPE,
        )
        result = client.get_balance_allowance(params)

        if isinstance(result, dict) and "balance" in result:
            balance = float(result["balance"])
            # Conditional token balances are returned in raw units (no /1e6)
            logger.debug(f"Conditional balance for {token_id[:16]}...: {balance:.4f}")
            return balance
        return 0.0

    except Exception as e:
        logger.error(f"Failed to get conditional token balance: {e}")
        return 0.0


def place_sell_order(token_id: str, price: float, num_shares: float) -> dict:
    """
    Places a FAK (Fill and Kill) SELL order for YES outcome shares.
    Used for same-day exits and profit-taking.

    Key difference from buy: size is in SHARES (not USDC), and we must
    verify we actually hold enough conditional tokens before selling.

    Args:
        token_id:    outcome token ID
        price:       sell price as probability (0.01 to 0.99)
        num_shares:  number of shares to sell

    Returns:
        dict with order details (or simulated response in DRY_RUN mode)
    """
    if not (0.01 <= price <= 0.99):
        logger.error(f"Invalid sell price {price} - must be between 0.01 and 0.99")
        return {"status": "REJECTED", "reason": "invalid_price"}

    if num_shares < 5:
        msg = f"Sell order too small: {num_shares:.2f} shares (minimum is 5)"
        logger.warning(msg)
        return {"status": "REJECTED", "reason": "below_minimum_shares"}

    # Defense-in-depth: token IDs shorter than 40 chars are truncated/invalid.
    # Valid Polymarket token IDs are 77 characters. Anything shorter than 40
    # is definitely wrong and will fail the balance check with a misleading error.
    # (Root cause: position 136 Cape Town had a 16-char stub stored in DB.)
    if len(token_id) < 40:
        logger.error(
            f"Token ID too short ({len(token_id)} chars): {token_id!r} — "
            f"likely truncated in DB. Expected 77-char Polymarket token."
        )
        return {"status": "REJECTED", "reason": f"invalid_token_id_length_{len(token_id)}"}

    usdc_value = num_shares * price

    if DRY_RUN:
        msg = (
            f"[DRY RUN] Would SELL {num_shares:.2f} shares "
            f"@ ${price:.4f} = ${usdc_value:.2f} USDC "
            f"| token={token_id[:16]}..."
        )
        logger.info(msg)
        print(msg)
        return {
            "orderID": "DRY_RUN_SELL",
            "status": "simulated",
            "token_id": token_id,
            "price": price,
            "size": num_shares,
            "size_usdc": usdc_value,
            "side": "SELL",
        }

    # Live selling
    client = get_client()
    try:
        # Verify we hold enough conditional tokens
        balance = get_conditional_token_balance(token_id, client)
        if balance < num_shares:
            msg = (
                f"Insufficient conditional token balance: have {balance:.2f}, "
                f"need {num_shares:.2f} for token {token_id[:16]}..."
            )
            logger.warning(msg)
            if balance >= 5:
                # Sell what we have instead of failing
                num_shares = balance
                logger.info(f"Adjusted sell to available balance: {num_shares:.2f}")
            else:
                return {"status": "REJECTED", "reason": "insufficient_token_balance"}

        # Ensure allowance is set for selling this token (cached if already approved)
        if token_id not in _allowance_cache:
            set_conditional_allowance(client, token_id)
            _allowance_cache.add(token_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_tick = pool.submit(client.get_tick_size, token_id)
            fut_neg = pool.submit(client.get_neg_risk, token_id)
            tick_size = fut_tick.result()
            neg_risk = fut_neg.result()

        order_args = OrderArgs(
            price=price,
            size=num_shares,
            side=SELL,
            token_id=token_id,
            builder_code=BUILDER_CODE,  # CLOB V2: builder attribution
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        signed_order = client.create_order(order_args, options)
        
        # Try FAK first (immediate fill or cancel)
        try:
            resp = client.post_order(signed_order, OrderType.FAK)
        except Exception as fak_err:
            # FAK threw an exception (PolyApiException) instead of returning
            # a dict. This happens on "no orders found to match" errors.
            # Fall through to GTC retry.
            error_str = str(fak_err)
            logger.info(
                f"FAK sell threw exception (will retry GTC): {error_str[:150]}"
            )
            resp = {"status": "FAK_EXCEPTION", "reason": error_str}

        # If FAK fails due to no matching orders, fall back to GTC limit order.
        # GTC sits on the book until someone takes the other side.
        # This handles illiquid losing positions where no one is bidding.
        faK_status = resp.get("status", "FAILED")
        if faK_status not in ("MATCHED", "LIVE", "simulated") and faK_status != "FAK_EXCEPTION":
            error_msg = str(resp.get("error_message", resp.get("reason", "")))
            if not ("no orders found to match" in str(error_msg).lower() or
                    "no match" in str(error_msg).lower()):
                pass  # Some other failure — don't retry GTC
            else:
                faK_status = "FAK_EXCEPTION"  # Trigger GTC path below

        if faK_status == "FAK_EXCEPTION":
            # FAK failed (exception or no-match error). Retry as GTC.
            # Re-sign the order — signed_order may be single-use (nonce/replay protection).
            logger.info(
                f"FAK sell failed. Retrying as GTC limit order | "
                f"token={token_id[:16]}... | {num_shares:.2f} @ ${price:.4f}"
            )
            try:
                signed_order_gtc = client.create_order(order_args, options)
                resp = client.post_order(signed_order_gtc, OrderType.GTC)
                logger.info(
                    f"GTC sell order placed: id={resp.get('orderID')} | "
                    f"status={resp.get('status')}"
                )
            except Exception as gtc_err:
                logger.error(f"GTC sell fallback also failed: {gtc_err}")
                # Re-raise to be caught by outer handler
                raise

        logger.info(
            f"SELL order response (full): {resp}"
        )
        logger.info(
            f"SELL order placed: id={resp.get('orderID')} | "
            f"token={token_id[:16]}... | "
            f"{num_shares:.2f} shares @ ${price:.4f} | "
            f"type=FAK | status={resp.get('status')}"
        )
        return resp

    except Exception as e:
        logger.error(f"SELL order failed for token {token_id[:16]}...: {e}")
        return {"status": "FAILED", "reason": str(e)}


def place_ladder_bids(
    token_id: str,
    size_usdc: float,
    prices: list[float] = None,
    wait_seconds: float = 30.0,
) -> dict:
    """
    Places GTC limit buy orders at multiple price levels for illiquid markets.
    Waits up to `wait_seconds` for any fills, then cancels unfilled orders.

    Strategy: the probability model says this outcome is highly likely.
    No one is selling at cheap prices yet, so we place standing bids at
    $0.01 through $0.05 and wait. Any fill at these prices is enormous edge.

    Args:
        token_id:      YES outcome token ID
        size_usdc:     total USDC budget for the ladder (split across rungs)
        prices:        list of prices for each rung (default: [0.01..0.05])
        wait_seconds:  how long to wait for fills before cancelling (default: 30s)

    Returns:
        dict with:
            status: "FILLED" | "PARTIAL" | "NONE" | "FAILED"
            fills: list of filled order dicts
            cancelled: number of cancelled unfilled orders
            total_spent: USDC actually deployed
            avg_price: weighted average fill price (or 0)
            total_shares: total shares acquired
    """
    if prices is None:
        prices = [0.01, 0.02, 0.03, 0.04, 0.05]

    # Split budget equally across rungs
    per_rung = size_usdc / len(prices)

    if DRY_RUN:
        # Simulate: pretend the lowest rung fills
        best_price = prices[0]
        sim_shares = per_rung / best_price
        msg = (
            f"[DRY RUN] LADDER BID: {len(prices)} rungs "
            f"@ {[f'${p:.2f}' for p in prices]} | "
            f"${per_rung:.2f}/rung | token={token_id[:16]}..."
        )
        logger.info(msg)
        print(msg)
        return {
            "status": "FILLED",
            "fills": [{
                "orderID": "DRY_RUN_LADDER",
                "price": best_price,
                "size_usdc": per_rung,
                "shares": sim_shares,
            }],
            "cancelled": len(prices) - 1,
            "total_spent": per_rung,
            "avg_price": best_price,
            "total_shares": sim_shares,
        }

    # Live ladder bidding
    client = get_client()
    placed_orders = []

    try:
        # Pre-fetch tick_size and neg_risk once for all rungs
        set_conditional_allowance(client, token_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_tick = pool.submit(client.get_tick_size, token_id)
            fut_neg = pool.submit(client.get_neg_risk, token_id)
            tick_size = fut_tick.result()
            neg_risk = fut_neg.result()

        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)

        # Place GTC orders at each rung
        for price in prices:
            num_shares = per_rung / price
            if num_shares < 5:
                logger.debug(
                    f"Ladder rung ${price:.2f}: {num_shares:.1f} shares < 5 minimum, skipping"
                )
                continue

            order_args = OrderArgs(
                price=price,
                size=num_shares,
                side=BUY,
                token_id=token_id,
                builder_code=BUILDER_CODE,  # CLOB V2: builder attribution
            )

            try:
                signed_order = client.create_order(order_args, options)
                resp = client.post_order(signed_order, OrderType.GTC)
                order_id = resp.get("orderID", "")
                status = resp.get("status", "UNKNOWN")
                logger.info(
                    f"Ladder rung placed: ${price:.2f} x {num_shares:.1f} shares | "
                    f"id={order_id} status={status}"
                )
                placed_orders.append({
                    "orderID": order_id,
                    "price": price,
                    "size_usdc": per_rung,
                    "shares": num_shares,
                    "status": status,
                })
            except Exception as e:
                logger.warning(f"Ladder rung ${price:.2f} failed: {e}")

        if not placed_orders:
            return {
                "status": "FAILED",
                "fills": [],
                "cancelled": 0,
                "total_spent": 0.0,
                "avg_price": 0.0,
                "total_shares": 0.0,
            }

        # Wait for potential fills
        logger.info(
            f"Ladder: {len(placed_orders)} orders placed, waiting {wait_seconds}s for fills..."
        )
        time.sleep(wait_seconds)

        # Check which orders filled vs still open
        fills = []
        to_cancel = []

        try:
            open_orders = client.get_orders()
            open_ids = {o.get("id", o.get("orderID", "")) for o in open_orders}
        except Exception as e:
            logger.warning(f"Could not fetch open orders to check fills: {e}")
            open_ids = set()

        for order in placed_orders:
            oid = order["orderID"]
            if oid and oid not in open_ids:
                # Not in open orders -> it filled (or was matched)
                fills.append(order)
            elif oid in open_ids:
                to_cancel.append(oid)
            # If status was already MATCHED at placement, count as fill
            if order["status"] == "MATCHED" and order not in fills:
                fills.append(order)

        # Cancel unfilled orders
        cancelled = 0
        for oid in to_cancel:
            try:
                client.cancel(oid)
                cancelled += 1
                logger.debug(f"Cancelled unfilled ladder order: {oid}")
            except Exception as e:
                logger.warning(f"Failed to cancel ladder order {oid}: {e}")

        # Compute summary
        total_spent = sum(f["size_usdc"] for f in fills)
        total_shares = sum(f["shares"] for f in fills)
        avg_price = (
            sum(f["price"] * f["shares"] for f in fills) / total_shares
            if total_shares > 0 else 0.0
        )

        if fills:
            status = "FILLED" if len(fills) == len(placed_orders) else "PARTIAL"
        else:
            status = "NONE"

        logger.info(
            f"Ladder result: {status} | {len(fills)}/{len(placed_orders)} filled | "
            f"${total_spent:.2f} spent | {total_shares:.1f} shares @ avg ${avg_price:.4f} | "
            f"{cancelled} cancelled"
        )

        return {
            "status": status,
            "fills": fills,
            "cancelled": cancelled,
            "total_spent": total_spent,
            "avg_price": avg_price,
            "total_shares": total_shares,
        }

    except Exception as e:
        logger.error(f"Ladder bid failed for token {token_id[:16]}...: {e}")
        # Try to cancel any placed orders on failure
        for order in placed_orders:
            try:
                client.cancel(order["orderID"])
            except Exception:
                pass
        return {
            "status": "FAILED",
            "fills": [],
            "cancelled": 0,
            "total_spent": 0.0,
            "avg_price": 0.0,
            "total_shares": 0.0,
            "reason": str(e),
        }


def place_gtc_order(
    token_id: str,
    price: float,
    size_usdc: float,
    wait_seconds: float = 60.0,
) -> dict:
    """
    Places a single GTC limit buy order at a specific price, waits for a fill,
    then cancels if unfilled. Used as the last-trade-price fallback path for
    illiquid markets where no qualifying asks exist in the live order book.

    Unlike place_ladder_bids(), this places one rung at a real price anchor
    (the last trade price) rather than speculative $0.01-$0.05 rungs.

    Args:
        token_id:     outcome token ID
        price:        limit buy price as probability (0.01-0.99)
        size_usdc:    USDC amount to spend
        wait_seconds: seconds to wait for a fill before cancelling (default: 60)

    Returns:
        dict with keys:
            status:       "FILLED" | "NONE" | "FAILED"
            total_spent:  USDC actually deployed (0 if no fill)
            total_shares: shares acquired (0 if no fill)
            avg_price:    fill price (0 if no fill)
            order_id:     order ID string
    """
    num_shares = size_usdc / price

    if num_shares < 5:
        msg = f"GTC order too small: {num_shares:.2f} shares (minimum is 5) at price {price:.4f}"
        logger.warning(msg)
        return {"status": "FAILED", "reason": "below_minimum_shares",
                "total_spent": 0.0, "total_shares": 0.0, "avg_price": 0.0, "order_id": ""}

    if DRY_RUN:
        msg = (
            f"[DRY RUN] GTC BID: {num_shares:.2f} shares "
            f"@ ${price:.4f} = ${size_usdc:.2f} USDC "
            f"| token={token_id[:16]}..."
        )
        logger.info(msg)
        print(msg)
        return {
            "status": "FILLED",
            "total_spent": size_usdc,
            "total_shares": num_shares,
            "avg_price": price,
            "order_id": "DRY_RUN_GTC",
        }

    client = get_client()
    order_id = ""

    try:
        # Use pre-cached values if available (set by precache_token_metadata).
        # Falls back to live fetches if cache miss (non-sniper code paths).
        if token_id in _allowance_cache:
            logger.debug(f"GTC: using precached allowance for {token_id[:16]}")
        else:
            set_conditional_allowance(client, token_id)
            _allowance_cache.add(token_id)

        if token_id in _tick_cache and token_id in _neg_risk_cache:
            tick_size = _tick_cache[token_id]
            neg_risk = _neg_risk_cache[token_id]
            logger.debug(f"GTC: using precached tick/neg_risk for {token_id[:16]}")
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_tick = pool.submit(client.get_tick_size, token_id)
                fut_neg  = pool.submit(client.get_neg_risk,  token_id)
                tick_size = fut_tick.result()
                neg_risk  = fut_neg.result()
            _tick_cache[token_id] = tick_size
            _neg_risk_cache[token_id] = neg_risk

        order_args = OrderArgs(
            price=price,
            size=num_shares,
            side=BUY,
            token_id=token_id,
            builder_code=BUILDER_CODE,  # CLOB V2: builder attribution
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        signed_order = client.create_order(order_args, options)
        resp = client.post_order(signed_order, OrderType.GTC)

        order_id = resp.get("orderID", "")
        status   = resp.get("status", "UNKNOWN")

        logger.info(
            f"GTC order placed: id={order_id} | "
            f"{num_shares:.2f} shares @ ${price:.4f} | status={status}"
        )

        # If already matched at placement, return immediately
        if status == "MATCHED":
            return {
                "status": "FILLED",
                "total_spent": size_usdc,
                "total_shares": num_shares,
                "avg_price": price,
                "order_id": order_id,
            }

        # Wait for potential fill
        logger.info(f"GTC order {order_id}: waiting {wait_seconds}s for fill...")
        time.sleep(wait_seconds)

        # Check if the order filled (no longer in open orders)
        try:
            open_orders = client.get_orders()
            open_ids = {o.get("id", o.get("orderID", "")) for o in open_orders}
        except Exception as e:
            logger.warning(f"Could not fetch open orders to check GTC fill: {e}")
            open_ids = set()

        if order_id and order_id not in open_ids:
            # Not in open orders -> filled
            logger.info(f"GTC order {order_id} filled.")
            return {
                "status": "FILLED",
                "total_spent": size_usdc,
                "total_shares": num_shares,
                "avg_price": price,
                "order_id": order_id,
            }

        # Not filled - cancel and return NONE
        if order_id:
            try:
                client.cancel(order_id)
                logger.info(f"GTC order {order_id} cancelled (no fill after {wait_seconds}s).")
            except Exception as e:
                logger.warning(f"Failed to cancel GTC order {order_id}: {e}")

        return {
            "status": "NONE",
            "total_spent": 0.0,
            "total_shares": 0.0,
            "avg_price": 0.0,
            "order_id": order_id,
        }

    except Exception as e:
        logger.error(f"GTC order failed for token {token_id[:16]}...: {e}")
        if order_id:
            try:
                client.cancel(order_id)
            except Exception:
                pass
        return {
            "status": "FAILED",
            "reason": str(e),
            "total_spent": 0.0,
            "total_shares": 0.0,
            "avg_price": 0.0,
            "order_id": order_id,
        }


def cancel_order(order_id: str) -> dict:
    """Cancels an open order by ID."""
    if DRY_RUN:
        msg = f"[DRY RUN] Would CANCEL order {order_id}"
        logger.info(msg)
        print(msg)
        return {"status": "simulated_cancel", "orderID": order_id}

    client = get_client()
    try:
        result = client.cancel(order_id)
        logger.info(f"Order cancelled: {order_id}")
        return result
    except Exception as e:
        logger.error(f"Cancel failed for {order_id}: {e}")
        return {"status": "FAILED", "reason": str(e)}


def get_open_orders() -> list:
    """Returns all currently open orders on the account."""
    if DRY_RUN:
        return []
    client = get_client()
    try:
        return client.get_orders()
    except Exception as e:
        logger.error(f"Failed to fetch open orders: {e}")
        return []


if __name__ == "__main__":
    print(f"Executor loaded. DRY_RUN = {DRY_RUN}")
    print("Testing credential loading (no order placed)...")
    try:
        # Just instantiate client to verify credentials load
        client = get_client()
        print("Credentials loaded successfully.")
        print(f"CLOB host: {CLOB_HOST}")
        print(f"Chain ID:  {CHAIN_ID}")
    except EnvironmentError as e:
        print(f"ERROR: {e}")
