"""
Full 4-day backtest: ALL bot trades vs Wunderground.
Tests: if we'd used Wunderground instead of GFS, which trades
would we have taken, which would we have blocked?
"""

import sqlite3, json, sys
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/root/weatherbot")
from wunderground_client import fetch_forecasts, wunderground_prob, get_forecast_for_date

conn = sqlite3.connect("/root/weatherbot/positions.db")
conn.row_factory = sqlite3.Row

# Wunderground gives 5-day forecasts. To be safe, use 4-day lookback.
cutoff = str((datetime.now(timezone.utc) - timedelta(days=4)).date())

rows = conn.execute("""
    SELECT * FROM positions
    WHERE entry_time >= ?
    ORDER BY entry_time ASC
""", (cutoff + "T00:00:00",)).fetchall()

print(f"Backtesting {len(rows)} trades from last 4 days (since {cutoff})")
cities = list(set(r["city"] for r in rows))
print(f"Loading Wunderground for {len(cities)} cities...")
forecasts = fetch_forecasts(cities)

print()
print("=" * 95)
print("FULL BACKTEST: Bot (GFS/t-dist) vs Wunderground (resolution source)")
print("=" * 95)

trades_taken = 0
trades_blocked = 0
trades_would_win = 0
trades_would_lose = 0
total_saved = 0.0
total_would_win_amount = 0.0
total_bot_lost = 0.0

results = []

for r in rows:
    d = dict(r)
    city = d["city"]
    date_str = d["market_date"]
    market_type = d["market_type"]
    unit = d.get("unit", "C")
    bucket_low = d["bucket_low"]
    bucket_high = d["bucket_high"]
    entry = d["entry_price"]
    size = d["size_usdc"] or 0
    status = d["status"]
    pnl = d["pnl_usdc"] or 0
    bot_prob = d["forecast_prob"] or 0
    bot_edge = d["edge"] or 0
    outcome = d.get("outcome", "")

    city_data = forecasts.get(city, {})
    wu_prob = 0.0
    wu_reason = "No Wunderground data"
    wu_day = None

    if city_data and not city_data.get("error"):
        wu_prob, wu_reason = wunderground_prob(
            city_data, date_str, market_type,
            bucket_low, bucket_high
        )
        wu_day = get_forecast_for_date(city_data, date_str)

    wu_temp = None
    if wu_day:
        wu_temp = wu_day.get("low_c") if market_type == "lowest" else wu_day.get("high_c")

    # Would Wunderground have approved this trade?
    WU_THRESHOLD = 0.35
    would_trade = wu_prob >= WU_THRESHOLD

    # Would it win based on Wunderground's forecast?
    would_win = False
    if wu_temp is not None:
        if market_type == "highest":
            if bucket_low is not None and bucket_high is not None:
                would_win = bucket_low <= wu_temp < bucket_high
            elif bucket_low is not None:
                would_win = wu_temp >= bucket_low
            elif bucket_high is not None:
                would_win = wu_temp <= bucket_high
        elif market_type == "lowest":
            if bucket_low is not None:
                would_win = wu_temp >= bucket_low
            elif bucket_high is not None:
                would_win = wu_temp <= bucket_high

    if would_trade:
        trades_taken += 1
        if would_win:
            trades_would_win += 1
            if entry < 0.99:
                win_return = (1.0 / entry - 1.0) * size
                total_would_win_amount += win_return
        else:
            trades_would_lose += 1
    else:
        trades_blocked += 1
        total_saved += size

    if status in ("resolved_lost", "closed") and pnl < 0:
        total_bot_lost += abs(pnl)

    # Bucket display
    bl_d = f"{bucket_low}" if bucket_low is not None else ""
    bh_d = f"-{bucket_high}" if bucket_high is not None else ""
    bucket = f"[{bl_d}{bh_d}]{unit}"

    emoji = "✅" if would_trade else "🛑"
    win_mark = " 🏆" if would_win else ""
    results.append((emoji, city, date_str, market_type, bucket,
                    bot_prob, bot_edge, entry, size, wu_prob,
                    wu_temp or "?", wu_reason[:60], win_mark))

# Print results sorted by date
for emoji, city, dt, mt, bucket, bp, be, ep, sz, wp, wt, wr, wm in results:
    wu_temp_display = f"{wt}°C" if wt != "?" else "?"
    print(f"{emoji} {city:14s} {dt} {mt:7s} {bucket:15s} | bot {bp:.0%}/{be:.0%} | WU {wp:.0%} {wu_temp_display:>4s}{wm}")
    if wp < WU_THRESHOLD:
        print(f"    ↳ WU says: {wr} → SKIP (save ${sz:.2f})")

print()
print("=" * 95)
print("BACKTEST SUMMARY")
print("=" * 95)
print(f"Total trades:           {len(rows)}")
print(f"Trades WU would take:   {trades_taken}  ({trades_would_win} would-win, {trades_would_lose} would-lose)")
print(f"Trades WU would block:  {trades_blocked}")
print(f"Money saved blocking:   ${total_saved:.2f}")
print(f"Would-win return:       ${total_would_win_amount:.2f}")
print(f"Bot actual lost (blocked trades): ${total_bot_lost:.2f}")
print(f"Net improvement:        ${total_saved - abs(total_bot_lost):+.2f} vs bot's results")
print()
print("Key: 🛑 = Wunderground would have blocked this trade")
print("     🏆 = Trade Wunderground says would win")
print(f"     WU threshold = {WU_THRESHOLD:.0%} (prob below this → skip)")

conn.close()
