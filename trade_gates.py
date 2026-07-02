"""
trade_gates.py - Hard pre-trade sanity gates (Phase 10).

Root-cause defense layer added after the 2026-06 audit: 9 of 10 executed
trades were longshot buys driven by broken forecast inputs (e.g. Denver
forecast high 57.2°F in late June vs an 83°F threshold) combined with a
capped model probability of 0.65 against market asks of $0.001-0.034.

Design principles:
  1. A large model-vs-market disagreement is a BUG SIGNAL, not an edge.
     Market makers use the same GFS/ECMWF data we do; if we disagree with
     the book by 35+ points, our data feed is almost certainly wrong.
  2. Never trade a forecast that a second independent source disputes.
     Garbage-in cannot be fixed downstream.
  3. Never trade a same-day "highest" forecast that the thermometer has
     already proven wrong (observed temp above forecast max).
  4. Fail safe: missing data blocks the trade, it does not wave it through.

Every gate is a pure function returning (ok: bool, reason: str | None) so
the whole layer is unit-testable without network, CLOB, or .env access.
"""

from __future__ import annotations

import logging
from datetime import datetime, date, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────
# Above this |model_prob - market_price| gap the trade is rejected as a
# suspected data error. All 10 trades in the June audit had gaps >= 0.60.
MAX_MODEL_MARKET_DIVERGENCE = 0.35

# Max allowed disagreement between the primary (WU) forecast and an
# independent second source (Open-Meteo) for the same city/date, in °C.
CROSS_SOURCE_TOLERANCE_C = 4.0

# Same-day "highest" markets: if the current observation already exceeds
# the forecast max by more than this, the forecast is stale/broken.
SAME_DAY_OBSERVED_TOLERANCE_C = 1.0

# Fraction of bankroll that may be deployed per UTC day (README contract:
# 20%. bot.py previously used 50% per *cycle*, i.e. up to 48x per day).
DAILY_SPEND_FRACTION = 0.20


def check_model_market_divergence(model_prob: float,
                                  market_price: float | None) -> tuple[bool, str | None]:
    """Reject trades where the model and the market disagree so much that
    a data error is more likely than a genuine edge."""
    if market_price is None:
        return False, "no market price available"
    divergence = abs(model_prob - market_price)
    if divergence > MAX_MODEL_MARKET_DIVERGENCE:
        return False, (
            f"model/market divergence {divergence:.2f} > "
            f"{MAX_MODEL_MARKET_DIVERGENCE:.2f} — treated as data error, not edge"
        )
    return True, None


def check_cross_source_agreement(primary_c: float | None,
                                 secondary_c: float | None,
                                 tolerance_c: float = CROSS_SOURCE_TOLERANCE_C
                                 ) -> tuple[bool, str | None]:
    """Require an independent second forecast source to agree with the
    primary within tolerance. Missing data fails safe (no trade)."""
    if primary_c is None:
        return False, "no primary forecast"
    if secondary_c is None:
        return False, "no independent second-source forecast (fail-safe: no trade)"
    gap = abs(primary_c - secondary_c)
    if gap > tolerance_c:
        return False, (
            f"cross-source disagreement {gap:.1f}°C > {tolerance_c:.1f}°C "
            f"(primary={primary_c:.1f}°C, secondary={secondary_c:.1f}°C)"
        )
    return True, None


def check_same_day_observed(forecast_max_c: float,
                            observed_c: float | None,
                            tolerance_c: float = SAME_DAY_OBSERVED_TOLERANCE_C
                            ) -> tuple[bool, str | None]:
    """Same-day highest-temp markets: the daily max cannot be below what
    the station has already observed. If the current METAR reading exceeds
    the forecast max, the forecast is stale — reject."""
    if observed_c is None:
        return True, None  # No observation available; other gates still apply
    if observed_c > forecast_max_c + tolerance_c:
        return False, (
            f"observed {observed_c:.1f}°C already exceeds forecast max "
            f"{forecast_max_c:.1f}°C (+{tolerance_c:.1f}°C tolerance) — stale forecast"
        )
    return True, None


def hours_to_resolution(market_date_str: str,
                        tz_name: str | None,
                        now: datetime | None = None) -> float | None:
    """Hours until the end of the market's local calendar day (the earliest
    the market can resolve). Returns None if the date cannot be parsed."""
    try:
        market_date = datetime.strptime(market_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    end_of_day = datetime.combine(market_date + timedelta(days=1), dtime.min, tzinfo=tz)
    return (end_of_day - now.astimezone(tz)).total_seconds() / 3600.0


def check_hours_to_resolution(market_date_str: str,
                              tz_name: str | None,
                              min_hours: float = 2.0,
                              now: datetime | None = None) -> tuple[bool, str | None]:
    """Skip markets resolving in under min_hours (MIN_HOURS_TO_RES was
    defined in strategy.py but never enforced anywhere)."""
    hrs = hours_to_resolution(market_date_str, tz_name, now=now)
    if hrs is None:
        return False, f"unparseable market date {market_date_str!r}"
    if hrs < min_hours:
        return False, f"resolves in {hrs:.1f}h < {min_hours:.1f}h minimum"
    return True, None


class DailySpendTracker:
    """Tracks USDC deployed per UTC day across scan cycles.

    bot.py previously reset its spend counter every cycle, so the 'daily'
    cap was actually a per-15-minute cap — up to 96 cycles/day each with a
    fresh budget. This tracker rolls over at UTC midnight only.
    """

    def __init__(self, fraction: float = DAILY_SPEND_FRACTION):
        self.fraction = fraction
        self._day: str | None = None
        self._spent = 0.0

    def _roll(self, today: str) -> None:
        if self._day != today:
            self._day = today
            self._spent = 0.0

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def spent_today(self, today: str | None = None) -> float:
        self._roll(today or self._today())
        return self._spent

    def allows(self, size_usdc: float, bankroll: float,
               today: str | None = None) -> bool:
        self._roll(today or self._today())
        return self._spent + size_usdc <= bankroll * self.fraction

    def record(self, size_usdc: float, today: str | None = None) -> None:
        self._roll(today or self._today())
        self._spent += size_usdc


def run_sanity_gates(model_prob: float,
                     market_price: float | None,
                     primary_forecast_c: float | None,
                     secondary_forecast_c: float | None,
                     observed_c: float | None = None,
                     is_same_day_highest: bool = False) -> tuple[bool, str | None]:
    """Composite gate used by bot.py just before a signal is accepted.

    Returns (ok, reason). The first failing gate short-circuits; its reason
    is logged to scan_log with the SANITY_ prefix for auditability.
    """
    ok, why = check_cross_source_agreement(primary_forecast_c, secondary_forecast_c)
    if not ok:
        return False, f"SANITY_CROSS_SOURCE: {why}"

    if is_same_day_highest and primary_forecast_c is not None:
        ok, why = check_same_day_observed(primary_forecast_c, observed_c)
        if not ok:
            return False, f"SANITY_OBSERVED: {why}"

    ok, why = check_model_market_divergence(model_prob, market_price)
    if not ok:
        return False, f"SANITY_DIVERGENCE: {why}"

    return True, None
