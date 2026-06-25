# Bot.py Handoff — CORRECTED (verified against committed code)

**Corrects:** `bot-py-handoff.md` (Hermes/Raijin, 2026-06-20)
**Verified against:** branch `claude/bot-py-handoff-review-4r3qfa`, `bot.py`
**Date:** 2026-06-20

> **Why this exists.** The original handoff describes the active bot as a
> 1,107-line, GFS-only Ides-of-March with no exposure cap, no Wunderground,
> no METAR, and a multi-bucket "spray" problem. **None of that matches the
> code committed to this repository.** The committed `bot.py` is the
> V2-lineage bot (Wunderground-primary, METAR, risk manager, highest-prob
> bucket selection). The original appears to review an earlier snapshot.
> This document records what the committed code actually does.

---

## Active bot: Ides-of-March — what is actually committed

| Attribute | Original handoff claimed | **Verified in repo** |
|---|---|---|
| Lines | 1,107 | **1,763** (1,755 before the SIGHUP fix below) |
| Size | 44,859 bytes | **~84,600 bytes** |
| Scan interval | 30 min | **15 min** (`bot.py:119` `SCAN_INTERVAL=15`) |
| Forecast source | GFS only | **Wunderground (primary) + GFS/Open-Meteo + METAR** |
| Exposure cap | none | **50%** (`UNRESOLVED_EXPOSURE_CAP=0.50`, enforced `bot.py:~1196`) |
| Bucket selection | multi-bucket spray | **highest-prob bucket only per (city,date)** (`bot.py:~1029`) |
| WU prob cap | (n/a — claimed no WU) | **`WU_MAX_PROB=0.65`** Phase 3 (`bot.py:610`) |

The committed file is, in substance, the bot the original handoff labels
"Weatherbot V2." Its 15-minute scan, line count, byte size, and WU/METAR
pipeline all line up with that description — not with the "Ides-of-March"
column.

### Imported local modules

```
bot.py imports:
  weather_v2          get_forecast, get_forecast_low, get_ensemble_forecast,
                      get_city_gfs_ensemble, get_gfs_spread, CITIES, …
  markets             markets, parse, price, book_asks
  executor            place_buy_order, place_gtc_order, place_ladder_bids, DRY_RUN, …
  strategy            prob functions, find_edge, thresholds, UNRESOLVED_EXPOSURE_CAP
  risk_manager_v2     get_current_bankroll, get_safe_position_size, check_drawdown
  positions           record_entry, get_open_positions, get_total_open_exposure, init_db
  position_monitor    monitor_positions, resolve_past_positions, needs_fast_monitoring
  redeemer            redeem_all_winners (gasless)
  aviation_weather    get_current_metar_temps, AVIATION_ICAO
  metar_bias          compute_biases_from_metar
  wunderground_client fetch_forecasts, wunderground_match, AIRPORT_COORDS
  wu_empirical        log_wu_scan
  logger              log_trade, log_scan
  notifier            TelegramNotifier
```

---

## 🚨 Blocking issue the original handoff missed: repo is not runnable

`bot.py` (and the `weather.py` compatibility shim) import **seven modules
that are not committed to this repository and do not exist in its git
history**:

```
weather_v2.py   wunderground_client.py   wu_empirical.py
aviation_weather.py   metar_bias.py   metar_block.py   risk_manager_v2.py
```

`import weather_v2` (`bot.py:39`) raises `ImportError` at startup, so the
code as committed **cannot run or be tested here.** These files are not
`.gitignore`'d — they are simply absent. They presumably live in the VPS's
gitignored `VPS files/` directory.

> **NOTE / cannot be resolved from this environment.** This session runs in
> an isolated cloud container with no route to the VPS and no copy of those
> modules. The missing files must be sourced from the VPS (`/root/...`) and
> committed there. I have flagged the gap but cannot supply the files.

For a true handoff, committing these seven modules is the **highest-priority
item** — without them the repo is a non-runnable partial snapshot.

---

## Critical-gaps scorecard (original handoff vs. reality)

| # | Original "critical gap" | Reality in committed code | Verdict |
|---|---|---|---|
| 1 | SIGHUP not ignored | Was true | ✅ valid — **fixed on this branch** |
| 2 | No unresolved exposure cap | `UNRESOLVED_EXPOSURE_CAP=0.50` enforced | ❌ already done |
| 3 | Single forecast source (GFS) | WU-primary + GFS + METAR | ❌ wrong |
| 4 | No METAR safety gate | METAR integrated (`aviation_weather`, `metar_bias`)* | ⚠️ mostly wrong* |
| 5 | Multi-bucket spray | Keeps only highest-prob bucket per (city,date) | ❌ already done |

\* Caveat: `metar_bias` (bias correction from live obs) is imported, but the
dedicated **pre-trade `metar_block.py` hard gate** is among the seven missing
modules. So a forecast-vs-live-obs *block* may not be wired in even though
METAR data is consumed for bias. Confirm once the modules are committed.

**Net:** only gap #1 was real, and it is now fixed.

---

## Fix applied on this branch

**SIGHUP guard** — added to `main()` in `bot.py`:

```python
if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    logger.info("SIGHUP ignored (logrotate/detach-safe)")
```

(`import signal` added to the top-of-file imports.) Guarded with `hasattr`
so it is a no-op on platforms without `SIGHUP` (e.g. Windows). The bot now
survives logrotate, systemd restart sequences, terminal/screen detach, and
stray `kill -HUP`. Verified with `python3 -m py_compile bot.py`.

---

## Remaining real work (in priority order)

1. **Commit the 7 missing modules** from the VPS so the repo is runnable and
   reproducible. (Must be done on the VPS — not possible from this session.)
2. **Confirm the METAR pre-trade block** is actually wired once
   `metar_block.py` lands; if not, port it as the original doc suggested.
3. Re-audit the remaining strengths/weaknesses in the original doc — several
   "V2 only" features (exposure cap, WU matching, highest-prob bucket,
   GTC/FOK fallback) are already present here, so the architecture-evolution
   narrative ("regression from V2 to Ides") is inverted and should be
   rewritten against the committed code.

---

*Corrected handoff — verified against committed source, 2026-06-20.*
