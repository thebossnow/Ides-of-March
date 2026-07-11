"""
Clean backtest: only trades where Wunderground actually has forecast data.
Filters out OnChainDetect artifacts and pre-May-5 trades.
"""

import sqlite3, sys, json
sys.path.insert(0, "/root/weatherbot")
from wunderground_client import fetch_forecasts, wunderground_prob, get_forecast_for_date

conn = sqlite3.connect("/root/weatherbot/positions.db")
conn.row_factory = sqlite3.Row

# Get trades from May 5+ (in forecast window)
rows = conn.execute("""
    SELECT * FROM positions
    WHERE entry_time >= '2026-05-04T00:00:00'
      AND city != 'OnChainDetect'
    ORDER BY entry_time ASC
""").fetchall()

print(f"Trades May 4+: {len(rows)}")
cities = list(set(r["city"] for r in rows))
forecasts = fetch_forecasts(cities)

print()
print("=" * 95)
print("CLEAN BACKTEST: Bot vs Wunderground (trades with actual WU forecast data)")
print("=" * 95)
print(f"{'':4s} {'City':14s} {'Date':12s} {'Type':7s} {'Bucket':18s} {'Bot p/e':>8s}  {'WU p':>5s} {'WU':>4s}  {'Verdict'}")
print("-" * 95)

WU_THRESHOLD = 0.35
took = 0
blocked = 0
saved = 0.0
would_win = 0
would_lose = 0
win_return = 0.0

for r in rows:
    d = dict(r)
    city = d["city"]
    date_str = d["market_date"]
    mt = d["market_type"]
    unit = d.get("unit", "C")
    bl = d["bucket_low"]
    bh = d["bucket_high"]
    entry = d["entry_price"]
    size = d["size_usdc"] or 0
    bot_p = d["forecast_prob"] or 0
    bot_e = d["edge"] or 0

    cd = forecasts.get(city, {})
    if cd.get("error") or not cd.get("forecast_days"):
        continue  # skip if no WU data

    wu_p, wu_reason = wunderground_prob(cd, date_str, mt, bl, bh)
    wu_day = get_forecast_for_date(cd, date_str)
    
    # Skip if no WU forecast for this date (outside 5-day window)
    if not wu_day:
        continue

    wu_temp = wu_day.get("low_c") if mt == "lowest" else wu_day.get("high_c")
    wu_temp_str = f"{wu_temp}°C" if wu_temp else "?"

    would_trade = wu_p >= WU_THRESHOLD

    # Would win based on WU forecast?
    win = False
    if wu_temp is not None:
        if mt == "highest":
            if bl is not None and bh is not None:
                win = bl <= wu_temp < bh
            elif bl is not None:
                win = wu_temp >= bl
            elif bh is not None:
                win = wu_temp <= bh
        elif mt == "lowest":
            if bl is not None:
                win = wu_temp >= bl
            elif bh is not None:
                win = wu_temp <= bh

    if would_trade:
        took += 1
        if win:
            would_win += 1
            if entry < 0.99:
                win_return += (1.0/entry - 1.0) * size
        else:
            would_lose += 1
    else:
        blocked += 1
        saved += size

    # Format
    bl_d = f"{bl}" if bl is not None else "-inf"
    bh_d = f"-{bh}" if bh is not None else ""
    bucket = f"[{bl_d}{bh_d}]{unit}"

    emoji = "✅" if would_trade else "🛑"
    wm = "🏆WIN " if win else ("❌LOSE" if would_trade else "      ")
    pe = f"{bot_p:.0%}/{bot_e:.0%}"
    print(f"{emoji}  {city:14s} {date_str} {mt:7s} {bucket:18s} {pe:>8s}  {wu_p:4.0%} {wu_temp_str:>4s}  {wm}")

print("-" * 95)
print()
print(f"TRADES TAKEN:   {took}  (would-win: {would_win}, would-lose: {would_lose})")
print(f"TRADES BLOCKED: {blocked}")
print(f"Money saved:    ${saved:.2f}")
if would_win > 0:
    print(f"Would-win P&L:  ${win_return:.2f}")
    print(f"Net outcome:    ${win_return - would_lose*3:.2f}  (wins - blocked losses)")
print()
print(f"COMPARISON: Bot took {took+blocked} trades → Wunderground says only {took} were worth it.")
print(f"            Blocked {blocked}/{took+blocked} ({blocked/(took+blocked)*100:.0f}%) = ${saved:.2f} saved")

conn.close()
