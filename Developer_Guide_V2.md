# Technical Documentation / Developer Guide

**Project Name:** Polymarket Weather Bot
**Version:** 2.0.0
**Last Updated:** 2026-04-04
**Author(s):** TheBossNow
**Repository:** Ides_of_March (private)

---

## Changelog from v1.0.0

**New modules added (position lifecycle management):**
- `positions.py` - SQLite-backed persistent position tracker
- `observed_temps.py` - Fetches actual observed temperatures for resolution
- `position_monitor.py` - Same-day exit signals and profit-taking logic
- `redeemer.py` - Gasless redemption of winning positions via Polymarket relayer
- `debug_balance.py` - Balance debugging utility

**Modified modules:**
- `bot.py` - Added 4-schedule architecture (30min scan, 15min fast monitor, daily resolution, daily report), dynamic bankroll via `get_current_bankroll()`, SQLite position recording, removed best-bucket-only filter, multi-bucket per city/date now allowed
- `strategy.py` - Added dynamic probability floor by forecast horizon (`PROB_FLOOR_BY_HORIZON`), updated `should_trade()` with dual-gate (edge AND probability floor)
- `executor.py` - Singleton CLOB client (no more per-call re-derivation), FOK order type, added `place_sell_order()` and `get_conditional_token_balance()`
- `requirements.txt` - Added `py-builder-relayer-client`, `eth-abi`, `pytz`

**Strategy changes:**
- Removed best-bucket-only filter. Multiple buckets per city/date are now allowed if each independently passes both the edge threshold AND the probability floor
- Signals sorted by probability descending so the most likely outcome fills first when daily spend limit is approached
- Daily spend cap reduced from 25% to 20% of bankroll

---

## Table of Contents
- [Overview](#overview)
- [Goals and Non-Goals](#goals-and-non-goals)
- [Architecture](#architecture)
- [Key Components / Modules](#key-components--modules)
  - [bot.py](#botpy)
  - [markets.py](#marketspy)
  - [weather.py](#weatherpy)
  - [strategy.py](#strategypy)
  - [executor.py](#executorpy)
  - [positions.py](#positionspy)
  - [observed_temps.py](#observed_tempspy)
  - [position_monitor.py](#position_monitorpy)
  - [redeemer.py](#redeemerpy)
  - [logger.py](#loggerpy)
  - [notifier.py](#notifierpy)
  - [set_allowances.py](#set_allowancespy)
  - [debug_balance.py](#debug_balancepy)
- [Supporting Files](#supporting-files)
  - [deploy.sh](#deploysh)
  - [github_push.sh / github_push.ps1](#github_pushsh--github_pushps1)
  - [requirements.txt](#requirementstxt)
  - [.env.template](#envtemplate)
  - [.gitignore](#gitignore)
- [Data Flow](#data-flow)
- [Dependencies](#dependencies)
- [Configuration and Environment](#configuration-and-environment)
- [Algorithms and Non-Obvious Logic](#algorithms-and-non-obvious-logic)
- [Error Handling and Edge Cases](#error-handling-and-edge-cases)
- [Testing Strategy](#testing-strategy)
- [Performance Considerations](#performance-considerations)
- [Security and Compliance](#security-and-compliance)
- [Known Limitations and Future Improvements](#known-limitations-and-future-improvements)
- [How to Run / Develop](#how-to-run--develop)
- [API Reference / Key Interfaces](#api-reference--key-interfaces)
- [Glossary](#glossary)

---

## Overview

The Polymarket Weather Bot is an automated trading bot that identifies and exploits pricing inefficiencies in Polymarket's temperature prediction markets. It fetches real-time weather forecasts from Open-Meteo, compares the forecast probability of a temperature outcome against the market's implied probability, and places limit buy orders on underpriced YES tokens via Polymarket's CLOB (Central Limit Order Book) API on Polygon Mainnet.

As of v2.0, the bot manages the full position lifecycle: entry, monitoring, exit (same-day loss-cutting and profit-taking), resolution validation against actual observed temperatures, and gasless on-chain redemption of winning positions. All positions are persisted in a SQLite database that survives restarts.

The bot runs on a multi-schedule architecture (30-min scan, 15-min fast monitor, daily resolution/redemption, daily report), supports 39 cities across 6 continents, and uses a Student's t-distribution probability model with fractional Kelly criterion position sizing. All activity is monitored via Telegram notifications with daily P&L reports.

## Goals and Non-Goals

**Goals:**

- Automatically discover and parse all active Polymarket temperature markets
- Generate accurate probability estimates from weather forecast data
- Identify positive-edge trades where forecast probability exceeds market price by a configurable threshold (default 15%) AND meets a dynamic probability floor
- Execute trades with conservative position sizing (fractional Kelly)
- Track all positions persistently in SQLite with full metadata
- Monitor open positions for same-day exit signals and profit-taking opportunities
- Resolve past positions against actual observed temperatures
- Automatically redeem winning positions for USDC via gasless relayer transactions
- Maintain a continuous heartbeat with the CLOB API to keep orders active
- Provide real-time trade alerts and daily summary reports via Telegram
- Dynamically fetch the real on-chain USDC balance for bankroll sizing
- Support both DRY_RUN (paper trading) and LIVE trading modes
- Run unattended on a VPS with automatic error recovery

**Non-Goals (out of scope):**

- Precipitation, volcano, earthquake, or non-temperature weather markets (planned but not yet implemented)
- Multi-exchange or cross-market arbitrage
- Historical backtesting engine
- Web UI or dashboard
- NO token (short) positions (buying NO when market overprices YES)

---

## Architecture

The bot follows a modular pipeline architecture with a multi-schedule event loop and persistent state via SQLite.

```
+------------------+     +------------------+     +------------------+
|   Open-Meteo     |     | Polymarket Gamma |     | Polymarket CLOB  |
|  Forecast API    |     |   (market list)  |     |  (prices/orders) |
+--------+---------+     +--------+---------+     +--------+---------+
         |                        |                    |         ^
         v                        v                    v         |
   +-----------+          +--------------+       +--------------+
   | weather.py|          |  markets.py  |       |  executor.py |
   +-----------+          +--------------+       | (buy + sell) |
         |                        |              +--------------+
         v                        v                    ^
   +----------------------------------------------------+     |
   |              bot.py (Orchestrator)                  |-----+
   |  - 30-min scan cycle (new trades + monitor)        |
   |  - 15-min fast monitor (positions <12h out)        |
   |  - Daily 00:05 UTC: resolve + redeem               |
   |  - Daily 23:55 UTC: daily report                   |
   +------+---------+---------+---------+---------------+
          |         |         |         |
          v         v         v         v
   +----------+ +--------+ +--------+ +-----------+
   |strategy.py| |logger.py| |notifier| |positions.py|
   +----------+ +--------+ |  .py   | | (SQLite)   |
                            +--------+ +-----+-----+
                                              |
          +-----------------------------------+-------------------+
          |                    |                                   |
          v                    v                                   v
   +----------------+  +-------------------+             +-----------+
   |observed_temps.py|  |position_monitor.py|             |redeemer.py|
   | (Archive API)   |  | (exits/profits)   |             | (Relayer) |
   +----------------+  +-------------------+             +-----------+
                                                               |
                                                               v
                                                     +------------------+
                                                     | Polymarket       |
                                                     | Relayer API      |
                                                     | (gasless redeem) |
                                                     +------------------+
```

**Design pattern:** Modular pipeline with multi-schedule polling and persistent state. Each module has a single responsibility and can be tested independently via `if __name__ == "__main__"` blocks.

**Runtime threads:**

1. Main thread: scheduler loop (`schedule` library, 30-second tick)
2. Heartbeat thread: sends CLOB heartbeats every 5 seconds (daemon)
3. Telegram thread: dedicated asyncio event loop for non-blocking sends (daemon)

**Scheduled jobs:**

| Schedule | Function | Purpose |
|---|---|---|
| Every 30 min | `run_cycle()` | Full scan: discover markets, calculate edges, place trades, monitor positions |
| Every 15 min | `_fast_monitor_cycle()` | Check exits/profit-taking for positions within 12h of resolution (no-op if none) |
| Daily 00:05 UTC | `_daily_resolve_and_redeem()` | Resolve past positions via observed temps, redeem winners |
| Daily 23:55 UTC | `_send_daily_report()` | Send daily P&L summary via Telegram |

**Persistent state:**

- `positions.db` (SQLite): All positions with full lifecycle (open, exited, resolved_won, resolved_lost, redeemed)
- `trade_log.csv`: Append-only trade execution audit trail
- `scan_log.csv`: Append-only scan decision audit trail

---

## Key Components / Modules

---

### bot.py

**Purpose:** Main entry point and orchestrator. Runs the multi-schedule loop, coordinates all other modules, and manages the full bot lifecycle including position monitoring, resolution, and redemption.

**Role in project:** The brain of the operation. Ties together market discovery, weather forecasting, probability estimation, trade execution, position management, resolution, redemption, logging, and notifications.

**Technical aspects:**

- **Multi-schedule architecture:** Uses the `schedule` library to manage 4 jobs: 30-min scan cycle, 15-min fast monitor, daily 00:05 UTC resolution + redemption, daily 23:55 UTC report. The main loop ticks every 30 seconds via `time.sleep(30)`.
- **Scan cycle (`run_cycle`, 4 phases):**
  - Phase 1: Iterates all active weather markets, parses metadata, fetches cached forecasts, calculates edge for each. Collects all qualifying signals. Now uses dual-gate filtering: `should_trade(edge, forecast_prob, market_date)` checks both edge threshold AND dynamic probability floor.
  - Phase 2: All qualifying signals pass through (best-bucket-only filter removed in v2). Signals sorted by probability descending so the most likely outcome fills first when daily spend limit is approached. Multi-bucket bets per city/date are now allowed when each independently passes both gates.
  - Phase 3: Executes orders sequentially, respecting a daily spend cap of 20% of bankroll. Records each successful trade in the SQLite positions database via `record_entry()`.
  - Phase 4: Calls `monitor_positions()` to check all open positions for same-day exit signals and profit-taking opportunities.
- **Dynamic bankroll (`get_current_bankroll`):** Fetches real on-chain USDC balance from the CLOB API at cycle start. Tries signature_type=1 first (works for proxy wallet), falls back to type=2, then falls back to $200 hardcoded. Balance is fetched fresh each cycle so Kelly sizing reflects actual available capital.
- **Fast monitor cycle (`_fast_monitor_cycle`):** Runs every 15 minutes but short-circuits immediately if `needs_fast_monitoring()` returns False (no positions within 12h of resolution). When active, calls `monitor_positions()` for higher-urgency exit checking.
- **Daily resolve and redeem (`_daily_resolve_and_redeem`):** Runs at 00:05 UTC. Step 1: `resolve_past_positions()` fetches actual observed temps from Open-Meteo Archive API, determines win/loss for each past-date position. Step 2: `redeem_all_winners()` submits gasless redemption transactions for all resolved winners.
- **Position tracking (dual-layer):** `_traded_tokens` in-memory set prevents re-entering the same token within a single day (resets at midnight UTC). `is_token_traded_today()` checks SQLite for persistence across restarts.
- **SQLite initialization:** Calls `init_db()` at startup to ensure the positions table exists.
- **Forecast cache:** `forecast_cache` dict keyed by city name. Each city's forecast is fetched once per cycle and reused across all markets for that city.
- **Heartbeat thread:** Background daemon thread sends CLOB heartbeats every 5 seconds. Uses the server-provided heartbeat_id protocol (start with empty string, use server response ID for next call). Includes re-auth on 401 errors and Telegram alerts after 10 consecutive failures.
- **Auth retry:** `_get_clob_client_safe()` wraps client creation with 3 retries and exponential backoff. Sends Telegram alert after 10 consecutive failures across cycles.
- **Graceful shutdown:** Catches `KeyboardInterrupt`, stops heartbeat thread, sends shutdown notification, and calls `notifier.shutdown()`.

**Configuration constants:**

| Constant | Default | Description |
|---|---|---|
| `BANKROLL` | Dynamic via `get_current_bankroll()` | Real on-chain USDC balance, fallback $200 |
| `SCAN_INTERVAL` | 30 | Minutes between standard scan cycles |
| `FAST_MONITOR_INTERVAL` | 15 | Minutes between fast monitor checks |
| `FORECAST_DAYS` | 6 | Max days ahead to fetch forecasts |
| `MIN_PRICE_FLOOR` | 0.005 | Skip markets priced below this (lowered from 0.05 in v1) |
| `MAX_DAILY_SPEND` | 20% of bankroll | Hard cap on daily deployment (was 25% in v1) |
| `RESOLVED_CHECK_HOUR/MINUTE` | 00:05 UTC | When to run daily resolution + redemption |

---

### markets.py

**Purpose:** Market discovery and metadata parsing. Fetches active weather markets from Polymarket's Gamma API and extracts structured data (city, date, temperature bucket, token IDs) from unstructured question text and slugs.

**Role in project:** The data ingestion and normalization layer. Converts raw Polymarket market objects into clean metadata dicts that the rest of the pipeline can consume.

**Technical aspects:**

- **Market discovery (`get_weather_markets`):** Paginates through the Gamma API (100 markets per page) filtering by temperature keywords in the question field. Keywords include: "temperature", "high temperature", degree symbols, "central park", etc. No auth required for Gamma API.
- **Dual-parser architecture (`parse_market_metadata`):** Attempts question-based parsing first (human-readable text), then falls back to slug-based parsing. If any field (city, date, bounds) fails in question parsing, the slug parser fills the gap. This hybrid approach handles 100% of known market formats.
- **City alias system (`CITY_ALIASES`):** 39 cities with multiple aliases each. Keys are lowercase strings checked against the question text. Values map to canonical city keys used by `weather.py`. Insertion order matters since Python dicts preserve order; more specific aliases (e.g., "new york city") come before less specific ones ("new york").
- **Slug city map (`_SLUG_CITY_MAP`):** Auto-built from `CITY_ALIASES` on first use. Converts hyphenated slug fragments ("new-york") to canonical keys ("NYC").
- **Slug parsing (`_parse_from_slug`):** Handles the deterministic slug format: `highest-temperature-in-{city}-on-{month}-{day}-{year}-{temp_suffix}`. Temp suffixes: range ("52-53f"), exact degree ("10c"), or-below ("20corbelow"), or-higher ("84forhigher").
- **Temperature bounds parsing (`_extract_temp_bounds`):** Regex-based extraction from question text. Handles formats: "be X or higher", "be X or lower", "between X and Y", "X-Y degrees", exact degree ("be 12 degrees C"). Includes Unicode dash normalization (`_normalize_dashes`) since Polymarket uses en-dash (U+2013) in range questions.
- **Date extraction (`_extract_date`):** Parses ISO dates and natural language dates ("April 3, 2026"). Falls back to current year if year is omitted.
- **Token ID extraction (`get_yes_token_id`):** Parses JSON fields (Gamma API returns clobTokenIds as JSON strings). Matches by outcome label ("Yes"/"True") first, falls back to index 0 (YES by Polymarket convention).
- **Price fetching:**
  - `get_midpoint_price`: Fetches order book, computes (best_bid + best_ask) / 2. Falls back to ask-only or bid-only if one side is empty. Preferred method, more reliable in thin markets.
  - `get_market_price`: Fetches last trade price. Used as fallback when order book is unavailable.
- **CLOB client factory (`get_client`):** Creates authenticated client with configurable signature type (env var `POLYMARKET_SIG_TYPE`, default 2 for Gnosis Safe proxy). Normalizes funder address to lowercase with whitespace stripped.

---

### weather.py

**Purpose:** Weather forecast data provider. Fetches daily maximum temperature forecasts from the Open-Meteo API for all supported cities.

**Role in project:** The external data source that provides the forecast "truth" the bot compares against market prices. Accuracy of this data directly determines profitability.

**Technical aspects:**

- **City database (`CITIES`):** 39 cities with latitude, longitude, and IANA timezone. Covers North America (14), South America (2), Europe (11), Asia (10), Oceania (2). Coordinates pinpoint the city center for forecast accuracy.
- **Dual endpoint fallback (`OPEN_METEO_URLS`):** Primary endpoint (`api.open-meteo.com`) and secondary (`ensemble-api.open-meteo.com`) use different CDN edges. If the primary fails (common with VPS IPs due to SSL/rate-limit issues), the secondary often works.
- **Retry with exponential backoff (`_fetch_with_retry`):** 3 retries per endpoint with 1s/2s/4s backoff. Retries on SSL errors, connection errors, timeouts, and 5xx server errors. Does NOT retry on 4xx client errors. Tries all endpoints before raising.
- **Forecast output format:** Returns `{date_str: temp_celsius}` dict. Example: `{"2026-04-03": 18.5, "2026-04-04": 21.2}`. All temperatures in Celsius; conversion to Fahrenheit happens in `strategy.py`.
- **API parameters:** Requests `temperature_2m_max` (daily max temp at 2 meters above ground), uses city-local timezone, Celsius output, configurable forecast_days (default 6).
- **Utility functions:** `celsius_to_fahrenheit()` and `fahrenheit_to_celsius()` for unit conversion. Also exported for use by `strategy.py` and `observed_temps.py`.
- **No API key required:** Open-Meteo is free for non-commercial use under CC BY 4.0 license.

---

### strategy.py

**Purpose:** Edge calculation, probability estimation, and position sizing. The quantitative core that decides whether a trade has positive expected value and how much capital to allocate.

**Role in project:** The decision-making engine. Takes a weather forecast and market price as inputs, outputs a trade/no-trade decision and a dollar amount.

**Technical aspects:**

- **Probability model (Student's t-distribution):** Uses `scipy.stats.t` centered on the forecast temperature. The t-distribution produces "fat tails" compared to a Normal distribution, meaning more probability mass on extreme outcomes. Critical at longer forecast horizons where weather models have higher uncertainty.
- **Dynamic sigma (forecast uncertainty):**
  - Calibrated per forecast horizon in Fahrenheit: day 0 = 2.0F, day 1 = 2.5F, day 3 = 5.0F, day 6 = 8.0F.
  - Based on NWS/ECMWF verification data (1-day MAE around 2-3F, 3-day around 4-5F).
  - Celsius markets get sigma divided by 1.8 so both units are treated equivalently in probability space.
- **Dynamic degrees of freedom (df):** Controls tail fatness. Day 0 = 20 (near-Normal), day 3 = 5 (pronounced fat tails), day 6 = 3 (very fat tails). Lower df = more probability on extreme outcomes = less overconfident bets at longer horizons.
- **Forecast probability (`forecast_probability`):** Computes P(bucket_low <= temp < bucket_high) using the CDF of the t-distribution. Handles open-ended buckets (low=None for "or below", high=None for "or higher"). Clamped to [0.001, 0.999].
- **Edge calculation (`find_edge`):** Simply `forecast_prob - market_price`. Positive edge means the market is underpricing the outcome.
- **Dynamic probability floor (NEW in v2):** `PROB_FLOOR_BY_HORIZON` dict prevents buying low-probability buckets even when the edge is mathematically positive. Same-day floor = 30%, 1-day = 20%, 2-day = 18%, 3+ days = 15%. This stops the trap of buying a 28% probability bucket at 12% market price (16% edge but loses 72% of the time).
- **Dual-gate `should_trade` (CHANGED in v2):** Now requires both `edge >= ENTRY_THRESHOLD` (15%) AND `forecast_prob >= probability_floor_for_horizon`. Previously only checked edge.
- **`get_prob_floor(market_date_str)` (NEW in v2):** Returns the probability floor for a given market date based on forecast horizon. Used by `bot.py` to log the specific reason a trade was rejected.
- **Kelly criterion position sizing (`kelly_position_size`):**
  - Formula: `kelly_full = (b * p - q) / b` where `b = (1/market_price) - 1` (net payout odds), `p = win_prob`, `q = 1 - p`.
  - Uses `KELLY_FRACTION = 0.15` (15% fractional Kelly) for conservative sizing.
  - Result clamped to [`MIN_POSITION_USDC` ($5), `MAX_POSITION_USDC` ($25)].
  - Returns 0 if Kelly is negative (no edge).

**Configuration constants:**

| Parameter | Default | Notes |
|---|---|---|
| `ENTRY_THRESHOLD` | 0.15 (15%) | Minimum edge to enter a trade |
| `EXIT_THRESHOLD` | 0.05 (5%) | Minimum edge to keep a position |
| `MAX_POSITION_USDC` | $25 | Hard cap per trade |
| `MIN_POSITION_USDC` | $5 | Minimum meaningful trade |
| `KELLY_FRACTION` | 0.15 (15%) | Fractional Kelly multiplier |
| `MIN_HOURS_TO_RES` | 2.0 | Skip markets resolving within 2 hours |
| `PROB_FLOOR_BY_HORIZON` | {0: 0.30, 1: 0.20, 2: 0.18, 3-6: 0.15} | **NEW:** Dynamic probability floor |

---

### executor.py

**Purpose:** Order placement and management via the Polymarket CLOB API. Handles buying YES tokens, selling positions, querying conditional token balances, and cancelling orders.

**Role in project:** The execution layer. Translates trade signals into real (or simulated) orders on the Polymarket exchange. Contains the DRY_RUN safety flag.

**Technical aspects:**

- **DRY_RUN safety flag:** `DRY_RUN = True` by default. When True, all order functions return simulated responses without touching the blockchain. Must be manually set to False for live trading.
- **Singleton CLOB client (CHANGED in v2):** `_init_client()` runs at module import time and creates a single `ClobClient` stored in `_client`. All functions reuse this singleton via `get_client()`. This fixed "invalid signature" errors caused by re-deriving API credentials on every call.
- **Buy order placement (`place_buy_order`, CHANGED in v2):**
  - Now uses `OrderType.FOK` (Fill or Kill) instead of GTC. Orders either fill immediately at the specified price or are cancelled entirely. This prevents stale limit orders from sitting on the book.
  - Validates price is between 0.01 and 0.99.
  - Calculates shares: `num_shares = size_usdc / price`. Rejects if below 5 shares (Polymarket minimum).
  - In live mode: calls `set_conditional_allowance` first (ERC-1155 approval per token), then fetches tick_size and neg_risk in parallel using `ThreadPoolExecutor(max_workers=2)`, then submits the order.
- **Sell order placement (`place_sell_order`, NEW in v2):**
  - Uses `OrderArgs(side=SELL, order_type=OrderType.FOK)` with `create_and_post_order()`.
  - Size is in SHARES (not USDC), unlike buy orders.
  - Checks conditional token balance before selling via `get_conditional_token_balance()`. If balance is insufficient but >= 5 shares, adjusts sell quantity to available balance rather than failing entirely.
  - Sets conditional token allowance before selling.
  - Parallel tick_size/neg_risk fetching same as buy path.
- **Conditional token balance (`get_conditional_token_balance`, NEW in v2):**
  - Queries `AssetType.CONDITIONAL` balance for a specific token ID.
  - Returns raw balance (no /1e6 division; conditional tokens are not denominated in USDC decimals).
  - Returns 999.0 in DRY_RUN mode to simulate holding shares.
- **Parallel pre-order fetches:** `get_tick_size` and `get_neg_risk` are independent GET requests fetched concurrently via `ThreadPoolExecutor`, cutting pre-order latency roughly in half.
- **Signature type:** Configurable via `POLYMARKET_SIG_TYPE` env var. Default 2 (Gnosis Safe-style proxy).
- **Order cancellation (`cancel_order`):** Cancels an open order by ID. Respects DRY_RUN.
- **Open orders query (`get_open_orders`):** Returns all currently open orders. Returns empty list in DRY_RUN mode.

---

### positions.py

**Purpose:** SQLite-backed persistent position tracker. Stores the full lifecycle of every position: entry, exit, resolution, and redemption with complete metadata.

**Role in project:** The backbone of the v2 position management system. Enables same-day exit signals, profit-taking, resolution validation, P&L tracking, and model calibration. Survives bot restarts.

**Technical aspects:**

- **Database schema:** Single `positions` table with 27 columns covering market identification (token_id, condition_id, slug, city, market_date), bucket definition (bucket_low, bucket_high, unit), entry details (entry_price, shares, size_usdc, entry_time, order_id), current state (status), exit details (exit_price, exit_time, exit_reason, exit_order_id), resolution details (actual_temp, actual_temp_source, resolved_time), P&L (pnl_usdc), metadata (question, forecast_prob, market_prob, edge, neg_risk, created_at).
- **Position statuses:** `open` (active position), `exited` (sold before resolution), `resolved_won` (market resolved in our favor), `resolved_lost` (market resolved against us), `redeemed` (winning payout claimed on-chain).
- **Thread-safe connections:** Uses `threading.local()` for per-thread SQLite connections. Each thread gets its own connection with `WAL` journal mode for concurrent reads and `busy_timeout=5000` for write contention.
- **Schema auto-initialization:** `_init_schema()` runs on first connection per thread via `_get_conn()`. Creates table and indexes if they don't exist. Safe to call multiple times.
- **Indexes:** On `status`, `token_id`, `market_date`, and `(city, market_date)` for efficient queries by the monitor and resolver.
- **DB path:** Configurable via `POSITIONS_DB_PATH` env var. Defaults to `positions.db` in the same directory as the script.
- **CRUD operations:**
  - `record_entry()`: Inserts a new open position. Called by `bot.py` after successful order placement. Returns row ID.
  - `record_exit()`: Updates status to `exited`, calculates P&L as `(exit_price - entry_price) * shares`. Called by `position_monitor.py`.
  - `record_resolution()`: Updates status to `resolved_won` or `resolved_lost`. Won P&L = `shares * (1.0 - entry_price)`. Lost P&L = `-size_usdc`. Called by `position_monitor.py` via `observed_temps.py`.
  - `record_redemption()`: Updates status from `resolved_won` to `redeemed`. Called by `redeemer.py`.
- **Query helpers:**
  - `get_open_positions()`: All positions with status='open', ordered by market_date.
  - `get_open_positions_for_date(date)`: Open positions for a specific market date.
  - `get_unresolved_past_positions()`: Open positions where market_date < today. These need resolution checking.
  - `get_unredeemed_winners()`: Positions with status='resolved_won'. These need redemption.
  - `is_token_traded_today(token_id)`: Checks if an open position exists for this token. Used by `bot.py` to prevent duplicate entries.
  - `get_pnl_summary()`: Aggregate stats across all positions (total, open, exited, won, lost, redeemed counts + total P&L, average P&L, total invested).
  - `get_calibration_data()`: Resolved positions with forecast vs. actual data for model tuning.

---

### observed_temps.py

**Purpose:** Fetches actual observed temperatures from the Open-Meteo Archive API and resolves positions by comparing actual temps against bucket boundaries.

**Role in project:** The truth oracle. Determines whether each past position won or lost based on real weather data. Also provides intra-day observed maximums for same-day exit signals.

**Technical aspects:**

- **Two data sources:**
  - `get_historical_max_temp(city, target_date)`: Uses the Open-Meteo Archive API (`archive-api.open-meteo.com`) for past dates. Returns ERA5 reanalysis data blended with station observations. Returns dict with `temp_c`, `temp_f`, `source`, `date`.
  - `get_current_day_max(city)`: Uses the Open-Meteo Forecast API hourly endpoint for today's running max. Fetches all hourly observations up to the current UTC time, returns the maximum. Returns dict with `temp_c`, `temp_f`, `source`, `hour_count`, `last_hour`.
- **Bucket result checking (`check_bucket_result`):** Determines if `actual_temp` falls within `[bucket_low, bucket_high)`. Returns `won` (bool), `margin` (distance from nearest boundary), and `boundary_flag` (True if within 1 degree of any boundary). Boundary-flagged results trigger a manual review alert via Telegram since ERA5 data may differ from the exact station Polymarket uses.
- **Batch resolution (`resolve_positions`):** Takes a list of position dicts, fetches actual temps (with per-city/date caching to avoid duplicate API calls), checks bucket results, and returns a list of resolution result dicts. Handles unit conversion (uses temp_f for F markets, temp_c for C markets).
- **Retry logic:** Same `_fetch_with_retry` pattern as `weather.py` (3 retries, exponential backoff, handles transient errors).
- **Reuses city coordinates:** Imports `CITIES` dict from `weather.py` to maintain a single source of truth for city coordinates.

---

### position_monitor.py

**Purpose:** Monitors open positions for exit signals. Implements two exit strategies: same-day loss-cutting and profit-taking.

**Role in project:** The active position management layer. Prevents holding near-certain losers to resolution and locks in profits when conditions warrant.

**Technical aspects:**

- **Same-day exit (`_check_same_day_exit`):**
  - Only applies to positions where `market_date == today`.
  - Fetches the current observed daily max via `get_current_day_max()`.
  - Triggers if observed max is 2+ degrees (`TEMP_EXIT_MARGIN_DEG`) above the bucket's high boundary. At that point, the temperature can only go higher and the position is very likely to lose.
  - Does NOT exit when current max is below `bucket_low`, because temperatures can still rise later in the day.
  - Returns an exit action dict with position_id, token_id, shares, reason, current_temp.
- **Profit-taking (`_check_profit_take`):**
  - Applies to all open positions.
  - Step 1: Fetches current market price via midpoint/last trade.
  - Step 2: Checks if unrealized profit >= 50% (`PROFIT_TAKE_THRESHOLD`).
  - Step 3: If profit threshold met, fetches updated weather forecast and recalculates probability.
  - Step 4: Sells only if updated forecast probability has dropped below 60% (`PROFIT_TAKE_PROB_CEILING`). If the model is still confident (prob >= 60%), holds through to resolution for full $1 payout.
  - This avoids selling positions that are highly likely to win just because they're already profitable.
- **Monitor execution (`monitor_positions`):**
  - Iterates all open positions.
  - Checks same-day exit first (higher priority), then profit-taking.
  - For each exit signal: determines sell price, calls `place_sell_order()`, records exit in SQLite via `record_exit()`, sends Telegram notification with P&L.
  - Returns summary dict: positions_checked, exits_triggered, exits_executed, errors.
- **Resolution of past positions (`resolve_past_positions`):**
  - Fetches all open positions where market_date < today.
  - Calls `observed_temps.resolve_positions()` to get actual temps and win/loss results.
  - Records each resolution in SQLite via `record_resolution()`.
  - Sends Telegram alerts for each resolution, with special boundary warnings for results within 1 degree.
  - Updates notifier daily stats via `record_settlement()`.
- **Fast monitoring gate (`needs_fast_monitoring`):**
  - Returns True if any open position is within 12 hours (`FAST_MONITOR_HOURS`) of resolution.
  - Used by `bot.py` to decide whether the 15-min fast loop should run or short-circuit.
  - Calculates hours to resolution by estimating market resolution at 23:59 local time on the market date (using `pytz` for timezone conversion).

**Configuration constants:**

| Constant | Default | Description |
|---|---|---|
| `TEMP_EXIT_MARGIN_DEG` | 2.0 | Degrees outside bucket before same-day exit |
| `PROFIT_TAKE_THRESHOLD` | 0.50 (50%) | Minimum unrealized profit to trigger profit-taking |
| `PROFIT_TAKE_PROB_CEILING` | 0.60 (60%) | Only sell if updated prob drops below this |
| `FAST_MONITOR_HOURS` | 12.0 | Threshold for activating the 15-min monitor loop |

---

### redeemer.py

**Purpose:** Redeems winning positions on resolved Polymarket markets. Winning YES shares pay $1.00 USDC each. Uses the Polymarket relayer infrastructure for gasless on-chain transactions.

**Role in project:** The cash-out layer. Converts resolved winning positions into actual USDC in the wallet without requiring POL gas.

**Technical aspects:**

- **Architecture:** Encodes a `redeemPositions()` call to the Conditional Tokens Framework (CTF) contract on Polygon, wraps it in a `SafeTransaction`, and submits via the `RelayClient` from `py-builder-relayer-client`. The relayer executes the transaction gaslessly through the user's Safe proxy wallet.
- **Calldata encoding (`_build_redeem_calldata`):** Uses `eth_abi.encode()` to ABI-encode the parameters: collateralToken (USDC.e address), parentCollectionId (zero bytes32), conditionId, and indexSets ([1, 2] for both YES and NO outcomes). Prepends the function selector `0x01b7037c`.
- **Relay client (`_get_relay_client`):** Creates a `RelayClient` with Builder API credentials (separate from the CLOB API credentials). Requires `POLYMARKET_BUILDER_KEY`, `POLYMARKET_BUILDER_SECRET`, `POLYMARKET_BUILDER_PASSPHRASE` in `.env`. Using dedicated builder creds avoids shared rate limits.
- **Single position redemption (`redeem_position`):** Builds calldata, determines target contract (CTF for normal markets, NegRiskAdapter for neg_risk markets), submits SafeTransaction via relayer, polls for confirmation with 60-second timeout. Returns status dict.
- **Batch redemption (`redeem_all_winners`):** Fetches all `resolved_won` positions from SQLite, redeems up to `MAX_REDEMPTIONS_PER_CYCLE` (10) with `REDEEM_DELAY_S` (5 seconds) between each to respect rate limits. Marks successfully redeemed positions as `redeemed` in SQLite. Sends Telegram notification per redemption. Stops early on rate limiting.
- **Contract addresses (Polygon Mainnet):**
  - CTF: `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
  - NegRiskAdapter: `0xC5d563A36AE78145C45a50134d48A1215220f80a`
  - USDC.e: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`
- **Known limitation:** The official `py-clob-client` has no `redeem` method (feature request #139 still open). This module uses the community `polymarket-apis` pattern (py-builder-relayer-client) as a workaround.
- **Rate limit handling:** Detects 429 / "quota" / "rate" in error responses and stops the batch early, reporting remaining positions for next cycle.

---

### logger.py

**Purpose:** CSV-based audit trail for all trade decisions and scan results. Every market scanned and every trade placed is logged regardless of outcome.

**Role in project:** The record-keeping system. Provides a complete audit trail for post-analysis, debugging, and performance evaluation.

**Technical aspects:**

- **Two log files:**
  - `trade_log.csv`: Records every executed trade with full context (17 columns including city, date, forecast temp, bucket bounds, probability, market price, edge, size, order ID, order status, question text).
  - `scan_log.csv`: Records every market scanned with the decision (TRADE, PASS, SKIP) and reason (9 columns). This captures the full decision funnel including markets that were skipped.
- **Auto-header creation (`_ensure_headers`):** Creates CSV files with headers on first write.
- **CLOB API key convention:** Reads `orderID` (not `id`) from order responses, matching the Polymarket CLOB API convention. Falls back to `id` for backward compatibility.
- **Question truncation:** Truncates question text to 200 characters to prevent CSV field overflow.
- **Recent trades query (`get_recent_trades`):** Returns last N trades as list of dicts via `csv.DictReader`.

---

### notifier.py

**Purpose:** Telegram notification system. Sends real-time trade alerts, cycle summaries, position exit notifications, resolution results, redemption confirmations, error notifications, and daily P&L reports.

**Role in project:** The monitoring and alerting layer. Lets the operator monitor bot activity remotely without SSH access.

**Technical aspects:**

- **Thread-safe async architecture:** Uses a dedicated asyncio event loop running in its own daemon thread (`telegram-loop`). This avoids conflicts with the main thread's scheduler and the heartbeat thread. All Telegram sends go through `_run_async()` which schedules coroutines on the dedicated loop via `asyncio.run_coroutine_threadsafe()`.
- **Retry logic:** 3 retries per message with 2-second exponential backoff. Returns True/False to indicate success.
- **Daily stats tracking:** Thread-safe dict protected by `threading.Lock()`. Tracks: start balance, current balance, trades entered/skipped/won/lost/in-progress, total spent. Reset at midnight UTC.
- **Notification types:**
  - `notify_startup`: Bot started, mode, bankroll, scan interval.
  - `notify_trade`: Per-trade alert with city, date, edge, size, price, probability, order status.
  - `notify_error`: Error alerts with component name and error text (truncated to 500 chars).
  - `notify_cycle_summary`: Markets scanned, trades placed, total spent, elapsed time.
  - `send_daily_report`: Full daily summary with balance, P&L (absolute and percentage), trade counts by status.
- **HTML parse mode:** All messages use `ParseMode.HTML` for formatting.
- **Clean shutdown:** `shutdown()` stops the dedicated event loop and joins the thread with 5-second timeout.

---

### set_allowances.py

**Purpose:** One-time blockchain setup script. Grants the Polymarket exchange contract permission to spend USDC.e (ERC-20 collateral approval). Also provides the `set_conditional_allowance()` function used by `executor.py` for per-token ERC-1155 approvals.

**Role in project:** A prerequisite step before live trading. Must be run once per wallet.

**Technical aspects:**

- **Collateral allowance (one-time):** Sets ERC-20 USDC.e approval via `client.update_balance_allowance()` with `AssetType.COLLATERAL`. Requires POL gas on the proxy wallet (1-2 POL).
- **Conditional token allowance (per-market):** `set_conditional_allowance()` is called by `executor.py` before each live order (both buy and sell). Sets ERC-1155 approval for the specific outcome token via `AssetType.CONDITIONAL`.
- **Interactive confirmation:** Prompts user before submitting on-chain transaction.
- **Verification step:** Reads back the allowance after setting it to confirm success.
- **Error guidance:** Lists the 3 most common failure causes.

---

### debug_balance.py

**Purpose:** Standalone diagnostic utility for debugging USDC balance queries. Tests both signature_type=1 and signature_type=2 to determine which returns the real cash balance for the proxy wallet.

**Role in project:** Development/debugging tool. Used to verify that `get_current_bankroll()` returns accurate on-chain balance.

**Technical aspects:**

- Creates a `ClobClient` and iterates through signature types [2, 1].
- For each: calls `update_balance_allowance()` (forces cache refresh), waits 3 seconds, then calls `get_balance_allowance()`.
- Divides raw balance by 1,000,000 (USDC has 6 decimals).
- Prints the first result that exceeds $10 as the real balance.

---

## Supporting Files

---

### deploy.sh

**Purpose:** One-time VPS setup and deployment script. Installs system packages, creates a Python virtual environment, installs dependencies, configures the firewall, and verifies the installation.

**Technical aspects:**

- 7-step automated setup: system update, package install (python3, pip, screen, git, ufw), firewall (SSH only), venv creation, pip install from requirements.txt, .env creation with secure permissions (chmod 600), and import test of all modules.
- Prompts for credentials if `.env` doesn't exist (input hidden with `read -s`).
- Provides post-setup instructions for running `set_allowances.py` and starting the bot in a `screen` session.

---

### github_push.sh / github_push.ps1

**Purpose:** Cross-platform scripts (Bash and PowerShell) to initialize a local git repo, create a private GitHub repository named "Ides_of_March", and push the initial commit.

**Technical aspects:**

- Uses `gh` CLI for GitHub operations (repo creation, auth).
- Safety check: verifies `.env` is in `.gitignore` before committing. Aborts if credentials could be exposed.
- Interactive confirmation before commit.

---

### requirements.txt

**Purpose:** Python dependency manifest.

**Dependencies:**

| Package | Version | Purpose |
|---|---|---|
| py-clob-client | >=0.21.0 | Polymarket CLOB API client |
| python-dotenv | >=1.0.0 | .env file loading |
| requests | >=2.31.0 | HTTP client for APIs |
| web3 | ==7.12.1 | Ethereum/Polygon blockchain interaction |
| schedule | >=1.2.0 | Job scheduling for scan cycles |
| python-dateutil | >=2.8.2 | Date parsing utilities |
| numpy | latest | Numerical computing |
| scipy | latest | Statistical distributions (Student's t) |
| pandas | latest | Data manipulation |
| python-telegram-bot | >=21.0 | Telegram Bot API |
| py-builder-relayer-client | >=0.0.1 | **NEW:** Gasless redemption via Polymarket relayer |
| eth-abi | >=5.0.0 | **NEW:** ABI encoding for CTF contract calls |
| pytz | >=2024.1 | **NEW:** Timezone-aware resolution time calculations |

---

### .env.template

**Purpose:** Template for credential configuration. Documents required environment variables.

**Required variables:** `POLYMARKET_PRIVATE_KEY` (hex private key), `POLYMARKET_FUNDER` (proxy wallet address).

**Additional variables for v2 features:** `POLYMARKET_BUILDER_KEY`, `POLYMARKET_BUILDER_SECRET`, `POLYMARKET_BUILDER_PASSPHRASE` (for gasless redemption), `POSITIONS_DB_PATH` (optional, SQLite database path).

---

### .gitignore

**Purpose:** Prevents sensitive and generated files from being committed.

**Excluded:** `.env`, `*.log`, `trade_log.csv`, `scan_log.csv`, `__pycache__/`, `*.pyc`, `venv/`.

**Note:** `positions.db` should also be added to `.gitignore` (currently not listed).

---

## Data Flow

### Trade Entry Flow (every 30 minutes)

1. **Market Discovery:** `bot.py` calls `markets.get_weather_markets()` which paginates through the Polymarket Gamma API, keyword-filtering for temperature markets.

2. **Metadata Parsing:** For each market, `markets.parse_market_metadata()` extracts: city, date, temperature bucket, YES token ID, condition_id. Uses dual-parser (question text first, slug fallback).

3. **Weather Forecast:** `bot.py` calls `weather.get_forecast(city, days=6)`. Results are cached per city per cycle.

4. **Unit Conversion:** `strategy.convert_forecast_to_market_unit()` converts Celsius forecast to Fahrenheit if market uses F.

5. **Probability Estimation:** `strategy.forecast_probability()` computes P(temp in bucket) using a Student's t-distribution with horizon-dependent sigma and df.

6. **Price Fetching:** `markets.get_midpoint_price()` fetches order book midpoint. Falls back to `get_market_price()` (last trade).

7. **Edge Calculation:** `strategy.find_edge()` computes `forecast_prob - market_price`.

8. **Dual-gate Trade Decision:** `strategy.should_trade(edge, forecast_prob, market_date)` checks edge >= 15% AND forecast_prob >= dynamic probability floor.

9. **Position Sizing:** `strategy.kelly_position_size()` computes USDC amount using fractional Kelly with real bankroll from `get_current_bankroll()`, clamped to $5-$25.

10. **Signal Sorting:** Signals sorted by probability descending. Most likely outcome fills first.

11. **Order Execution:** `executor.place_buy_order()` sets token allowance, fetches tick_size/neg_risk in parallel, submits FOK BUY order.

12. **Position Recording:** `positions.record_entry()` stores the position in SQLite with full metadata.

13. **Logging + Notification:** CSV logging via `logger.py`, Telegram alert via `notifier.py`.

### Position Monitoring Flow (every 30 min + 15 min fast)

14. **Same-day Exit Check:** `position_monitor._check_same_day_exit()` fetches intra-day observed max from `observed_temps.get_current_day_max()`. Triggers sell if temp exceeds bucket_high by 2+ degrees.

15. **Profit-taking Check:** `position_monitor._check_profit_take()` compares current market price to entry price. If 50%+ profit AND updated forecast prob < 60%, triggers sell.

16. **Sell Execution:** `executor.place_sell_order()` checks conditional token balance, submits FOK SELL order.

17. **Exit Recording:** `positions.record_exit()` updates SQLite with exit price, reason, and P&L.

### Resolution Flow (daily at 00:05 UTC)

18. **Historical Temp Fetch:** `observed_temps.get_historical_max_temp()` fetches actual recorded daily max from Open-Meteo Archive API.

19. **Bucket Comparison:** `observed_temps.check_bucket_result()` determines win/loss and flags boundary results.

20. **Resolution Recording:** `positions.record_resolution()` updates SQLite status to `resolved_won` or `resolved_lost` with actual temp and P&L.

### Redemption Flow (daily at 00:05 UTC, after resolution)

21. **Winner Identification:** `positions.get_unredeemed_winners()` queries SQLite for `resolved_won` positions.

22. **Calldata Encoding:** `redeemer._build_redeem_calldata()` ABI-encodes the `redeemPositions()` call.

23. **Gasless Submission:** `redeemer.redeem_position()` submits via `RelayClient` to the Polymarket relayer.

24. **Redemption Recording:** `positions.record_redemption()` updates status to `redeemed`.

---

## Dependencies

**Runtime:**

| Package | Purpose |
|---|---|
| `py-clob-client >=0.21.0` | Official Polymarket CLOB client. Orders, heartbeats, balances on Polygon. |
| `web3 ==7.12.1` | Pinned for py-clob-client compatibility. Ethereum/Polygon interaction. |
| `py-builder-relayer-client >=0.0.1` | **NEW:** Gasless transaction submission via Polymarket relayer for redemptions. |
| `eth-abi >=5.0.0` | **NEW:** ABI encoding for CTF contract redeemPositions() calldata. |
| `scipy` | Student's t-distribution CDF for probability estimation. |
| `python-telegram-bot >=21.0` | Async Telegram Bot API wrapper. |
| `requests` | HTTP client for Open-Meteo and Gamma API calls. |
| `schedule` | Lightweight job scheduler. |
| `python-dotenv` | Loads `.env` credentials. |
| `pytz >=2024.1` | **NEW:** Timezone-aware calculations for market resolution time estimation. |
| `numpy`, `pandas` | Numerical computing and data manipulation. |

**Why key choices:**

- `py-builder-relayer-client` is the only way to do gasless redemptions since `py-clob-client` has no redeem method.
- `eth-abi` is the standard for ABI encoding in the Python ecosystem, used to construct CTF contract calldata.
- `pytz` was added because `position_monitor.py` needs to estimate market resolution time in the market's local timezone.
- SQLite was chosen over Redis/Postgres for zero-configuration persistence. WAL mode handles the bot's read/write concurrency without a separate database server.

---

## Configuration and Environment

**Required environment variables (in `.env`):**

| Variable | Description |
|---|---|
| `POLYMARKET_PRIVATE_KEY` | Hex private key (0x-prefixed) for the Magic.link-exported wallet |
| `POLYMARKET_FUNDER` | Polymarket proxy wallet address (from polymarket.com/settings) |
| `POLYMARKET_SIG_TYPE` | Signature type: 1 (EOA) or 2 (Gnosis Safe proxy, default) |
| `TELEGRAM_TOKEN` | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for notifications |

**Additional variables for v2 redemption:**

| Variable | Description |
|---|---|
| `POLYMARKET_BUILDER_KEY` | Builder API key for relayer authentication |
| `POLYMARKET_BUILDER_SECRET` | Builder API secret |
| `POLYMARKET_BUILDER_PASSPHRASE` | Builder API passphrase |
| `POSITIONS_DB_PATH` | Optional: override SQLite database file path |

---

## Algorithms and Non-Obvious Logic

**1. Student's t-Distribution vs. Normal Distribution**

The bot uses a t-distribution instead of Normal for probability estimation. At short horizons (day 0-1), high df (12-20) makes it nearly identical to Normal. At longer horizons (day 5-6), low df (3.0-3.5) produces significantly fatter tails. Weather forecast errors at 5+ days are not normally distributed; they have occasional large misses that a Normal distribution underweights. Without fat tails, the bot would be overconfident on distant markets.

**2. Dynamic Probability Floor (NEW in v2)**

The edge threshold alone is insufficient to filter bad bets. Example: a 28% forecast probability bucket priced at 12% has a 16% edge (passes the 15% threshold), but it still loses 72% of the time. The probability floor (30% same-day, 20% 1-day, 15% 3+ days) prevents these traps. The floor is tighter at short horizons because the forecast is more reliable and we should demand higher confidence. At long horizons, the floor is looser because wide sigma means even centered buckets have moderate probabilities.

**3. Multi-Bucket Strategy (CHANGED in v2)**

v1 used a "best-bucket-only" filter that kept only the highest-edge signal per (city, date). v2 removes this. Multiple buckets per city/date are allowed if each independently passes both the edge threshold AND the probability floor. This is justified because the probability floor already prevents correlated low-quality bets. Signals are sorted by probability descending so the most likely outcome fills first if the daily spend limit is approached.

**4. Heartbeat Protocol**

The Polymarket CLOB requires a heartbeat every few seconds. You must start with an empty string ("") as the heartbeat_id. The server responds with a heartbeat_id that you must use on the next call. Using a self-generated ID causes the server to reject every heartbeat and cancel all open orders.

**5. Kelly Criterion Payout Odds**

The Kelly formula uses `b = (1/market_price) - 1` for payout odds, NOT `b = (1/win_prob) - 1`. Using win_prob for b collapses Kelly to near-zero.

**6. Unicode Dash Normalization**

Polymarket returns en-dashes (U+2013) in range questions. `_normalize_dashes()` replaces 6 Unicode dash variants with ASCII hyphens before regex parsing.

**7. Singleton CLOB Client (CHANGED in v2)**

`executor.py` now initializes a single `ClobClient` at module import time. Re-deriving API credentials on every call caused "invalid signature" errors because the CLOB API expects consistent derived credentials within a session.

**8. FOK vs GTC Orders (CHANGED in v2)**

Buy and sell orders now use Fill-or-Kill (`OrderType.FOK`) instead of Good-Til-Cancelled. FOK orders either fill immediately at the specified price or are cancelled entirely. This prevents stale limit orders from sitting on the book and accumulating unwanted exposure.

**9. Conditional Token Balance for Sells (NEW in v2)**

Sell orders must check `AssetType.CONDITIONAL` balance (number of outcome token shares held), NOT the USDC collateral balance. The raw balance value is used directly (no /1e6 division since conditional tokens are not denominated in USDC decimals). If balance is insufficient but >= 5 shares (Polymarket minimum), the sell quantity is adjusted down to the available balance.

**10. Gasless Redemption via Relayer (NEW in v2)**

The official py-clob-client has no redeem method. Redemption is done by ABI-encoding a `redeemPositions()` call to the CTF contract, wrapping it in a `SafeTransaction`, and submitting via the builder relayer. The relayer executes the transaction gaslessly through the user's Safe proxy. Uses builder API creds (separate from CLOB API creds) to avoid shared rate limits.

---

## Error Handling and Edge Cases

**Auth failures:** `_get_clob_client_safe()` retries 3 times with exponential backoff. After 10 consecutive failures across cycles, sends a Telegram alert.

**Weather API failures:** `_fetch_with_retry()` tries 2 endpoints with 3 retries each (6 total attempts). Markets for unreachable cities are skipped.

**Market parsing failures:** Unparseable markets are logged as SKIP with reason "unparseable_metadata" and silently skipped.

**Price fetch failures:** If midpoint price fails, bot retries with a fresh CLOB client, then falls back to last trade price.

**Degenerate inputs:** Kelly sizing returns 0 for negative Kelly, zero bankroll, or prices at 0.0/1.0. Probability clamped to [0.001, 0.999].

**Daily spend cap:** 20% of bankroll per day prevents catastrophic single-day loss.

**Insufficient sell balance:** If conditional token balance < requested sell shares but >= 5 (minimum), sell is adjusted to available balance. If < 5, order is rejected.

**SQLite concurrency:** WAL journal mode allows concurrent reads. `busy_timeout=5000` handles write contention between the main thread and monitor. Thread-local connections prevent cross-thread connection sharing.

**Resolution boundary cases:** Positions where actual temp lands within 1 degree of a bucket boundary are flagged for manual review via Telegram, since ERA5 reanalysis data may differ from the exact station Polymarket uses.

**Rate-limited redemptions:** Batch redemption stops on the first rate limit error, reports remaining positions, and retries next daily cycle.

**Balance query fallback:** `get_current_bankroll()` tries signature_type=1, then type=2, then falls back to $200 hardcoded. Prevents bot from crashing if balance API is temporarily unavailable.

---

## Testing Strategy

Each module has a standalone `if __name__ == "__main__"` test block:

- `weather.py`: Fetches and prints forecasts for all 39 cities.
- `markets.py`: Fetches markets from Gamma API, parses and prints the first 10.
- `strategy.py`: Runs comprehensive test scenarios including the new probability floor at different horizons. Tests same-day centered, adjacent, far-bucket, and open-ended cases with expected pass/fail outcomes.
- `executor.py`: Tests credential loading without placing orders.
- `logger.py`: Writes a sample row to `trade_log.csv`.
- `positions.py`: Full smoke test: insert, query, resolve (win), redeem, P&L summary, cleanup.
- `observed_temps.py`: Tests historical max (yesterday), current day max, and bucket checking with boundary cases.
- `position_monitor.py`: Prints configuration constants and available functions.
- `redeemer.py`: Checks dependencies, env vars, and tests calldata encoding with selector verification.
- `debug_balance.py`: Tests balance fetching with both signature types.

**DRY_RUN mode** is the primary integration test. Runs the full pipeline including position recording in SQLite, but simulates order placement. Recommended: 7+ days of paper trading before going live.

---

## Performance Considerations

**Scan cycle latency:** A typical cycle scans 100-300 markets in under 60 seconds. The bottleneck is CLOB API calls for price fetching (one per market).

**Parallel pre-order fetches:** `get_tick_size` and `get_neg_risk` run concurrently, cutting per-order latency by roughly 50%.

**Forecast cache:** One API call per city per cycle instead of one per market.

**Observed temp cache:** `resolve_positions()` caches historical temps per (city, date) to avoid duplicate Archive API calls when multiple positions share a city/date.

**SQLite WAL mode:** Enables concurrent reads without blocking. Write operations are sequential but fast (single-row inserts/updates).

**Singleton CLOB client:** Eliminates redundant credential derivation on each API call.

**FOK orders:** Eliminate the overhead of tracking and managing stale GTC orders on the book.

**Rate-limited redemption:** 5-second delay between redemptions and max 10 per cycle prevents relayer overload.

---

## Security and Compliance

**Credential management:**

- Private key and funder address stored in `.env` with chmod 600 permissions.
- Builder API credentials (key, secret, passphrase) also in `.env`.
- `.env` excluded from git via `.gitignore`.
- `github_push.sh` and `.ps1` verify `.env` is gitignored before committing.

**Network security:**

- VPS firewall configured via `ufw` to allow only SSH.
- All API communication over HTTPS.
- Polygon Mainnet (chain_id=137) transactions require the private key.

**Trading safety:**

- `DRY_RUN = True` by default.
- `MAX_POSITION_USDC = $25` hard cap per trade.
- `MAX_DAILY_SPEND = 20%` of bankroll per day.
- 15% fractional Kelly (conservative).
- Dynamic probability floor prevents buying low-probability buckets.
- Position monitoring cuts losses on same-day positions when observed temps blow past bucket.

**Data safety:**

- SQLite positions database persists across restarts.
- WAL mode prevents database corruption on unexpected shutdown.

---

## Known Limitations and Future Improvements

### Current Limitations

- **Temperature markets only.** Precipitation, volcano, and earthquake markets are not parsed.
- **Single-strategy.** Only one probability model (Student's t) and one sizing model (fractional Kelly).
- **No backtesting.** Cannot evaluate strategy performance on historical data.
- **No web UI.** Monitoring is via Telegram and CSV/SQLite files only.
- **ERA5 vs. official station data.** The Open-Meteo Archive API uses ERA5 reanalysis which may differ from the exact weather station Polymarket uses. Boundary results (within 1 degree) are flagged but could still be wrong.
- **No NO token (short) positions.** Cannot profit when market overprices YES.
- **`positions.db` not in `.gitignore`.** Should be added to prevent accidental commit.
- **Builder API credentials not in `.env.template`.** Template should be updated with the new v2 variables.
- **Single-VPS deployment.** No failover if VPS goes down.

### Future Improvements

- **Precipitation/extreme weather markets.** Extend parsing and strategy to handle rain, snow, hurricane, and volcano prediction markets.
- **NO token (short) positions.** When forecast probability is significantly below market price, buy NO tokens for the opposite bet.
- **Backtesting engine.** Replay historical forecast data against historical market prices to evaluate strategy parameters.
- **Multi-model ensemble.** Fetch forecasts from multiple sources (GFS, ECMWF, HRES) and combine for more accurate probability estimates.
- **Adaptive sigma calibration.** Use `get_calibration_data()` from `positions.py` to dynamically update the sigma table based on actual forecast error data.
- **Web dashboard.** Real-time dashboard showing open positions, P&L, forecast vs. actual, and market scanner output.
- **Formal test suite (pytest).** Unit tests for parsing, probability, Kelly sizing, position lifecycle, and integration tests for the full pipeline.
- **Containerization (Docker).** Package the bot with dependencies for reproducible deployment.
- **Multi-VPS failover.** Run a standby instance that activates if the primary goes down.
- **GTC orders with active management.** Explore returning to GTC orders with a cancel-and-replace strategy when prices move, rather than FOK which may miss fills in thin markets.
- **Partial fill handling.** Track partial FOK fills and adjust position sizes in SQLite accordingly.
- **Builder API credential rotation.** Implement automatic credential rotation for the relayer to handle expiring keys.
- **Position portfolio view.** Aggregate open positions by city, date, and net exposure for risk monitoring.

---

## How to Run / Develop

```bash
# --- Local Development ---

# 1. Clone the repo
git clone https://github.com/TheBossNow/Ides_of_March.git
cd Ides_of_March

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials
cp .env.template .env
# Edit .env with your private key, proxy wallet, and builder API creds

# 5. Test individual modules
python3 weather.py            # Test Open-Meteo API
python3 markets.py            # Test market discovery and parsing
python3 strategy.py           # Test probability, Kelly, and probability floor
python3 executor.py           # Test credential loading (no order placed)
python3 positions.py          # Test SQLite position lifecycle
python3 observed_temps.py     # Test historical and intra-day temp fetching
python3 redeemer.py           # Test calldata encoding and dependency check
python3 debug_balance.py      # Test on-chain balance fetching

# 6. Run bot (paper trading mode, DRY_RUN=True)
python3 bot.py

# --- VPS Deployment ---

# 1. Upload bot files to VPS
scp -r bot/ user@your-vps-ip:~/weatherbot/

# 2. Run the deployment script
ssh user@your-vps-ip
cd ~/weatherbot
bash deploy.sh

# 3. Install v2 dependencies
source venv/bin/activate
pip install py-builder-relayer-client eth-abi pytz

# 4. Set USDC.e allowance (one-time)
python3 set_allowances.py

# 5. Start bot in a screen session
screen -S weatherbot
source venv/bin/activate
python3 bot.py
# Ctrl+A then D to detach

# 6. Monitor
tail -f bot.log
tail -f scan_log.csv
sqlite3 positions.db "SELECT * FROM positions WHERE status='open'"
screen -r weatherbot      # Re-attach
```

---

## API Reference / Key Interfaces

### bot.py

```python
def run_cycle() -> None:
    """Full scan cycle: discover, calculate, trade, monitor."""

def get_current_bankroll() -> float:
    """Fetch real USDC balance from chain. Fallback: $200."""

def main() -> None:
    """Entry point. Starts DB, heartbeat, scheduler, runs indefinitely."""
```

### markets.py

```python
def get_weather_markets() -> list[dict]:
    """Fetch all active weather markets from Gamma API."""

def parse_market_metadata(market: dict) -> dict | None:
    """Extract city, date, bucket, token_id, condition_id from market."""

def get_midpoint_price(token_id: str, client=None) -> float | None:
    """Order book midpoint (bid+ask)/2."""

def get_market_price(token_id: str, client=None) -> float | None:
    """Last trade price. Fallback price source."""
```

### weather.py

```python
def get_forecast(city_name: str, days: int = 3) -> dict[str, float]:
    """Returns {date_str: temp_celsius} for the next N days."""

def celsius_to_fahrenheit(c: float) -> float:
def fahrenheit_to_celsius(f: float) -> float:
```

### strategy.py

```python
def forecast_probability(forecast_temp, bucket_low, bucket_high,
                         unit="F", model_uncertainty_deg=None,
                         market_date=None) -> float:
    """P(temp in bucket) using Student's t. Returns [0.001, 0.999]."""

def find_edge(forecast_prob: float, market_price: float) -> float:
    """Edge = forecast_prob - market_price."""

def should_trade(edge: float, forecast_prob: float = None,
                 market_date: str = None) -> bool:
    """True if edge >= 15% AND prob >= dynamic floor for horizon."""

def get_prob_floor(market_date_str: str = None) -> float:
    """Returns probability floor for the given forecast horizon."""

def kelly_position_size(bankroll, edge, win_prob, market_price=0.5) -> float:
    """Fractional Kelly position size in USDC. Clamped to [$5, $25]."""
```

### executor.py

```python
def get_client() -> ClobClient:
    """Returns the module-level singleton ClobClient."""

def place_buy_order(token_id: str, price: float, size_usdc: float) -> dict:
    """Place FOK BUY. Returns order response or simulated in DRY_RUN."""

def place_sell_order(token_id: str, price: float, num_shares: float) -> dict:
    """Place FOK SELL. Checks conditional token balance first."""

def get_conditional_token_balance(token_id: str, client=None) -> float:
    """Number of conditional token shares held for a given token."""

def cancel_order(order_id: str) -> dict:
def get_open_orders() -> list:
```

### positions.py

```python
def init_db() -> None:
    """Force DB initialization. Safe to call multiple times."""

def record_entry(...) -> int:
    """Record new position. Returns row ID."""

def record_exit(position_id, exit_price, exit_reason, exit_order_id=None) -> None:
    """Record position exit with P&L."""

def record_resolution(position_id, won, actual_temp=None, source=None) -> None:
    """Record win/loss with actual temp data."""

def record_redemption(position_id: int) -> None:
    """Mark resolved_won position as redeemed."""

def get_open_positions() -> list[dict]:
def get_unresolved_past_positions() -> list[dict]:
def get_unredeemed_winners() -> list[dict]:
def is_token_traded_today(token_id: str) -> bool:
def get_pnl_summary() -> dict:
def get_calibration_data() -> list[dict]:
```

### observed_temps.py

```python
def get_historical_max_temp(city: str, target_date: str) -> dict | None:
    """Actual recorded daily max from Open-Meteo Archive API."""

def get_current_day_max(city: str) -> dict | None:
    """Running daily max from hourly observations for today."""

def check_bucket_result(actual_temp, bucket_low, bucket_high) -> dict:
    """Returns {won, margin, boundary_flag}."""

def resolve_positions(positions: list[dict]) -> list[dict]:
    """Batch resolution: fetch actual temps, determine win/loss for each."""
```

### position_monitor.py

```python
def monitor_positions(notifier=None) -> dict:
    """Check all open positions for exit signals. Execute sells."""

def resolve_past_positions(notifier=None) -> dict:
    """Resolve past-date positions against actual observed temps."""

def needs_fast_monitoring() -> bool:
    """True if any position is within 12h of resolution."""
```

### redeemer.py

```python
def redeem_position(condition_id: str, neg_risk: bool = False) -> dict:
    """Redeem a single resolved position via relayer."""

def redeem_all_winners(notifier=None) -> dict:
    """Batch redeem all resolved_won positions. Returns summary."""
```

### logger.py

```python
def log_trade(...) -> None:
def log_scan(...) -> None:
def get_recent_trades(n: int = 10) -> list[dict]:
```

### notifier.py

```python
class TelegramNotifier:
    def send_message(text: str) -> bool
    def notify_startup(bankroll, mode, scan_interval) -> None
    def notify_trade(slug, city, date_str, edge, size_usdc, price, prob, order_status) -> None
    def notify_error(component: str, error: str) -> None
    def notify_cycle_summary(trades_placed, total_spent, markets_scanned, elapsed_s) -> None
    def send_daily_report(end_balance: float) -> None
    def record_trade(entered=True, size_usdc=0.0) -> None
    def record_settlement(won: bool) -> None
    def reset_daily(current_balance: float) -> None
    def shutdown() -> None
```

---

## Glossary

| Term | Definition |
|---|---|
| **CLOB** | Central Limit Order Book. Polymarket's order matching engine. |
| **Gamma API** | Polymarket's public market metadata API. No authentication required. |
| **FOK** | Fill or Kill. An order that must fill immediately and completely or be cancelled. Used in v2 instead of GTC. |
| **GTC** | Good-Til-Cancelled. An order that remains open until filled or cancelled. Used in v1, replaced by FOK in v2. |
| **YES/NO Token** | Binary outcome tokens on Polymarket. YES pays $1 if the event occurs, $0 otherwise. |
| **Edge** | The difference between the bot's forecast probability and the market's implied probability. |
| **Probability Floor** | **NEW in v2.** Minimum forecast probability required to trade, scaled by forecast horizon. Prevents buying buckets the model itself considers unlikely. |
| **Kelly Criterion** | Formula for optimal bet sizing that maximizes long-term growth rate. Fractional Kelly uses 15% of full Kelly. |
| **Sigma** | Standard deviation of forecast uncertainty. Controls the spread of the probability distribution. |
| **Degrees of Freedom (df)** | Parameter of the Student's t-distribution controlling tail fatness. Lower df = fatter tails. |
| **DRY_RUN** | Paper trading mode. Full pipeline runs but orders are simulated. |
| **Proxy Wallet** | A Polymarket-specific smart contract wallet (Gnosis Safe-style) that holds funds and executes trades. |
| **Funder** | The proxy wallet address. Required by py-clob-client for order signing. |
| **Signature Type** | Authentication method: 1 = EOA/Magic.link, 2 = Gnosis Safe proxy (default). |
| **CTF** | Conditional Tokens Framework. The smart contract on Polygon that manages Polymarket outcome tokens and redemptions. |
| **NegRiskAdapter** | A wrapper contract for markets that use negative risk. Redemptions go through this instead of CTF directly. |
| **ERC-20** | Standard fungible token interface (USDC.e on Polygon). |
| **ERC-1155** | Multi-token standard used by Polymarket for conditional outcome tokens. |
| **POL** | Polygon's native gas token (formerly MATIC). Required for on-chain transactions (except gasless redemptions). |
| **Heartbeat** | Periodic signal to the CLOB API to keep orders active. Without it, Polymarket cancels all open orders. |
| **Open-Meteo** | Free weather forecast API providing GFS/ECMWF model data without an API key. |
| **Open-Meteo Archive** | **NEW in v2.** Historical weather data API using ERA5 reanalysis. Used to determine actual temperatures for position resolution. |
| **ERA5** | ECMWF Reanalysis v5. Global atmospheric reanalysis dataset blended with station observations. |
| **Slug** | URL-friendly market identifier on Polymarket. |
| **Bucket** | A temperature range for a binary market (e.g., 52-53F, or 60F-or-higher). |
| **MAE** | Mean Absolute Error. Measure of forecast accuracy. |
| **Builder API** | **NEW in v2.** Separate API credentials for the Polymarket relayer/builder infrastructure. Used for gasless transactions. |
| **Relayer** | **NEW in v2.** Polymarket infrastructure that executes on-chain transactions gaslessly on behalf of users. |
| **WAL** | Write-Ahead Logging. SQLite journal mode that allows concurrent reads during writes. |

---

## Highlights and Best Features

**Full position lifecycle management (NEW in v2).** The bot now handles the complete trade lifecycle from entry through exit, resolution, and redemption. Positions are persisted in SQLite and survive restarts. This transforms the bot from a fire-and-forget order placer into a complete trading system.

**Dual-gate trade filtering (NEW in v2).** The dynamic probability floor prevents the mathematically tempting but practically losing trap of buying low-probability buckets with high edge. Combined with the edge threshold, this is a two-layer quality filter on trade signals.

**Same-day loss-cutting (NEW in v2).** When observed temperatures blow past a bucket boundary by 2+ degrees, the bot sells immediately rather than holding a near-certain loser to resolution. This preserves capital for better opportunities.

**Intelligent profit-taking (NEW in v2).** Positions with 50%+ unrealized profit are sold only when the updated forecast probability drops below 60%. This balances locking in gains against holding high-confidence positions through to the full $1 payout.

**Gasless on-chain redemption (NEW in v2).** Winning positions are automatically redeemed via the Polymarket relayer without requiring POL gas. This closes the loop on the entire trade lifecycle.

**Statistically principled probability model.** Student's t-distribution with horizon-dependent sigma and df remains a strong foundation. Fat tails at longer horizons prevent overconfident bets.

**Dynamic bankroll.** Real on-chain balance is fetched each cycle so Kelly sizing reflects actual available capital rather than a stale hardcoded value.

**Robust dual-parser architecture.** The question-first, slug-fallback parsing handles 100% of known Polymarket weather market formats.

**Thread-safe Telegram monitoring.** Dedicated asyncio loop for non-blocking alerts across multiple threads.

**Complete audit trail.** Dual CSV logging (trade_log + scan_log) plus SQLite position database provide comprehensive historical records for analysis and model calibration.
