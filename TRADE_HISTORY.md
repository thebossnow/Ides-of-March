# Weather Trades History Audit (Phase 1)

**Generated:** 2026-06-27T23:15:05Z
**Bot:** /root/Ides-of-March/bot.py (weather threshold markets on Polymarket)

## Summary
- trade_log.csv: 14 lines (4 old Jun13 + 10 recent)
- positions.db: 9 rows (8 lost, 1 won). Net recorded PnL: +42.73
- scan_log.csv: ~2M lines, 9-10 TRADE entries only
- All executed trades used FOK sweep at low prices.
- Newest trades (Jun27): Helsinki and Munich added after previous audit.

## All Recorded Trades (from trade_log.csv + positions)

- 2026-06-13T01:55:39.717937 | Chicago 2026-06-14 | highest-temperature-in-chicago-on-june-14-2026-78forhigher
- 2026-06-13T02:15:41.902846 | Denver 2026-06-13 | highest-temperature-in-denver-on-june-13-2026-86forhigher
- 2026-06-13T19:41:24.196262 | Chengdu 2026-06-15 | highest-temperature-in-chengdu-on-june-15-2026-31corbelow
- 2026-06-13T22:27:43.057976 | Milan 2026-06-13 | highest-temperature-in-milan-on-june-13-2026-28corbelow
- 2026-06-26T14:07:58.932692 | Paris 2026-06-26 | lowest-temperature-in-paris-on-june-26-2026-24corbelow
- 2026-06-26T22:39:05.466506 | Munich 2026-06-26 | highest-temperature-in-munich-on-june-26-2026-31corbelow
- 2026-06-26T22:39:07.963481 | Amsterdam 2026-06-26 | highest-temperature-in-amsterdam-on-june-26-2026-33corbelow
- 2026-06-26T23:40:53.029662 | London 2026-06-26 | highest-temperature-in-london-on-june-26-2026-32corbelow
- 2026-06-27T00:21:21.889756 | London 2026-06-28 | highest-temperature-in-london-on-june-28-2026-32corhigher
- 2026-06-27T05:27:28.498925 | Chicago 2026-06-28 | highest-temperature-in-chicago-on-june-28-2026-75forbelow
- 2026-06-27T05:27:30.624039 | Austin 2026-06-27 | highest-temperature-in-austin-on-june-27-2026-91forbelow
- 2026-06-27T06:11:05.061061 | Denver 2026-06-27 | highest-temperature-in-denver-on-june-27-2026-83forbelow
- 2026-06-27T21:17:36.995942 | Helsinki 2026-06-27 | highest-temperature-in-helsinki-on-june-27-2026-20corbelow
- 2026-06-27T22:40:12.739298 | Munich 2026-06-27 | highest-temperature-in-munich-on-june-27-2026-32corbelow

## positions.db Details

