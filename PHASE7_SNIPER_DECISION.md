# Phase 7: Sniperweatherbot Review & Decision

## Deep Dive Findings
- sniperweatherbot/ is a parallel, high-speed implementation using:
  - WebSocket (wss://ws-subscriptions-clob.polymarket.com/ws/market) for real-time "new_market" push events (sub-second detection).
  - Forecast thread: blends GFS + ECMWF + GraphCast (Google DeepMind, weighted heavily as it outperforms traditional NWP on temp forecasts per Lam et al. 2023).
  - METAR aviation data for same-day.
  - Immediate GTC orders on top brackets via executor.
  - Target latency <500ms from event to order.
- Architecture different from main Ides-of-March/bot.py (which is scan-loop every ~30min with WU primary + Phase 1-6 conservative filters, prob fixes, calibration).
- sniper.py has extensive .bak history (fok_sweep, lowtemp, pegtobook, preSTATION) showing iterative experimentation.
- Current state (as of now): Running (process active), heavy on forecast refreshes + WS reconnects, but **0 positions/trades in dbs**, recent logs show no "trade/buy/fill" keywords – purely data gathering/forecasting mode.
- Symlinks to shared modules (executor, strategy, positions, etc.).
- Empty/zero trade dbs (positions.db 0 rows, trades.db empty).

## Comparison to Main Bot
- **Main (Ides-of-March)**: Mature, post-audit with:
  - Fixed prob model (no 0.65 cap on open thresholds, realistic 90%+ for strong cases).
  - Softened but safe filters (0.25 soft floor + high-edge exempt).
  - Early watch-only enforcement.
  - calibrate.py + integration with sigma v2 / wu_empirical.
  - Enhanced logging (PHASE6 diagnostics).
  - DRY_RUN paper mode.
  - Focus on WU + conservative "only best bucket per market".
- **Sniper**: Speed-focused sniper for edge on fresh listings. More aggressive WS push. GraphCast emphasis. Good for capturing liquidity at listing but higher variance/risk of bad fills or thin books.
- Overlap: Both use same underlying (markets, executor, strategy prob functions, risk).
- Differences in detection (scan vs WS), forecast blend, order timing.

## Decision
**Keep as separate for now.**
- Do not merge code (different paradigms: scan conservatism vs WS aggression).
- Run main bot as primary (with our fixes for "not stupid" trading).
- Keep sniper in "research/paper" mode or low allocation (e.g., via its own .env DRY_RUN or small size).
- Potential future: 
  - Use snipers WS detection to feed main bot.
