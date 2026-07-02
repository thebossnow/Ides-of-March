# Validation Plan: No Live Trades Until the Logic Is Proven

**Status:** LIVE TRADING LOCKED (2026-07-02)
**Unlock mechanism:** `validation_gate.py` certificate — nothing else.

## Why

The 2026-06-27 audit found that the bot's entire live history is 10 trades:
9 total losses and 1 lottery-style win (Paris, 52:1). Every trade executed
with the model probability pinned at the 0.65 cap against asks of
$0.001–0.034, i.e. the bot systematically bought exactly the contracts
where its data feed diverged most from market consensus. Net P&L (+$41.73)
is one lucky ticket, not evidence of edge.

## The lockout (enforced in code, not by discipline)

1. `executor.py`: `DRY_RUN` now **defaults to true**. Setting `DRY_RUN=false`
   is necessary but NOT sufficient for live trading.
2. Even with `DRY_RUN=false`, executor consults
   `validation_gate.live_trading_authorized()` at import. Without a fresh,
   passing certificate (`validation_certificate.json`), DRY_RUN is forced
   back on and a warning is logged.
3. Certificates expire after **7 days**. The edge must keep re-proving
   itself; a stale certificate re-locks live trading automatically.
4. Any failure mode — missing file, parse error, tampering, failed
   evaluation — locks trading (fail-safe).

## The make-or-break test

### Stage A — Deterministic (runs now): `pytest tests/`

`tests/test_make_or_break.py` is the break-it bar for the code logic:

- **Replay:** all 10 historically executed trades must be REJECTED by the
  fixed pipeline (divergence gate, and independently by the strategy gates
  once the WU bypass was removed). Includes the Paris winner — deliberately.
- **Gate coverage:** watch-only execution guard, WU date alignment
  (`validTimeLocal`), cross-source agreement, same-day stale-forecast
  detection, resolution proximity, daily spend cap across cycles, Kelly
  bounds.
- **Statistics:** the validation gate must fail the real live record, fail
  a no-skill synthetic record, fail a "profitable but miscalibrated" record,
  and pass only a genuinely skilled record.

If Stage A fails, the code is broken. Do not proceed to Stage B.

### Stage B — Paper campaign (make or break for the *edge*)

Run the bot in DRY_RUN for as long as it takes to accumulate **100 resolved
paper predictions** (at current filter tightness expect 4–8+ weeks; that is
itself information — a strategy that cannot generate 100 samples cannot be
validated or pay for itself). Then:

```bash
python3 validation_gate.py --evaluate positions.db
```

All four criteria must pass:

| # | Criterion | Threshold | Rationale |
|---|-----------|-----------|-----------|
| 1 | Sample size | n ≥ 100 resolved | n=10 cannot distinguish luck from skill |
| 2 | Calibration | model Brier < market Brier | if we can't out-forecast the book, every "edge" is adverse selection |
| 3 | Profitability | bootstrap 95% lower bound of mean P&L > 0 | one lottery win must not carry the record |
| 4 | Drawdown | max paper drawdown ≤ 20% of bankroll | survivable variance |

- **PASS** → certificate issued (7-day validity). Live trading may be
  enabled with `DRY_RUN=false`, at minimum size, and the evaluation must be
  re-run weekly to renew the certificate.
- **FAIL** → the bot has no demonstrated edge. That is the "break" outcome:
  stop tuning filters and either fix the forecast inputs (the root cause)
  or retire the strategy. Do not go live.

### Verification commands

```bash
pytest tests/ -v                          # Stage A
python3 validation_gate.py --check        # current lock status
python3 validation_gate.py --evaluate positions.db   # Stage B scoring
```
