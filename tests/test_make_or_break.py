"""
Make-or-break test suite for the Ides-of-March weather bot.

The bar this suite sets:

  PART 1 — REPLAY: every one of the 10 trades the bot actually executed in
  June 2026 (9 losers + 1 lottery win) must now be REJECTED by the fixed
  pipeline. If any of them would still trade, the bot is broken. The Paris
  winner is rejected on purpose: a 52:1 longshot that happened to hit is
  luck, not logic, and luck must not validate the system.

  PART 2 — GATES: each defect fix is exercised directly (watch-only leak,
  WU gate bypass, forecast date misalignment, cross-source sanity,
  stale-forecast detection, divergence, daily spend cap, resolution
  proximity).

  PART 3 — STATISTICS: the validation gate must FAIL the bot's real live
  record, FAIL a no-skill synthetic record, and PASS only a genuinely
  skilled record. Live trading stays locked until a paper campaign passes.

Run:  pytest tests/ -v
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from trade_gates import (
    MAX_MODEL_MARKET_DIVERGENCE,
    DailySpendTracker,
    check_cross_source_agreement,
    check_hours_to_resolution,
    check_model_market_divergence,
    check_same_day_observed,
    hours_to_resolution,
    run_sanity_gates,
)
import validation_gate as vg
from wunderground_client import _build_forecast_days


# ─────────────────────────────────────────────────────────────────────────
# The complete live trading record as of the 2026-06-27 audit
# (positions.db: 9 resolved_lost, 1 resolved_won). Every trade executed
# with the model probability pinned at the 0.65 WU cap against longshot
# asks. forecast_c is the WU input the bot believed; the Denver/Chicago
# values are the broken inputs the audit flagged.
# ─────────────────────────────────────────────────────────────────────────
AUDITED_TRADES = [
    # (city,        model_prob, fill_price, forecast_c, won,  pnl,    watch_only)
    ("Paris",       0.65,       0.0193,     18.0,       True, +50.73, False),
    ("Munich",      0.65,       0.0010,     27.0,       False, -1.0,  False),
    ("Amsterdam",   0.65,       0.0010,     28.0,       False, -1.0,  False),
    ("London",      0.65,       0.0010,     26.0,       False, -1.0,  False),
    ("London",      0.65,       0.0075,     36.0,       False, -1.0,  False),
    ("Chicago",     0.65,       0.0082,     18.0,       False, -1.0,  True),
    ("Austin",      0.65,       0.0337,     29.0,       False, -1.0,  False),
    ("Denver",      0.65,       0.0010,     14.0,       False, -1.0,  True),
    ("Helsinki",    0.65,       0.0010,     17.0,       False, -1.0,  True),
    ("Munich",      0.65,       0.0010,     26.0,       False, -1.0,  False),
]


# ═════════════════════════════════════════════════════════════════════════
# PART 1 — REPLAY: the fixed pipeline must reject all 10 executed trades
# ═════════════════════════════════════════════════════════════════════════

class TestHistoricalTradeReplay:
    def test_divergence_gate_rejects_every_audited_trade(self):
        """All 10 trades had model-vs-market gaps >= 0.60. The divergence
        gate treats >0.35 as a data error, so every one must be blocked —
        including the Paris winner."""
        for city, prob, price, _fc, _won, _pnl, _w in AUDITED_TRADES:
            ok, reason = check_model_market_divergence(prob, price)
            assert not ok, f"{city} @ {price} should be rejected, got pass"
            assert "divergence" in reason

    def test_strategy_gates_reject_every_audited_trade_without_wu_bypass(self):
        """The old code exempted WU signals from classify_skip_reason. With
        the bypass removed, the existing MAX_EDGE cap alone rejects all 10
        trades (edge >= 0.62 > 0.50 reads as impossible certainty)."""
        from strategy import classify_skip_reason
        today = date.today().isoformat()
        for city, prob, price, _fc, _won, _pnl, _w in AUDITED_TRADES:
            edge = prob - price
            reason = classify_skip_reason(edge, forecast_prob=prob, market_date=today)
            assert reason is not None, (
                f"{city}: edge={edge:.3f} must be skipped once WU bypass is gone"
            )
            assert reason == "edge_above_max"

    def test_composite_sanity_gate_rejects_denver_style_trade(self):
        """Denver 2026-06-27: WU said the daily high would be 14°C (57.2°F)
        in late June while other models said ~33°C. The composite gate must
        reject on cross-source disagreement before price is even fetched."""
        ok, reason = run_sanity_gates(
            model_prob=0.65,
            market_price=0.001,
            primary_forecast_c=14.0,
            secondary_forecast_c=33.0,
            observed_c=None,
        )
        assert not ok
        assert reason.startswith("SANITY_CROSS_SOURCE")

    def test_lottery_win_cannot_validate_the_system(self):
        """The single winner (Paris, 52:1) fails the same gates as the
        losers. This is intentional: the +$41.73 net P&L is one lottery
        ticket, not evidence of edge."""
        ok, _ = check_model_market_divergence(0.65, 0.0193)
        assert not ok


# ═════════════════════════════════════════════════════════════════════════
# PART 2 — GATES: each defect fix exercised directly
# ═════════════════════════════════════════════════════════════════════════

class TestWatchOnlyLeak:
    """Audit: Chicago, Denver and Helsinki traded from the watch list because
    FOK-path signal dicts were built without the is_watch_only key and the
    filter used .get() (None == not watch-only)."""

    def test_leaked_cities_are_on_watch_list(self):
        from strategy import is_city_watch_only
        for city in ("Chicago", "Denver", "Helsinki"):
            assert is_city_watch_only(city), f"{city} must be watch-only"

    def test_old_filter_predicate_leaked_signals_missing_the_key(self):
        # Reconstruction of the defect: signal built on the FOK path (no key)
        fok_signal = {"city": "Denver", "slug": "denver-83forbelow"}
        old_predicate_traded = not fok_signal.get("is_watch_only")
        assert old_predicate_traded, "defect reconstruction should show the leak"

    def test_new_execution_guard_blocks_even_without_the_key(self):
        from strategy import is_city_watch_only, is_city_tradable
        fok_signal = {"city": "Denver", "slug": "denver-83forbelow"}
        blocked = (fok_signal.get("is_watch_only")
                   or is_city_watch_only(fok_signal["city"])
                   or not is_city_tradable(fok_signal["city"]))
        assert blocked, "execution guard must re-derive watch status from city"


class TestForecastDateAlignment:
    """Audit smoking gun: WU forecast days were labeled with the SERVER's
    UTC date, not the station's local date, shifting forecasts by a day."""

    def _payload(self, dates, highs, lows):
        return {
            "validTimeLocal": [f"{d}T07:00:00-0600" for d in dates],
            "calendarDayTemperatureMax": highs,
            "calendarDayTemperatureMin": lows,
            "narrative": ["" for _ in dates],
        }

    def test_days_labeled_from_station_local_dates(self):
        # Station-local dates deliberately one day behind a UTC 'today'
        dates = ["2026-06-26", "2026-06-27", "2026-06-28"]
        days, err = _build_forecast_days(self._payload(dates, [33, 34, 32], [20, 21, 19]))
        assert err is None
        assert [d["date"] for d in days] == dates
        assert days[0]["high_c"] == 33

    def test_missing_valid_time_local_fails_safe(self):
        payload = self._payload(["2026-06-26"], [33], [20])
        del payload["validTimeLocal"]
        days, err = _build_forecast_days(payload)
        assert days == [] and err is not None and "validTimeLocal" in err

    def test_null_temps_rejected(self):
        days, err = _build_forecast_days(
            self._payload(["2026-06-26", "2026-06-27"], [None, 34], [20, 21]))
        assert days == [] and "None temperature" in err


class TestCrossSourceGate:
    def test_disagreement_blocks(self):
        ok, reason = check_cross_source_agreement(14.0, 33.0)
        assert not ok and "disagreement" in reason

    def test_missing_second_source_fails_safe(self):
        ok, reason = check_cross_source_agreement(27.0, None)
        assert not ok and "fail-safe" in reason

    def test_agreement_passes(self):
        ok, reason = check_cross_source_agreement(27.0, 28.5)
        assert ok and reason is None


class TestSameDayObservedGate:
    def test_observation_above_forecast_max_blocks(self):
        # Forecast high 14°C but the station already reads 25°C
        ok, reason = check_same_day_observed(14.0, 25.0)
        assert not ok and "stale" in reason

    def test_observation_below_forecast_passes(self):
        ok, _ = check_same_day_observed(30.0, 24.0)
        assert ok

    def test_no_observation_defers_to_other_gates(self):
        ok, _ = check_same_day_observed(30.0, None)
        assert ok


class TestDivergenceGate:
    def test_threshold_boundary(self):
        ok, _ = check_model_market_divergence(0.60, 0.60 - MAX_MODEL_MARKET_DIVERGENCE)
        assert ok  # exactly at the limit is allowed
        ok, _ = check_model_market_divergence(0.60, 0.60 - MAX_MODEL_MARKET_DIVERGENCE - 0.01)
        assert not ok

    def test_missing_price_fails_safe(self):
        ok, _ = check_model_market_divergence(0.60, None)
        assert not ok

    def test_reasonable_edge_passes(self):
        # A believable signal: model 45%, market 25% — 20pt edge, allowed
        ok, _ = check_model_market_divergence(0.45, 0.25)
        assert ok


class TestResolutionProximityGate:
    TZ = "Europe/London"

    def test_market_closing_within_two_hours_blocked(self):
        now = datetime(2026, 7, 2, 23, 30, tzinfo=ZoneInfo(self.TZ))
        ok, reason = check_hours_to_resolution("2026-07-02", self.TZ, min_hours=2.0, now=now)
        assert not ok and "resolves in" in reason

    def test_next_day_market_allowed(self):
        now = datetime(2026, 7, 2, 23, 30, tzinfo=ZoneInfo(self.TZ))
        ok, _ = check_hours_to_resolution("2026-07-03", self.TZ, min_hours=2.0, now=now)
        assert ok

    def test_unparseable_date_fails_safe(self):
        ok, _ = check_hours_to_resolution("garbage", self.TZ)
        assert not ok

    def test_hours_computation(self):
        now = datetime(2026, 7, 2, 22, 0, tzinfo=ZoneInfo(self.TZ))
        assert hours_to_resolution("2026-07-02", self.TZ, now=now) == pytest.approx(2.0)


class TestDailySpendCap:
    def test_cap_accumulates_across_cycles(self):
        """The defect: bot.py reset its spend counter every scan cycle, so
        the 'daily' cap never bound across cycles. The tracker must."""
        t = DailySpendTracker(fraction=0.20)
        bankroll, day = 100.0, "2026-07-02"
        assert t.allows(15.0, bankroll, today=day)
        t.record(15.0, today=day)          # cycle 1
        assert not t.allows(6.0, bankroll, today=day)   # cycle 2: 15+6 > 20
        assert t.allows(5.0, bankroll, today=day)       # 15+5 == 20 ok
        t.record(5.0, today=day)
        assert not t.allows(0.01, bankroll, today=day)  # budget exhausted

    def test_cap_rolls_over_at_new_day(self):
        t = DailySpendTracker(fraction=0.20)
        t.record(20.0, today="2026-07-02")
        assert t.allows(20.0, 100.0, today="2026-07-03")
        assert t.spent_today(today="2026-07-03") == 0.0


class TestKellySizing:
    def test_no_edge_means_no_position(self):
        from strategy import kelly_position_size
        assert kelly_position_size(100.0, edge=-0.1, win_prob=0.2, market_price=0.5) == 0.0

    def test_degenerate_inputs_return_zero(self):
        from strategy import kelly_position_size
        assert kelly_position_size(100.0, 0.2, win_prob=0.0, market_price=0.5) == 0.0
        assert kelly_position_size(100.0, 0.2, win_prob=0.5, market_price=0.0) == 0.0
        assert kelly_position_size(0.0, 0.2, win_prob=0.5, market_price=0.3) == 0.0

    def test_position_respects_bounds(self):
        from strategy import kelly_position_size, MIN_POSITION_USDC, MAX_POSITION_USDC
        size = kelly_position_size(1000.0, edge=0.2, win_prob=0.6, market_price=0.4)
        assert MIN_POSITION_USDC <= size <= MAX_POSITION_USDC


# ═════════════════════════════════════════════════════════════════════════
# PART 3 — STATISTICS: the make-or-break validation gate
# ═════════════════════════════════════════════════════════════════════════

def _records(trades):
    return [
        {"model_prob": p, "market_price": mp, "won": won, "pnl": pnl}
        for _city, p, mp, _fc, won, pnl, _w in trades
    ]


class TestMakeOrBreakEvaluation:
    def test_actual_live_record_fails(self):
        """The bot's entire real trading history must NOT pass validation:
        n=10 is statistically meaningless, and the market's implied
        probabilities were better calibrated than the model's (Brier 0.10
        vs 0.39). Net-positive P&L from one lottery hit does not rescue it."""
        stats = vg.evaluate_paper_record(_records(AUDITED_TRADES))
        assert not stats["passed"]
        assert not stats["checks"]["sample_size"]["passed"]
        assert not stats["checks"]["calibration_beats_market"]["passed"]
        checks = stats["checks"]["calibration_beats_market"]
        assert checks["brier_market"] < checks["brier_model"]

    @staticmethod
    def _skilled_record(n_blocks=30):
        """Model prob 0.60 with a true 60% win rate vs market price 0.45:
        genuine skill. 3 wins / 2 losses interleaved keeps drawdown small."""
        win_pnl = (1.0 / 0.45) - 1.0  # $1 stake at $0.45
        recs = []
        for _ in range(n_blocks):
            for _ in range(3):
                recs.append({"model_prob": 0.60, "market_price": 0.45,
                             "won": True, "pnl": win_pnl})
            for _ in range(2):
                recs.append({"model_prob": 0.60, "market_price": 0.45,
                             "won": False, "pnl": -1.0})
        return recs

    def test_skilled_record_passes(self):
        stats = vg.evaluate_paper_record(self._skilled_record())
        assert stats["n_resolved"] == 150
        for name, check in stats["checks"].items():
            assert check["passed"], f"criterion {name} failed: {check}"
        assert stats["passed"]

    def test_no_skill_record_fails(self):
        """Model claims 0.60 but the true rate equals the market's 0.50:
        zero edge. Both calibration and profitability must fail."""
        recs = []
        for i in range(150):
            won = i % 2 == 0
            recs.append({"model_prob": 0.60, "market_price": 0.50,
                         "won": won, "pnl": 1.0 if won else -1.0})
        stats = vg.evaluate_paper_record(recs)
        assert not stats["passed"]
        assert not stats["checks"]["calibration_beats_market"]["passed"]
        assert not stats["checks"]["profitability"]["passed"]

    def test_lucky_streak_without_calibration_fails(self):
        """Even a profitable run fails if the market out-forecasts the model
        — profit without calibration is luck riding variance."""
        recs = []
        for i in range(150):
            won = i % 3 != 0  # 66% win rate
            # Model wildly overconfident at 0.99; market honest at 0.66
            recs.append({"model_prob": 0.99, "market_price": 0.66,
                         "won": won, "pnl": 0.5 if won else -1.0})
        stats = vg.evaluate_paper_record(recs)
        assert not stats["passed"]
        assert not stats["checks"]["calibration_beats_market"]["passed"]


class TestCertificateLifecycle:
    def test_no_certificate_locks_live_trading(self, tmp_path):
        ok, reason = vg.live_trading_authorized(path=str(tmp_path / "missing.json"))
        assert not ok and "no validation certificate" in reason

    def test_passing_evaluation_issues_valid_certificate(self, tmp_path):
        cert_path = str(tmp_path / "cert.json")
        stats = vg.evaluate_paper_record(TestMakeOrBreakEvaluation._skilled_record())
        vg.issue_certificate(stats, path=cert_path)
        ok, reason = vg.live_trading_authorized(path=cert_path)
        assert ok and "valid until" in reason

    def test_failing_evaluation_cannot_issue_certificate(self, tmp_path):
        stats = vg.evaluate_paper_record(_records(AUDITED_TRADES))
        with pytest.raises(ValueError):
            vg.issue_certificate(stats, path=str(tmp_path / "cert.json"))

    def test_expired_certificate_relocks(self, tmp_path):
        cert_path = str(tmp_path / "cert.json")
        stats = vg.evaluate_paper_record(TestMakeOrBreakEvaluation._skilled_record())
        vg.issue_certificate(stats, path=cert_path)
        future = datetime.now(timezone.utc) + timedelta(days=vg.CERT_TTL_DAYS + 1)
        ok, reason = vg.live_trading_authorized(path=cert_path, now=future)
        assert not ok and "expired" in reason

    def test_tampered_certificate_relocks(self, tmp_path):
        import json
        cert_path = tmp_path / "cert.json"
        cert_path.write_text(json.dumps({
            "schema": vg.CERT_SCHEMA, "passed": False,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        }))
        ok, _ = vg.live_trading_authorized(path=str(cert_path))
        assert not ok

    def test_corrupt_certificate_relocks(self, tmp_path):
        cert_path = tmp_path / "cert.json"
        cert_path.write_text("{not json")
        ok, reason = vg.live_trading_authorized(path=str(cert_path))
        assert not ok and "unreadable" in reason


class TestPositionsDbLoader:
    @staticmethod
    def _make_db(tmp_path):
        import sqlite3
        db = str(tmp_path / "positions.db")
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE positions (
            forecast_prob REAL, market_prob REAL, pnl_usdc REAL,
            status TEXT, entry_time TEXT)""")
        rows = [
            # Pre-fix LIVE trades (June) — the broken-era record
            (0.65, 0.0193, 50.73, "resolved_won",  "2026-06-26T14:07:58"),
            (0.65, 0.0010, -1.0,  "resolved_lost", "2026-06-27T22:40:12"),
            # Stage B paper trades (post-deployment)
            (0.45, 0.30,   0.50,  "resolved_won",  "2026-07-03T10:00:00"),
            (0.40, 0.28,  -1.0,   "resolved_lost", "2026-07-04T10:00:00"),
            # Excluded regardless of window
            (0.55, 0.30,  None,   "open",          "2026-07-05T10:00:00"),
            (None, 0.30,  -1.0,   "resolved_lost", "2026-07-05T11:00:00"),
        ]
        conn.executemany("INSERT INTO positions VALUES (?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        return db

    def test_loads_only_resolved_rows(self, tmp_path):
        records = vg.records_from_positions_db(self._make_db(tmp_path))
        assert len(records) == 4
        assert records[0]["won"] is True and records[1]["won"] is False

    def test_since_cutoff_excludes_prefix_live_trades(self, tmp_path):
        """Stage B integrity: positions.db has no paper/live flag, so the
        pre-fix June live trades (incl. the +50.73 Paris outlier) must be
        excludable by entry_time. Without this, the lottery win produced by
        the OLD broken code could help 'validate' the NEW code."""
        db = self._make_db(tmp_path)
        records = vg.records_from_positions_db(db, since="2026-07-02")
        assert len(records) == 2
        assert all(r["pnl"] != 50.73 for r in records), "Paris live win must be excluded"
