Here's a clear breakdown of the differences:

**bot_FIXED.py is the simpler, earlier version. bot.py is the more advanced version.**

The current `bot.py` adds significant functionality on top of `bot_FIXED.py`:

**1. Dynamic Bankroll (vs. hardcoded)**
`bot.py` fetches the real USDC balance via `get_current_bankroll()` at runtime. `bot_FIXED.py` uses a hardcoded `BANKROLL = 200.0`.

**2. Position Tracking (SQLite)**
`bot.py` imports and uses `positions.py`, `position_monitor.py`, and `redeemer.py` for full position lifecycle management. `bot_FIXED.py` has none of this.

**3. Illiquid Market / Ladder Bidding**
`bot.py` detects illiquid books and routes them to `place_ladder_bids()` with a $0.01-$0.05 rung strategy. `bot_FIXED.py` only uses `place_buy_order()`.

**4. Multi-Bucket Strategy (vs. best-bucket-only)**
`bot.py` allows multiple qualifying buckets per city/date (sorted by probability, all meeting edge + prob floor). `bot_FIXED.py` only keeps the single best-edge bucket per city/date.

**5. Probability Floor**
`bot.py` uses `get_prob_floor(date_str)` for a horizon-adjusted floor and passes it into `should_trade()`. `bot_FIXED.py` uses a simpler `should_trade(edge)` with no prob floor logic.

**6. Additional Scheduled Jobs**
`bot.py` adds: a 15-min fast monitor loop (for positions near resolution), a daily 00:05 UTC resolution checker, and daily redemption of winning positions. `bot_FIXED.py` only has the 30-min scan.

**7. MIN_PRICE_FLOOR**
`bot.py` uses `0.005` (0.5 cents). `bot_FIXED.py` uses `0.05` (5 cents). The current bot trades much thinner books.

**8. Daily Spend Limit**
`bot.py` uses 20% of the live bankroll. `bot_FIXED.py` uses 25% of the hardcoded bankroll.

**9. Duplicate Trade Check**
`bot.py` checks both the in-memory set AND the SQLite DB (`is_token_traded_today()`). `bot_FIXED.py` only checks the in-memory set.

In short: `bot.py` is a fully-featured version with position management, illiquid market support, and automated resolution/redemption. `bot_FIXED.py` is a leaner trading-only loop without any of those layers.