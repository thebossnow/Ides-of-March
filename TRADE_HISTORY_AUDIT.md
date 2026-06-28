# Phase 1 Historical Audit + Phase 2 Fixes - Weather Bot Trades

**Audit date:** 2026-06-27
**Location:** /root/Ides-of-March on Vultr VPS

## Executive Summary
- **Volume:** Extremely low. 14 entries in trade_log.csv, but only **10 real executed TRADE** in scan_log and ~10 in positions.db (9 lost, 1 won).
- **P&L:** 9 x - = -9; 1 x +50.73 (Paris); net recorded ~+41.73.
- **Pattern of "garbage" trades:** Cheap FOK buys (0.001-0.03) on "or below high threshold" when WU forecast low. Model reported 65% prob (capped). Actual highs hotter than forecast on losers. Watch-only cities leaked (Chicago, Denver, Helsinki).
- **Why few trades:** Overly strict Phase 2 filters (best bucket >=30% prob, absolute 30%, edge floors, caps, watch list). Millions of scans, handful of trades.
- **New since last:** Helsinki 20C-below and Munich 32C-below on Jun 27 (both lost).
- **sniperweatherbot:** No meaningful trades (0 positions, logs = forecasts only).
- **Backup:** Only Jun13 trades, no positions.

## Full trade_log.csv (14 entries)
2026-06-13T01:55:39.717937,highest-temperature-in-chicago-on-june-14-2026-78forhigher,Chicago,2026-06-14,30,86.0,F,78.0,none,0.6002,0.0556,0.4975,1.0,False,0xb742a8220b7cdb2c4b8f2e5ab9dd6ccb32420f7f5ea61cb8a320585cb008d765,MATCHED,Will the highest temperature in Chicago be 78°F or higher on June 14?
2026-06-13T02:15:41.902846,highest-temperature-in-denver-on-june-13-2026-86forhigher,Denver,2026-06-13,33,91.4,F,86.0,none,0.6283,0.0487,0.5694,1.0,False,0x3b3d179f972076e2e4be5f117d2d06bfc6afc4c9eb167249a53559482f485050,MATCHED,Will the highest temperature in Denver be 86°F or higher on June 13?
2026-06-13T19:41:24.196262,highest-temperature-in-chengdu-on-june-15-2026-31corbelow,Chengdu,2026-06-15,28,28,C,none,31.0,0.6406,0.54,0.1006,2.71,False,0xafef010b7735cefccaa3b214e27fe11fc2b4e463d1c2b04a70ec884919fea803,MATCHED,Will the highest temperature in Chengdu be 31°C or below on June 15?
2026-06-13T22:27:43.057976,highest-temperature-in-milan-on-june-13-2026-28corbelow,Milan,2026-06-13,25,25,C,none,28.0,0.65,0.001,0.64,1.0,False,0x8bafb0c9849f3c5dfe458957e0c61cde2816bf0b2bcefec5a22fbfe36939cb7d,MATCHED,Will the highest temperature in Milan be 28°C or below on June 13?
2026-06-26T14:07:58.932692,lowest-temperature-in-paris-on-june-26-2026-24corbelow,Paris,2026-06-26,18,18,C,none,24.0,0.65,0.0193,0.6299,1.0,False,0x65063c2dfcfa2969224eba94843edddcf2356cc0679b9f3c2736a4162a2c4c01,MATCHED,Will the lowest temperature in Paris be 24°C or below on June 26?
2026-06-26T22:39:05.466506,highest-temperature-in-munich-on-june-26-2026-31corbelow,Munich,2026-06-26,27,27,C,none,31.0,0.65,0.001,0.64,1.0,False,0x3ffa01b1008b9e6b2fb9bd9f282eed248efeccd05b5d8e7ef7982993df75497f,MATCHED,Will the highest temperature in Munich be 31°C or below on June 26?
2026-06-26T22:39:07.963481,highest-temperature-in-amsterdam-on-june-26-2026-33corbelow,Amsterdam,2026-06-26,28,28,C,none,33.0,0.65,0.001,0.64,1.0,False,0xb76ce919456e8a2bb459277af54c1a35cef8fe43ad7147b26d70ed35c35fbb3c,MATCHED,Will the highest temperature in Amsterdam be 33°C or below on June 26?
2026-06-26T23:40:53.029662,highest-temperature-in-london-on-june-26-2026-32corbelow,London,2026-06-26,26,26,C,none,32.0,0.65,0.001,0.64,1.0,False,0xb9ef00ba51bba8bef233b436eb7cbc67a8915896d68d2dc4917ee169c97f7087,MATCHED,Will the highest temperature in London be 32°C or below on June 26?
2026-06-27T00:21:21.889756,highest-temperature-in-london-on-june-28-2026-32corhigher,London,2026-06-28,36,36,C,32.0,none,0.65,0.0075,0.638,1.0,False,0x7a74299e236dc514c4f020539875870a81f906e83511930788b5af96f6ff10e0,MATCHED,Will the highest temperature in London be 32°C or higher on June 28?
2026-06-27T05:27:28.498925,highest-temperature-in-chicago-on-june-28-2026-75forbelow,Chicago,2026-06-28,18,64.4,F,none,75.0,0.65,0.0082,0.64,1.0,False,0xbe782032fcda285a3665c649ff7884e52fa6501f786532cd24c25b56b91c974d,MATCHED,Will the highest temperature in Chicago be 75°F or below on June 28?
2026-06-27T05:27:30.624039,highest-temperature-in-austin-on-june-27-2026-91forbelow,Austin,2026-06-27,29,84.2,F,none,91.0,0.65,0.0337,0.5947,1.0,False,0xf943e3852fea1052652ef593e0ba6f1d3561690fd60b5270decab6aa4bad042d,MATCHED,Will the highest temperature in Austin be 91°F or below on June 27?
2026-06-27T06:11:05.061061,highest-temperature-in-denver-on-june-27-2026-83forbelow,Denver,2026-06-27,14,57.2,F,none,83.0,0.65,0.001,0.64,1.0,False,0x4eaee91dd07233a6e655ed79fe6c4f1492b44721b8c4cff0f37c8daa27aafdc4,MATCHED,Will the highest temperature in Denver be 83°F or below on June 27?
2026-06-27T21:17:36.995942,highest-temperature-in-helsinki-on-june-27-2026-20corbelow,Helsinki,2026-06-27,17,17,C,none,20.0,0.65,0.001,0.64,1.0,False,0x6d6b7f8365348b7ffb23c1cc8c990f1def73684b9a8d22ac67204e626761dfcd,MATCHED,Will the highest temperature in Helsinki be 20°C or below on June 27?
2026-06-27T22:40:12.739298,highest-temperature-in-munich-on-june-27-2026-32corbelow,Munich,2026-06-27,26,26,C,none,32.0,0.65,0.001,0.64,1.0,False,0x0c58f96b8f460b89f707b70f363ee19474820698933c64ea580a210d3edc44d7,MATCHED,Will the highest temperature in Munich be 32°C or below on June 27?

## positions.db Current State (10 rows)
