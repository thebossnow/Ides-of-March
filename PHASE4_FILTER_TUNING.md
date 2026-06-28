# Phase 4: Safe Filter Tuning + Watch List Hygiene

## Current Issues (from audit)
- SOFT_MIN_PROB = 0.30 too high → almost no trades pass Phase 2 best-bucket.
- Leaks: Chicago, Denver, Helsinki (in WATCH_ONLY) still got traded and lost.
- Enforcement of watch-only happens late (after signal collection).
- Lower edge defensive system defined but not fully used in main WU path.
- Horizon floors high (0.38 same day).
- With Phase 3 realistic probs (now 90%+ for good open cases), we can safely allow more.

## Proposed Safe Changes
1. Lower SOFT_MIN_PROB / LOWEST_SOFT_MIN_PROB to 0.25 (from 0.30).
2. Add high-edge exemption: if best prob >=0.20 AND edge >=0.20, allow (for strong signals).
3. Strengthen watch-only: hard early SKIP + log, before prob calc.
4. Add note for auto-watch: cities with 3+ recent losses in last 30d → auto add (future).
5. Keep UNRESOLVED_EXPOSURE_CAP=0.50, MAX_DAILY_LOSS=8%, best-per-market.
6. Make PROB_FLOOR_BY_HORIZON slightly softer for same-day if high edge.
7. Integrate should_trade_lower_edge check optionally.

Rationale: Recent data shows over-conservative (0 trades most cycles). Phase 3 fixes give better conviction signals. Leaks indicate enforcement gap.

High edge exemption added via re.sub (prob and edge >=0.20 bypass).
All syntax OK.

