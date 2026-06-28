# Phase 5: Calibration System - Started

Created calibrate.py (skeleton that loads resolved + wu, computes stats, suggests sigmas).

Current data limitation: only 1 resolved with actual_temp in positions.db.
wu_positions: 567 forecasts, 0 actuals populated here (backfill points to /root/weatherbot DB).

Ran backfill dry-run: 0 to backfill in this context.

Recommendations from run:
- Use / integrate sigma_calibration_v2.py for scan_log + wu join (unbiased, large N).
- Rebuild wu_empirical after backfill.
- Per-city multipliers already in strategy (CITY_SIGMA_MULTIPLIER, SIGMA_BY_HORIZON_F).
- After more data, auto-suggest updates to PROB_FLOOR_BY_HORIZON, CITY_SIGMA etc.

Next: wire bot to optionally load from calibration JSON, add logging of forecast vs actual on every resolution.

## Implementation Started
- Created calibrate.py skeleton using local DB, wu_positions, resolved.
- Leverages existing wu_empirical, sigma_calibration_v2, backfill.
- Run shows data limitation (few actuals).
- To fully populate: python3 backfill_ground_truth.py (note DB path may need symlink or edit for Ides copy).
- Next steps: extend to output JSON suggestions, integrate load in strategy or bot startup.

