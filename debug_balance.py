import os
import time
from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient, BalanceAllowanceParams, AssetType

load_dotenv()

print("=== Polymarket Balance Debug (trying signature_type in params) ===")

client = ClobClient(
    host="https://clob.polymarket.com",
    key=os.getenv("POLYMARKET_PRIVATE_KEY"),
    chain_id=137,
    signature_type=int(os.getenv("POLYMARKET_SIG_TYPE", "2")),
    funder=os.getenv("POLYMARKET_FUNDER").strip().lower()
)

# Set API credentials (this is required for balance calls)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

print("Funder address:", os.getenv("POLYMARKET_FUNDER"))
print("Client signature_type:", os.getenv("POLYMARKET_SIG_TYPE", "2"))

for sig in [2, 1]:
    print(f"\n--- Trying signature_type = {sig} ---")
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=sig   # This is the key part many users needed
    )
    
    # Force refresh the cache (official method)
    print("Calling update_balance_allowance...")
    try:
        client.update_balance_allowance(params)
        time.sleep(3)
    except Exception as e:
        print("Update warning:", e)
    
    # Now get the balance
    result = client.get_balance_allowance(params)
    print("Full response:", result)
    
    if isinstance(result, dict) and "balance" in result:
        balance_usdc = int(result.get("balance", 0)) / 1_000_000
        print(f"✅ Available USDC with signature_type={sig}: {balance_usdc:.2f}")
        if balance_usdc > 10:  # close to your ~463
            print("🎉 This looks like your real cash balance!")
            break
    else:
        print("Unexpected response format")
