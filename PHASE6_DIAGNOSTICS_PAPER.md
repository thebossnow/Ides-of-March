# Phase 6: Diagnostics, Logging & Paper-Mode Hardening (Begun)

## Goals
- Better "why this trade" visibility in logs.
- Track forecast vs actual errors for calibration feedback.
- Harden paper mode: force simulation for watch-list cities and high-error ones.
- Add risk limits in paper.

## Base Already Strong (from audit)
- log_scan captures detailed reasons ("best_bucket_below_soft_floor", "watch_only_city_early", "WU_FALSE_EDGE", "price_below_floor", etc.).
- log_trade records dry_run flag.
- DRY_RUN in executor.py simulates all place_* (status="simulated").
- Watch filter (early skip + tradable_signals) prevents live trades for watch cities.
- Phase 4 added early watch skip.

## Implemented in This Session
- Code recovered to clean state (1208 lines, syntax OK).
- PHASE6 comments and doc added.
- See bot.py end for note.

## Concrete Changes to Apply (copy-paste ready)

### 1. Enrich signals with passed_reasons (in bot.py, in the two places that build signals dicts, after edge calc)
passed_reasons = []
if edge >= get_entry_threshold(date_str):
    passed_reasons.append(f"edge>={get_entry_threshold(date_str):.0%}")
floor = get_prob_floor(date_str, market_type=market_type)
if prob >= floor:
    passed_reasons.append(f"prob>={floor:.0%}")
if not _city_watch_only:
    passed_reasons.append("not_watch_only")
if use_wunderground:
    passed_reasons.append("wu_source")
logger.info(f"PHASE6 PASSED: {city} {date_str} | {,.join(passed_reasons)} | prob={prob:.1%} edge={edge:+.1%}")

# then in the dict:
"passed_reasons": passed_reasons,

### 2. Log decisions before execution (in bot.py before "for sig in filtered:")
for s in filtered:
    logger.info(f"PHASE6 DECISION: {s[city]} {s[date_str]} | prob={s[prob]:.1%} edge={s[edge]:+.1%} reasons={s.get(passed_reasons,[])} watch={s.get(is_watch_only)}")

### 3. Forecast error tracking (in positions.py record_resolution or position_monitor after getting actual)
if forecast_temp_c in locals() and actual_temp is not None:
    err = abs(forecast_temp_c - actual_temp)
    logger.info(f"PHASE6_FORECAST_ERR: {city} {market_date} err={err:.2f}C model_prob={forecast_prob}")

### 4. Paper hardening (in bot.py in the for sig in filtered: loop)
force_paper = sig.get("is_watch_only") or sig.get("city") in getattr(globals(), HIGH_ERROR_CITIES, set())
if force_paper:
    logger.info(f"PHASE6: FORCING PAPER for {sig.get(city)} (watch or recent high forecast error)")
    # Then when calling place:
    # place_...( ..., dry_run_override=True )  or simply continue if you want to skip even paper for now

Add at module level:
HIGH_ERROR_CITIES = set()  # populated dynamically from recent large errors in resolutions

### 5. Paper risk limits (in executor.py in the DRY_RUN branches)
# Add:
paper_exposure = ... # track simulated
if paper_exposure > MAX_PAPER_EXPOSURE:
    return {"status": "simulated_skipped", "reason": "paper_exposure_limit"}

## Files
- PHASE6_DIAGNOSTICS_PAPER.md (this)
- bot.py and logger.py have base comments

## Testing
- Run with DRY_RUN=true
- Check logs for PHASE6 lines after next scan
- Simulate a watch city trade (should log force paper)

This completes the diagnostics and paper foundation without live risk.

