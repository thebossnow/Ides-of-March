"""
Check current OPEN positions against Wunderground.
Should we exit any of them now?
"""

import sqlite3, sys, json
sys.path.insert(0, "/root/weatherbot")
from wunderground_client import fetch_forecasts, wunderground_prob, get_forecast_for_date
from datetime import datetime, timezone

conn = sqlite3.connect("/root/weatherbot/positions.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT * FROM positions WHERE status = 'open'
    ORDER BY entry_time ASC
""").fetchall()

if not rows:
    print("No open positions.")
    conn.close()
    exit()

cities = list(set(r["city"] for r in rows))
print(f"Loading Wunderground for {len(cities)} cities...")
forecasts = fetch_forecasts(cities)

print()
print("=" * 90)
print("OPEN POSITIONS vs WUNDERGROUND NOW")
print("=" * 90)

exit_candidates = []
hold_positions = []

for r in rows:
    d = dict(r)
    city = d["city"]
    date_str = d["market_date"]
    unit = d.get("unit", "C")
    market_type = d["market_type"]
    bucket_low = d["bucket_low"]
    bucket_high = d["bucket_high"]

    city_data = forecasts.get(city, {})
    
    wu_prob, wu_reason = 0.0, "No data"
    wu_day = None
    if city_data and not city_data.get("error"):
        wu_prob, wu_reason = wunderground_prob(
            city_data, date_str, market_type, bucket_low, bucket_high
        )
        wu_day = get_forecast_for_date(city_data, date_str)

    if wu_day:
        wu_temp = wu_day.get("low_c") if market_type == "lowest" else wu_day.get("high_c")
        wu_temp_str = f"{wu_temp}°C" if wu_temp is not None else "?"
    else:
        wu_temp_str = "N/A"

    bot_prob = d["forecast_prob"] or 0
    bot_edge = d["edge"] or 0
    entry = d["entry_price"]
    size = d["size_usdc"]

    # Bucket display
    unit_sym = unit
    bl_d = f"{bucket_low}{unit_sym}" if bucket_low is not None else ""
    bh_d = f"-{bucket_high}{unit_sym}" if bucket_high is not None else ""
    bucket = f"[{bl_d}{bh_d}]"

    decision = ""
    if wu_prob <= 0.05:
        decision = "🔴 EXIT NOW — Wunderground says NO"
        exit_candidates.append((city, date_str, size, wu_prob, wu_reason))
    elif wu_prob <= 0.35:
        decision = "🟡 BORDERLINE — consider exit"
        hold_positions.append((city, date_str, size, wu_prob))
    else:
        decision = "✅ HOLD — Wunderground agrees"
        hold_positions.append((city, date_str, size, wu_prob))

    print(f"\n{decision}")
    print(f"  {city:14s} {date_str} {market_type:7s} {bucket:16s}")
    print(f"  Bot:  prob={bot_prob:.0%} edge={bot_edge:.0%} @ ${entry:.4f} size=${size:.2f}")
    print(f"  WU:   prob={wu_prob:.0%} temp={wu_temp_str} ({wu_reason})")

print()
print("=" * 90)
if exit_candidates:
    print("🔴 EXIT CANDIDATES (Wunderground says NO):")
    total = 0
    for city, dt, sz, p, reason in exit_candidates:
        print(f"  {city} {dt}  size=${sz:.2f}  prob={p:.0%}  {reason}")
        total += sz
    print(f"  Total at risk: ${total:.2f}")
elif hold_positions:
    print("✅ All positions have Wunderground support (prob >= 5%).")
    for city, dt, sz, p in hold_positions:
        print(f"  {city} {dt}  size=${sz:.2f}  prob={p:.0%}")

conn.close()
