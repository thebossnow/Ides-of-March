"""
executor.py - Order placement via Polymarket global CLOB API.
Uses py-clob-client with configurable signature_type (default: 2 for Gnosis Safe proxy).

IMPORTANT: DRY_RUN = True until paper trading validates your edge.
           Only set DRY_RUN = False after 7+ days of paper-trade verification.
"""

import os
import logging
import concurrent.futures
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
from set_allowances import set_conditional_allowance

load_dotenv()
logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID  = 137  # Polygon Mainnet

# Signature type: 1 = EOA/Magic.link proxy, 2 = Gnosis Safe-style proxy
# Set POLYMARKET_SIG_TYPE=1 in .env to override (default: 2)
SIG_TYPE  = int(os.getenv("POLYMARKET_SIG_TYPE", "2"))

# -----------------------------------------------------------------------
# SAFETY FLAG - Change to False ONLY when ready for live trading
# -----------------------------------------------------------------------
DRY_RUN = True


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
    c = ClobClient(
        CLOB_HOST,
        key=pk,
        chain_id=CHAIN_ID,
        signature_type=SIG_TYPE,
        funder=funder.lower().strip(),
    )
    c.set_api_creds(c.create_or_derive_api_creds())
    logger.info("ClobClient initialized (singleton). sig_type=%s", SIG_TYPE)
    return c

_client: ClobClient = _init_client()


def get_client() -> ClobClient:
    """Returns the module-level singleton ClobClient."""
    return _client


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

    num_shares = size_usdc / price

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
        # Must run before order submission; kept sequential (blockchain tx).
        set_conditional_allowance(client, token_id)

        # get_tick_size and get_neg_risk are independent read-only GET
        # requests. Fetch them in parallel to cut pre-order latency roughly
        # in half. Both use the same client object; py-clob-client GET calls
        # are stateless so concurrent reads are safe.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_tick = pool.submit(client.get_tick_size, token_id)
            fut_neg  = pool.submit(client.get_neg_risk,  token_id)
            tick_size = fut_tick.result()
            neg_risk  = fut_neg.result()

        order_args = OrderArgs(
            price=price,
            size=num_shares,
            side=BUY,
            token_id=token_id,
            order_type=OrderType.FOK,
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        resp = client.create_and_post_order(order_args, options)

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
    Places a FOK (Fill or Kill) SELL order for YES outcome shares.
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
            "actual_shares": num_shares,
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
                # Sell only what we hold; caller must use actual_shares from response
                logger.info(f"Adjusted sell to available balance: {balance:.2f} (was {num_shares:.2f})")
                num_shares = balance
            else:
                return {"status": "REJECTED", "reason": "insufficient_balance"}

        # Ensure allowance is set for selling this token
        set_conditional_allowance(client, token_id)

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
            order_type=OrderType.FOK,
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        resp = client.create_and_post_order(order_args, options)

        logger.info(
            f"SELL order response (full): {resp}"
        )
        logger.info(
            f"SELL order placed: id={resp.get('orderID')} | "
            f"token={token_id[:16]}... | "
            f"{num_shares:.2f} shares @ ${price:.4f} | "
            f"type=FOK | status={resp.get('status')}"
        )
        # Always include actual_shares so callers can compute correct P&L
        # even when balance was adjusted below the originally requested amount.
        resp["actual_shares"] = num_shares
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
        # Simulate a realistic fill at the middle rung (not the cheapest $0.01).
        # Using $0.01 gives 100x share count and massively overstates paper-trade P&L.
        sim_price = prices[len(prices) // 2]  # e.g. $0.03 for default 5-rung ladder
        sim_shares = per_rung / sim_price
        msg = (
            f"[DRY RUN] LADDER BID: {len(prices)} rungs "
            f"@ {[f'${p:.2f}' for p in prices]} | "
            f"${per_rung:.2f}/rung | sim fill @ ${sim_price:.2f} | token={token_id[:16]}..."
        )
        logger.info(msg)
        print(msg)
        return {
            "status": "FILLED",
            "fills": [{
                "orderID": "DRY_RUN_LADDER",
                "price": sim_price,
                "size_usdc": per_rung,
                "shares": sim_shares,
            }],
            "cancelled": len(prices) - 1,
            "total_spent": per_rung,
            "avg_price": sim_price,
            "total_shares": sim_shares,
        }

    # Live ladder bidding
    import time

    client = get_client()
    placed_orders = []
    fills = []  # populated during placement (MATCHED) and post-wait check (LIVE→filled)

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
                order_type=OrderType.GTC,
            )

            try:
                resp = client.create_and_post_order(order_args, options)
                order_id = resp.get("orderID", "")
                status = resp.get("status", "UNKNOWN")
                logger.info(
                    f"Ladder rung placed: ${price:.2f} x {num_shares:.1f} shares | "
                    f"id={order_id} status={status}"
                )
                # Only track orders that were actually accepted (MATCHED or LIVE).
                # Rejected/unknown statuses must not be counted as potential fills.
                if status in ("MATCHED", "LIVE"):
                    placed_orders.append({
                        "orderID": order_id,
                        "price": price,
                        "size_usdc": per_rung,
                        "shares": num_shares,
                        "placement_status": status,
                    })
                    if status == "MATCHED":
                        # Already filled at placement — count immediately as fill
                        fills.append(placed_orders[-1])
                else:
                    logger.warning(f"Ladder rung ${price:.2f} rejected by CLOB: status={status}")
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

        # Check which LIVE orders filled vs still open
        to_cancel = []

        try:
            open_orders = client.get_orders()
            open_ids = {o.get("id", o.get("orderID", "")) for o in open_orders}
        except Exception as e:
            logger.warning(f"Could not fetch open orders to check fills: {e}")
            open_ids = set()

        for order in placed_orders:
            oid = order["orderID"]
            if order["placement_status"] == "MATCHED":
                # Already counted as fill at placement time — skip
                continue
            # placement_status == "LIVE": order was resting; check if it filled
            if oid and oid not in open_ids:
                # No longer in open orders after wait → filled
                fills.append(order)
            elif oid in open_ids:
                to_cancel.append(oid)

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
