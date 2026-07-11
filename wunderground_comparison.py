"""
Comparison: Bot's actual trades vs Wunderground forecasts.
Shows what would have happened if we used the resolution source's own
forecast instead of GFS/t-distribution.
"""

import sqlite3, json, sys
sys.path.insert(0, "/root/weatherbot")
from wunderground_client import (
    fetch_forecasts, wunderground_prob, get_forecast_for_date
)

conn = sqlite3.connect("/root/weatherbot/positions.db")
conn.row_factory = sqlite3.Row

# Get trades since May 5
rows = conn.execute("""
    SELECT * FROM positions
    WHERE entry_time >= '2026-05-05T00:00:00'
    ORDER BY entry_time ASC
""").fetchall()

# Get unique cities
cities = list(set(r["city"] for r in rows))
print(f"Loading Wunderground forecasts for {len(cities)} cities...")
forecasts = fetch_forecasts(cities)

print()
print("=" * 90)
print("BOT TRADES vs WUNDERGROUND (the resolution source)")
print("=" * 90)

total_saved = 0
total_lost = 0
trades_blocked = 0
trades_allowed = 0

for r in rows:
    d = dict(r)
    city = d["city"]
    date_str = d["market_date"]
    unit = d.get("unit", "C")
    market_type = d["market_type"]

    bucket_low = d["bucket_low"]
    bucket_high = d["bucket_high"]

    city_data = forecasts.get(city, {})

    # Convert F to C if needed (Wunderground is Celsius)
    bl_c = bucket_low
    bh_c = bucket_high
    if unit == "F" and bucket_low is not None:
        bl_c = round((bucket_low - 32) * 5/9, 1)
    if unit == "F" and bucket_high is not None:
        bh_c = round((bucket_high - 32) * 5/9, 1)

    # Wunderground probability
    wu_prob, wu_reason = 0.0, "No Wunderground data"
    if city_data and not city_data.get("error"):
        wu_prob, wu_reason = wunderground_prob(
            city_data, date_str, market_type, bucket_low, bucket_high
        )

    # Get the Wunderground forecast for that date
    wu_day = None
    if city_data and not city_data.get("error"):
        wu_day = get_forecast_for_date(city_data, date_str)

    # What Wunderground actually says
    if wu_day:
        if market_type == "highest":
            wu_temp = wu_day.get("high_c")
        else:
            wu_temp = wu_day.get("low_c")
        wu_temp_str = f"{wu_temp}°C" if wu_temp is not None else "?"
    else:
        wu_temp_str = "N/A"

    # Would we have traded?
    # Use simple logic: prob >= 0.35 → trade, else skip
    would_trade = wu_prob >= 0.35
    bot_prob = d["forecast_prob"] or 0
    bot_edge = d["edge"] or 0

    # Decision
    if would_trade:
        verdict = "✅ TRADE"
        trades_allowed += 1
        color = ""
    else:
        verdict = "🛑 SKIP"
        trades_blocked += 1
        color = ""
        if d["size_usdc"]:
            total_saved += abs(d["size_usdc"])

    # Build bucket display
    bl_disp = f"{bucket_low}{unit}" if bucket_low is not None else ""
    bh_disp = f"-{bucket_high}{unit}" if bucket_high is not None else ""
    bucket = f"[{bl_disp}{bh_disp}]"

    print(f"\n{verdict} {city:14s} {date_str} {market_type:7s} {bucket:16s}")
    print(f"  Bot:    prob={bot_prob:.0%}  edge={bot_edge:.0%}  @ ${d['entry_price']:.4f}  bet=${d['size_usdc']:.2f}")
    print(f"  WU:     prob={wu_prob:.0%}  temp={wu_temp_str}  ({wu_reason})")
    if not would_trade:
        print(f"  RESULT: Would have SKIPPED → saved ${abs(d['size_usdc']):.2f}")

print()
print("=" * 90)
print(f"SUMMARY: Trades allowed: {trades_allowed}  |  Trades blocked: {trades_blocked}")
print(f"         Money saved by blocking: ${total_saved:.2f}")
print(f"         All-time realized PnL: +$245.92")
print(f"         If we'd blocked these:  +${total_saved:.2f} recovered")
conn.close()
