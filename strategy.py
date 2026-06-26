"""
strategy.py - Edge calculation, probability estimation, and position sizing.
Uses fractional Kelly criterion for conservative position sizing.

Probability estimation (v2 — 2026-05-04):
  PRIMARY:   GFS ensemble empirical   — P(bucket) = count(members in bucket) / 30
             Zero calibration. Physics-based. GFS's own uncertainty estimate.
  FALLBACK:  Student's t-distribution — used when GFS ensemble is unavailable
             or for lowest-temp markets (GFS ensemble only covers max temp).

City blocking: cities with GFS ensemble std ≥ HIGH_GFS_SPREAD_THRESHOLD
(1.5°C) are blocked from trading. High spread = GFS can't agree = too risky.
"""

import functools
import logging
import sqlite3
from datetime import datetime, date
from zoneinfo import ZoneInfo
from scipy.stats import t as t_dist, norm as norm_dist
from weather_v2 import (
    celsius_to_fahrenheit,
    fahrenheit_to_celsius,
    get_city_gfs_ensemble,
    get_gfs_spread,
)

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# GFS Ensemble Spread Threshold — block cities GFS can't agree on
# Graduated by lead time: tighter for longer horizons.
# 5+ days out: blocked entirely (GFS error too large).
# -----------------------------------------------------------------------
# ── STABLE-MARKETS MODE (2026-05-04) ──
# GFS ensemble spread must be ≤ 0.7°C for any trade.
# This eliminates ~70% of markets but every survivor has genuine
# ensemble consensus — GFS physics agrees with itself.
GFS_SPREAD_THRESHOLD_BY_LEAD_TIME = {
    0:  0.7,   # Same-day: tight (METAR active, can't afford noise)
    1:  0.7,   # Tomorrow: tight
    2:  0.7,   # 2 days: tight
    3:  0.7,   # 3 days: tight
    4:  0.7,   # 4 days: tight
}
GFS_SPREAD_THRESHOLD_DEFAULT = 0.7
HIGH_GFS_SPREAD_THRESHOLD = 0.7  # kept for backward compat

MAX_LEAD_TIME_DAYS = 4  # Block all trades 5+ days out

# -----------------------------------------------------------------------
# Lead-time graduated entry thresholds
# Longer horizons = more uncertainty = demand more edge to trade
# -----------------------------------------------------------------------
ENTRY_THRESHOLD = 0.15  # Default (1-day, kept for backward compat)

ENTRY_THRESHOLD_BY_LEAD_TIME = {
    0:  0.15,   # Same-day: raised from 0.12 — Phase 3 conservative
    1:  0.20,   # Tomorrow: raised from 0.15
    2:  0.25,   # 2 days: raised from 0.20
    3:  0.30,   # 3 days: raised from 0.28
    4:  0.38,   # 4 days: raised from 0.35
}
ENTRY_THRESHOLD_DEFAULT = 0.20  # Fallback for unknown horizons

# -----------------------------------------------------------------------
# Tunable parameters - adjust based on your paper-trading results
# -----------------------------------------------------------------------
ENTRY_THRESHOLD      = 0.15   # Default (1-day) — kept for backward compat; ENTRY_THRESHOLD_BY_LEAD_TIME is authoritative
MAX_POSITION_USDC    = 2.0    # Reduced 2026-06-08: prioritize sample size over per-trade EV.
                              # Rationale (per session notes): cohort win rates are unstable on
                              # short windows (walk-forward Δ ±30%); Kelly sizing on biased
                              # probabilities is dangerous (Thorp); $2/trade gives 6× more
                              # data points per bankroll dollar for calibration convergence.
                              # FOK sweep path was already hardcoded to $2 (bot.py:855,875);
                              # this aligns the Kelly/GTC path with that ceiling.
MIN_POSITION_USDC    = 1.0    # Polymarket CLOB minimum order size.
                              # (Was 3.0; lowered to permit Kelly to size down to $1 when edge
                              # is weak. Without this, the clamp max(MIN, min(MAX, x)) would
                              # force everything to MIN when MIN > MAX.)
KELLY_FRACTION       = 0.08   # Conservative fractional Kelly (was 0.18)
MIN_HOURS_TO_RES     = 2.0    # Skip markets resolving in < 2 hours

# ── Realistic probability & edge caps (2026-05-17) ──────────────────────
# No single 1°C/2°F bucket should have >40% forecast probability.
# Polymarket prices these buckets at 2-15% because the market knows
# temperature is inherently uncertain. 85% on a single bucket is absurd.
# Likewise, edge >50% means the model claims near-certainty — unrealistic
# for any temperature forecast. These caps prevent snake-oil signals.
MAX_FORECAST_PROB = 0.65       # Sanity ceiling — Phase 1 sigma makes overconfidence impossible; honest ORHIGHER signals can legitimately hit 50-65%
MAX_EDGE          = 0.50       # Hard cap on edge (prob - market_price)

# Hard cap applied when the point forecast is on the adverse side of an
# open-ended bucket — i.e. the model is betting against its own forecast.
# Calibration (2026-06-08, 97 resolved trades) shows these yield <5% wins;
# the WU 2× sigma stacking inflates them to 34-35%, creating phantom FOK edge.
# Cap at 10% so no open-ended adverse bet qualifies under the 15% edge floor.
OPEN_ENDED_ADVERSE_PROB_CAP = 0.10

# -----------------------------------------------------------------------
# Blocked cities — markets with poor bucket coverage (backtest-verified)
# These cities use ORHIGHER/ORBELOW bucket structures on Polymarket where
# the actual winning temperature often falls in a range that isn't offered
# as a market option. Backtest results (Apr 2026):
#   Miami 0/7, San Francisco 1/6, Seattle 2/7, Chicago 2/6,
#   Denver 3/7, Atlanta 3/7, Panama City 2/6
# Do not trade until Polymarket offers full bucket coverage.
# -----------------------------------------------------------------------
# Watch-only cities: scan and collect data, but do NOT place new trades.
# These cities have poor performance (0% WR, high failure rates, structural gaps, or boss directive).
# Re-evaluate after investigation or 2 weeks of observation data.
WATCH_ONLY_CITIES: set[str] = {
    # Boss directive: never trade
    "Hong Kong", "Seoul",
    # ORHIGHER/ORBELOW coverage gaps — no full bucket coverage
    "Miami", "San Francisco", "Seattle", "Chicago",
    "Denver", "Atlanta", "Panama City",
    # City performance review 2026-05-19: 0% win rate, P&L < -$20
    "Helsinki",      # P&L -$36.17, WR 0%, 7 trades
    "Warsaw",        # P&L -$33.57, WR 0%, 9 trades, 4 failed
    "Wuhan",         # P&L -$30.17, WR 0%, 4 trades
    "Wellington",    # P&L -$29.32, WR 0%, 9 trades
    "NYC",           # P&L -$23.55, WR 0%, 10 trades
    "Lucknow",       # P&L -$21.08, WR 0%, 8 trades
    # High failure rate — investigate root cause
    "Moscow",        # 7/9 failed orders
    "Chongqing",     # 3/5 failed, no wins
    "Mexico City",   # 0% WR, 4 trades (small sample)
}

# BLOCKED_CITIES removed — all cities now in WATCH_ONLY_CITIES.
# Kept for backward compat (ORHIGHER_ORBELOW_CITIES references it).
BLOCKED_CITIES: set[str] = set()

# Cities where ORHIGHER/ORBELOW fallback applies — same set but named for clarity
ORHIGHER_ORBELOW_CITIES = BLOCKED_CITIES

# -----------------------------------------------------------------------
# Lottery cities — structurally high model error.
# Hong Kong and Seoul moved to BLOCKED_CITIES (Boss directive 2026-05-18).

# -----------------------------------------------------------------------
# Per-city model preference for non-WU fallback (Item 3 fix)
# Based on backtest MAE (April 2026, 47 forecasts across 27 cities).
# Ordered: first = most preferred, last = least.
# Used by ensemble_probability() when Wunderground data is unavailable.
# -----------------------------------------------------------------------
CITY_MODEL_PREFERENCE: dict[str, list[str]] = {
    # GraphCast-dominant cities
    "Beijing":       ["GraphCast", "ECMWF", "GFS"],
    "Mexico City":   ["GraphCast", "ECMWF", "GFS"],
    "LA":            ["GraphCast", "ECMWF", "GFS"],
    "NYC":           ["GraphCast", "ECMWF", "GFS"],
    "Toronto":       ["GraphCast", "ECMWF", "GFS"],
    # ECMWF-dominant cities
    "Ankara":        ["ECMWF", "GraphCast", "GFS"],
    "Buenos Aires":  ["ECMWF", "GraphCast", "GFS"],
    "Istanbul":      ["ECMWF", "GraphCast", "GFS"],
    "London":        ["ECMWF", "GFS", "GraphCast"],
    "Lucknow":       ["ECMWF", "GraphCast", "GFS"],
    "Madrid":        ["ECMWF", "GraphCast", "GFS"],
    "Paris":         ["ECMWF", "GraphCast", "GFS"],
    "Seoul":         ["ECMWF", "GraphCast", "GFS"],
    "Singapore":     ["ECMWF", "GraphCast", "GFS"],
    "Tokyo":         ["ECMWF", "GraphCast", "GFS"],
    "Warsaw":        ["ECMWF", "GraphCast", "GFS"],
    "Wuhan":         ["ECMWF", "GraphCast", "GFS"],
    # GFS-dominant cities
    "Helsinki":      ["GFS", "ECMWF", "GraphCast"],
    "Milan":         ["GFS", "ECMWF", "GraphCast"],
    "Sao Paulo":     ["GFS", "ECMWF", "GraphCast"],
    "Taipei":        ["GFS", "ECMWF", "GraphCast"],
    "Tel Aviv":      ["GFS", "ECMWF", "GraphCast"],
    "Wellington":    ["GFS", "ECMWF", "GraphCast"],
}
# Default for cities not in dict (no backtest data yet)
# ECMWF wins most often overall (14/27 cities in backtest)
MODEL_PREFERENCE_DEFAULT: list[str] = ["ECMWF", "GraphCast", "GFS"]


# ---------------------------------------------------------------------------
# GFS Forecast Bias Correction (2026-05-26)
# Per-city, per-horizon warm bias derived from forecast_log vs METAR actuals.
# GFS is systematically warm-biased in many cities, especially at day+2–4.
# We correct by shifting the bucket threshold UP by the bias before counting
# ensemble members, making it harder for warm-biased members to qualify.
# ---------------------------------------------------------------------------
GFS_BIAS_DB = "/root/weathercore/bias/bias.db"

CITY_TO_ICAO: dict[str, str] = {
    "Amsterdam": "EHAM", "Ankara": "LTAC", "Atlanta": "KATL",
    "Austin": "KAUS", "Beijing": "ZBAA", "Buenos Aires": "SAEZ",
    "Busan": "RKPK", "Cape Town": "FACT", "Chengdu": "ZUUU",
    "Chicago": "KORD", "Chongqing": "ZUCK", "DC": "KDCA",
    "Dallas": "KDFW", "Denver": "KDEN", "Guangzhou": "ZGGG",
    "Helsinki": "EFHK", "Hong Kong": "VHHH", "Houston": "KIAH",
    "Istanbul": "LTFM", "Jakarta": "WIII", "Jeddah": "OEJN",
    "Karachi": "OPKC", "Kuala Lumpur": "WMKK", "LA": "KLAX",
    "Lagos": "DNMM", "London": "EGLL", "Lucknow": "VILK",
    "Madrid": "LEMD", "Manila": "RPLL", "Mexico City": "MMMX",
    "Miami": "KMIA", "Milan": "LIMC", "Moscow": "UUEE",
    "Munich": "EDDM", "NYC": "KJFK", "Panama City": "MPTO",
    "Paris": "LFPG", "San Francisco": "KSFO", "Sao Paulo": "SBGR",
    "Seattle": "KSEA", "Seoul": "RKSI", "Shanghai": "ZSPD",
    "Shenzhen": "ZGSZ", "Singapore": "WSSS", "Taipei": "RCTP",
    "Tel Aviv": "LLBG", "Tokyo": "RJTT", "Toronto": "CYYZ",
    "Warsaw": "EPWA", "Wellington": "NZWN", "Wuhan": "ZHHH",
}


@functools.lru_cache(maxsize=256)
def get_gfs_bias_c(city: str, horizon_days: int) -> float:
    """Return per-city, per-horizon GFS warm bias in Celsius.

    Positive = GFS runs too warm (most NA cities).
    Negative = GFS runs too cold (e.g. Tokyo, Seoul, Singapore).
    Returns 0.0 if no data available — no correction applied.

    Source: forecast_log.max_c vs bias_samples.observed_c (METAR actual).
    Refreshed monthly by re-running snapshot_forecast.py + bias_backfill.
    """
    icao = CITY_TO_ICAO.get(city)
    if not icao:
        return 0.0
    try:
        conn = sqlite3.connect(GFS_BIAS_DB)
        row = conn.execute("""
            SELECT AVG(f.max_c - b.observed_c)
            FROM forecast_log f
            JOIN bias_samples b ON f.icao = b.icao AND f.target_date = b.date
            WHERE f.icao = ?
              AND f.horizon_days = ?
              AND f.model = 'GFS'
              AND b.observed_c IS NOT NULL
        """, (icao, horizon_days)).fetchone()
        conn.close()
        if row and row[0] is not None:
            bias = float(row[0])
            logger.debug(f"GFS bias lookup: {city} ({icao}) h={horizon_days} → {bias:+.3f}°C")
            return bias
    except Exception as e:
        logger.debug(f"GFS bias lookup failed for {city} h={horizon_days}: {e}")
    return 0.0


def is_city_tradable(city: str) -> bool:
    """Return False if the city is on the blocklist."""
    return city not in BLOCKED_CITIES


def is_city_watch_only(city: str) -> bool:
    """Return True if the city is watch-only (observe but don't trade)."""
    return city in WATCH_ONLY_CITIES

# DAILY DRAWDOWN PROTECTION
MAX_DAILY_LOSS_PCT   = 8.0    # Pause trading for the day if down more than this %

# UNRESOLVED EXPOSURE CAP (Phase E — 2026-05-03)
# If total open position size exceeds this fraction of bankroll, stop buying.
# Prevents the bot from piling into positions when capital is trapped
# unresolved. May 2: bot had $40+ open vs $24 bankroll (167% exposure).
UNRESOLVED_EXPOSURE_CAP = 0.50  # Max 50% of bankroll in unresolved positions

# Horizon-dependent probability floors for Phase 1 screening.
# Same-day and next-day floors lowered (was 0.45/0.38) to unblock the correct
# bucket on 5-outcome markets where even the most likely outcome rarely tops 40%.
# Phase 2 best-bucket selection + SOFT_MIN_PROB backstop provide the remaining
# quality filter so loosening here doesn't open the floodgates.
PROB_FLOOR_BY_HORIZON = {
    0: 0.38,   # Same day  (raised from 0.35 — Phase 1)
    1: 0.35,   # Tomorrow  (raised from 0.30)
    2: 0.35,   # 2-day     (raised from 0.32)
    3: 0.32,   # 3-day     (raised from 0.28)
    4: 0.28,   # 4-day     (raised from 0.25)
    5: 0.25,   # 5-day     (raised from 0.22)
    6: 0.22,   # 6-day     (raised from 0.20)
    7: 0.20,   # 7-day     (raised from 0.18)
    8: 0.18,   # 8-day     (raised from 0.16)
    9: 0.16,   # 9-day     (raised from 0.15)
    10: 0.15,  # 10-day    (raised from 0.14)
}
PROB_FLOOR_DEFAULT = 0.14  # Fallback for 11+ days (raised from 0.13)

# Soft absolute floor applied in Phase 2 AFTER best-bucket selection.
# Even the highest-prob bucket in a market must clear this or we skip the market
# entirely. Acts as a backstop now that Phase 1 floors are more permissive.
SOFT_MIN_PROB = 0.30  # Raised from 0.25 — Phase 1

# ── Absolute minimum probability floor (Boss directive 2026-05-18) ──────
# No trade below 20% model probability, regardless of horizon or market type.
# This is a hard floor that overrides all horizon-dependent floors.
# Combined with MAX_FORECAST_PROB=0.65, honest ORHIGHER probs are allowed through.
ABSOLUTE_MIN_PROB = 0.30  # Raised from 0.20 — Phase 1 conservative floor
# Daily minimum temperature forecasts have no historical calibration data
# from our trading. Values are based on ECMWF verification literature
# (2m min RMSE ~2-3K, similar to max) with a 1.2x safety factor.
# These will be recalibrated once we accumulate 20+ resolved positions.
# -----------------------------------------------------------------------
LOWEST_SIGMA_MULTIPLIER = 1.2    # Conservative 20% wider sigma vs max-temp
LOWEST_PROB_FLOOR = {
    0: 0.40,   # Same-day (vs 0.35 for max-temp)
    1: 0.35,   # Tomorrow (vs 0.30)
    2: 0.35,   # (vs 0.32)
    3: 0.32,   # (vs 0.28)
    4: 0.28,   # (vs 0.25)
    5: 0.25,   # (vs 0.22)
    6: 0.22,   # (vs 0.20)
    7: 0.20,   # (vs 0.18)
    8: 0.18,   # (vs 0.16)
    9: 0.16,   # (vs 0.15)
    10: 0.15,  # (vs 0.14)
}
LOWEST_PROB_FLOOR_DEFAULT = 0.14  # Fallback for 11+ days (vs 0.13)
LOWEST_SOFT_MIN_PROB = 0.30       # Soft absolute floor (vs 0.25)

# -----------------------------------------------------------------------
# Dynamic sigma (forecast uncertainty in Fahrenheit)
# Based on NWS/ECMWF verification: 1-day MAE ~2-3F, 3-day ~4-5F.
# Keys are days-ahead (0 = today, 1 = tomorrow, etc.)
# RECALIBRATED 2026-05-03: increased ~1.4x based on 30-day backtest
# (83 resolved positions, Apr-May 2026) showing actual RMSE 2-3x
# larger than theoretical sigma. Base same-day raised from 2.5→3.5F.
# -----------------------------------------------------------------------
SIGMA_BY_HORIZON_F = {
    0: 3.5,    # Same-day: 1.94°C (was 2.5F/1.39°C — too tight)
    1: 5.5,    # Tomorrow (was 4.0)
    2: 7.5,    # 2 days out (was 5.5)
    3: 9.0,    # 3 days out (was 6.5)
    4: 10.5,   # 4 days out (was 7.5)
    5: 12.0,   # 5 days out (was 8.5)
    6: 13.5,   # 6 days out (was 9.5)
    7: 15.0,   # 7 days out (was 10.5)
    8: 16.5,   # 8 days out (was 11.5)
    9: 17.0,   # 9 days out (was 12.0)
}
SIGMA_DEFAULT_F = 17.0  # Fallback for 10+ days (was 12.0)

# -----------------------------------------------------------------------
# City-specific sigma multipliers.
# Applied on top of the horizon-based sigma to capture local climate
# variability. Continental/inland cities have wider distributions;
# tropical and desert cities have tighter ones.
#
# RECALIBRATED 2026-05-03: values derived from 83 resolved positions
# (Apr-May 2026) comparing bucket midpoints vs actual observed temps.
# RMSE ranged from 0.16°C (Beijing) to 3.24°C (Seoul). Multipliers
# include 1.5x safety factor to account for bucket-midpoint being a
# lower bound on true forecast error. Base sigma was also raised 1.4x.
# -----------------------------------------------------------------------
CITY_SIGMA_MULTIPLIER = {
    # HIGH VARIABILITY — verified RMSE > 1.5°C (n≥2)
    "Seoul":         2.5,   # rmse=3.24°C n=9 — worst performer, way too tight before
    "Ankara":        2.0,   # rmse=2.32°C n=3
    "Wuhan":         1.5,   # rmse=2.15°C n=3
    "Amsterdam":     1.5,   # rmse=2.10°C n=2
    "Toronto":       1.5,   # rmse=1.91°C n=4 (was 1.5 — confirmed)
    "Istanbul":      1.5,   # rmse=1.66°C n=2
    "Hong Kong":     1.5,   # rmse=1.60°C n=3
    "Warsaw":        1.5,   # rmse=1.40°C n=5
    "Munich":        1.5,   # rmse=1.36°C n=2
    "Helsinki":      1.5,   # rmse=1.30°C n=4
    # Continental/inland — high climate variability (literature + RMSE data)
    "Moscow":        1.5,   # (was 1.5)
    "Chicago":       1.5,   # (was 1.4)
    "Atlanta":       1.5,   # (was 1.3)
    "Houston":       1.5,   # (was 1.2)
    "Beijing":       1.0,   # rmse=0.16°C n=3 — very stable in spring
    "Chongqing":     1.5,   # (was 1.3)
    "Denver":        1.5,   # (was 1.4)
    # MODERATE — rmse 0.5-1.5°C
    "London":        1.5,   # (was 1.2)
    "Tokyo":         1.0,   # (was 1.1 — stable maritime)
    "Wellington":    1.5,   # (was 1.3)
    "Seattle":       1.5,   # (was 1.2)
    "Milan":         1.0,   # rmse=0.45°C n=2 — stable
    "Paris":         1.0,   # rmse=0.98°C n=2
    "Mexico City":   1.0,   # rmse=1.07°C n=3
    "Lucknow":       1.0,   # rmse=1.00°C n=2
    # COASTAL / MEDITERRANEAN — low variability
    "Madrid":        1.0,   # rmse=0.24°C n=3 (was 1.0)
    "Tel Aviv":      1.0,   # rmse=0.42°C n=3 (was 0.95)
    "LA":            1.0,   # (was 0.9)
    "Sao Paulo":     1.0,   # (was 1.0)
    "Buenos Aires":  1.0,   # (was 1.1)
    "Shenzhen":      1.0,   # (was 1.1)
    "Miami":         1.0,   # blocked city but included for completeness
    # TROPICAL — tight temperature range day-to-day
    "Singapore":     1.0,   # (was 0.85)
    "Kuala Lumpur":  1.0,   # (was 0.85)
    "Bangkok":       1.0,   # (was 0.85)
    "Lagos":         1.0,   # (was 0.85)
    "Jakarta":       1.0,   # (was 0.85)
    # Indian subcontinent
    "Mumbai":        1.0,   # (was 0.9)
    "Kolkata":       1.0,   # (was 0.9)
}


# -----------------------------------------------------------------------
# WU forecast sigma horizon scaling — Phase 1 fix.
# WU accuracy degrades with forecast lead time. Cities in WU_CITY_SIGMA_F
# have a fixed same-day sigma; this table scales it up for future dates.
# Applied inside wu_normal_probability() for both table and fallback cities.
# Derived from WU MAE statistics (same-day ~2°C, 3-day ~3.5°C typical).
# -----------------------------------------------------------------------
WU_HORIZON_SCALE = {
    0: 1.0,    # Same-day: table value as-is
    1: 1.4,    # 1-day: 40% wider
    2: 1.8,    # 2-day: 80% wider
    3: 2.3,    # 3-day: 130% wider
    4: 2.8,    # 4-day: 180% wider
}
WU_HORIZON_SCALE_DEFAULT = 3.0  # 5+ days: 200% wider

# -----------------------------------------------------------------------
# WU-specific per-city sigma (degrees F) for wu_normal_probability().
# Derived from historical WU forecast error analysis.
# When a city is not in this dict, fallback = 2x horizon-based sigma.
# EMPIRICAL VALUES (from Option C):
# Base same-day sigma = 3.5°F → doubled to 7.0°F per Option A.
# City multipliers applied to this base where data available.
# -----------------------------------------------------------------------
WU_CITY_SIGMA_F = {
    # HIGH VARIABILITY — WU error > 3°F
    "Seoul":         14.0,  # 7.0 * 2.0 (CITY_SIGMA_MULTIPLIER 2.0)
    "Ankara":        14.0,  # 7.0 * 2.0
    "Wuhan":         10.5,  # 7.0 * 1.5
    "Toronto":       10.5,  # 7.0 * 1.5
    # DEFAULT FALLBACK — moderate variability
    "London":        10.5,  # 7.0 * 1.5
    "Tokyo":          7.0,  # 7.0 * 1.0 — stable maritime default
    "Milan":          7.0,  # 7.0 * 1.0 — stable
    "Paris":          7.0,  # 7.0 * 1.0 — stable
    "Singapore":      7.0,  # 7.0 * 1.0 — tropical stable
    # All other cities: fallback to 2x horizon-based sigma (calculated at runtime)
}

# -----------------------------------------------------------------------
# Dynamic degrees of freedom for Student's t-distribution.
# Lower df = fatter tails = more probability assigned to extreme outcomes.
# Same-day (df=12) has modest fat tails; 3-day (df=4) is meaningfully fat.
# RECALIBRATED 2026-05-03: lowered across all horizons because the
# previous near-Normal distributions (df=20 for same-day) produced
# absurdly overconfident probabilities (95% claimed, 17% actual).
# -----------------------------------------------------------------------
DF_BY_HORIZON = {
    0: 12.0,   # Same-day: modest fat tails (was 20 — near-Normal, too tight)
    1:  8.0,   # Tomorrow (was 12)
    2:  5.0,   # 2 days out (was 7)
    3:  4.0,   # 3 days out (was 5)
    4:  3.0,   # 4 days out (was 4)
    5:  2.5,   # 5 days out (was 3.5)
    6:  2.5,   # 6 days out (was 3)
    7:  2.0,   # 7 days out (was 2.5)
    8:  2.0,   # 8 days out (was 2.5)
    9:  2.0,   # 9 days out (was 2)
}
DF_DEFAULT = 2.0  # Fallback for 10+ days

# Use US Eastern as default timezone for date calculations since most markets are US-based.
_DEFAULT_TZ = ZoneInfo("America/New_York")


def _today() -> date:
    """Return today's date in US Eastern timezone."""
    return datetime.now(_DEFAULT_TZ).date()


def get_prob_floor(market_date_str: str = None, market_type: str = "highest") -> float:
    """
    Returns the minimum forecast probability required to trade,
    based on forecast horizon. Shorter horizons require higher
    confidence since the forecast is more reliable.

    Args:
        market_date_str: ISO date string (YYYY-MM-DD) of the market
        market_type: 'highest' or 'lowest'. Lowest-temp uses more
                     conservative floors due to zero calibration data.
    """
    if market_date_str is None:
        return PROB_FLOOR_DEFAULT
    try:
        market_date = datetime.strptime(market_date_str, "%Y-%m-%d").date()
        days_ahead = max(0, (market_date - _today()).days)
    except (ValueError, TypeError):
        return PROB_FLOOR_DEFAULT
    if market_type == "lowest":
        return LOWEST_PROB_FLOOR.get(days_ahead, LOWEST_PROB_FLOOR_DEFAULT)
    return PROB_FLOOR_BY_HORIZON.get(days_ahead, PROB_FLOOR_DEFAULT)


def _get_params(market_date_str: str, unit: str = "F", city: str = None,
                market_type: str = "highest") -> tuple:
    """
    Returns (sigma, df) for a market based on:
    1. Forecast horizon (days until market date)
    2. Temperature unit (C markets get sigma scaled by 1/1.8)
    3. City-specific multiplier (continental cities have wider distributions)
    4. Market type — lowest-temp applies LOWEST_SIGMA_MULTIPLIER (1.2x)

    sigma controls spread; df controls tail fatness of the t-distribution.

    Args:
        market_date_str: ISO date string (YYYY-MM-DD) of the market
        unit: "F" or "C"
        city: City name (optional) — applies CITY_SIGMA_MULTIPLIER if known
        market_type: 'highest' or 'lowest'

    Returns:
        (sigma, df) tuple
    """
    try:
        market_date = datetime.strptime(market_date_str, "%Y-%m-%d").date()
        days_ahead = (market_date - _today()).days
        days_ahead = max(0, days_ahead)  # Clamp: same-day or past = 0
    except (ValueError, TypeError):
        days_ahead = 2  # Conservative default if date parse fails

    sigma_f = SIGMA_BY_HORIZON_F.get(days_ahead, SIGMA_DEFAULT_F)
    df = DF_BY_HORIZON.get(days_ahead, DF_DEFAULT)

    # Apply city-specific multiplier if available
    if city:
        multiplier = CITY_SIGMA_MULTIPLIER.get(city, 1.0)
        sigma_f *= multiplier

    # Apply lowest-temp safety multiplier (no calibration data yet)
    if market_type == "lowest":
        sigma_f *= LOWEST_SIGMA_MULTIPLIER

    if unit.upper() == "C":
        # Convert Fahrenheit sigma to Celsius: divide by 1.8
        return sigma_f / 1.8, df

    return sigma_f, df


# ── Empirical GFS Ensemble Probability ───────────────────────────────
def empirical_probability(gfs_member_vals: list[float],
                          bucket_low: float | None,
                          bucket_high: float | None) -> float:
    """P(bucket) = count(GFS members in bucket) / total_members.

    Zero calibration. Zero assumptions. Physics-based — these are 30
    independent perturbed GFS runs from NOAA. The ensemble spread IS the
    uncertainty estimate.

    Args:
        gfs_member_vals: 30 GFS ensemble member temperatures (Celsius).
                         As returned by weather_v2.get_city_gfs_ensemble().
        bucket_low:      lower bound (None = -infinity).
        bucket_high:     upper bound (None = +infinity).

    Returns:
        probability float in [0.001, 0.999] — clamped to avoid zero/one.
    """
    if not gfs_member_vals:
        return 0.0

    in_bucket = 0
    for v in gfs_member_vals:
        above_low = bucket_low is None or v >= bucket_low
        below_high = bucket_high is None or v < bucket_high
        if above_low and below_high:
            in_bucket += 1

    prob = in_bucket / len(gfs_member_vals)
    return max(0.001, min(0.999, prob))


def empirical_probability_with_bias(gfs_member_vals: list[float],
                                     bucket_low: float | None,
                                     bucket_high: float | None,
                                     city: str = None) -> float:
    """empirical_probability — bias correction DISABLED (2026-05-05).

    Backtests showed per-city GFS ensemble bias degrades MAE:
      no-bias 1.19°C vs IEM 1.56°C vs ERA5 1.52°C vs GFS-thin 1.88°C
    See weathercore/backtest/forecast_backtest.py results.

    Now delegates directly to raw empirical_probability.
    """
    return empirical_probability(gfs_member_vals, bucket_low, bucket_high)


def compute_lead_time_days(market_date_str: str) -> int:
    """Days from today to market resolution date. 0 = same-day."""
    from datetime import date as date_type
    try:
        market_date = datetime.strptime(market_date_str, "%Y-%m-%d").date()
        return max(0, (market_date - _today()).days)
    except (ValueError, TypeError):
        return 0


def get_entry_threshold(market_date_str: str = None) -> float:
    """Return the graduated entry threshold for the given lead time."""
    if market_date_str is None:
        return ENTRY_THRESHOLD_DEFAULT
    lead_days = compute_lead_time_days(market_date_str)
    if lead_days > MAX_LEAD_TIME_DAYS:
        return 999.0  # Effectively blocked
    return ENTRY_THRESHOLD_BY_LEAD_TIME.get(lead_days, ENTRY_THRESHOLD_DEFAULT)


def get_spread_threshold(market_date_str: str = None) -> float:
    """Return the graduated GFS spread threshold for the given lead time."""
    if market_date_str is None:
        return GFS_SPREAD_THRESHOLD_DEFAULT
    lead_days = compute_lead_time_days(market_date_str)
    return GFS_SPREAD_THRESHOLD_BY_LEAD_TIME.get(
        lead_days, GFS_SPREAD_THRESHOLD_DEFAULT)


def is_city_gfs_stable(city: str, target_date: str,
                       threshold: float = None) -> bool:
    """Check if GFS ensemble spread is below threshold for this city+date.

    Uses graduated threshold by lead time (tighter for longer horizons).
    Returns True if stable enough to trade.
    Returns True if GFS ensemble is unavailable (don't block on missing data).
    Returns False if lead time exceeds MAX_LEAD_TIME_DAYS.
    """
    lead_days = compute_lead_time_days(target_date)
    if lead_days > MAX_LEAD_TIME_DAYS:
        return False  # Too far out — don't trade

    if threshold is None:
        threshold = get_spread_threshold(target_date)

    spread = get_gfs_spread(city, target_date)
    if spread is None:
        return True  # No data = don't block
    return spread < threshold


def forecast_probability(forecast_temp: float, bucket_low: float | None,
                          bucket_high: float | None, unit: str = "F",
                          model_uncertainty_deg: float = None,
                          market_date: str = None,
                          city: str = None,
                          forecast_bias: float = 0.0,
                          market_type: str = "highest",
                          is_wunderground: bool = False) -> float:
    """
    Estimates the probability that the actual temperature falls within the
    bucket [bucket_low, bucket_high] given a point forecast.

    Uses a Student's t-distribution centered on forecast_temp. The degrees
    of freedom (df) scale with forecast horizon: short horizons use high df
    (near-Normal), long horizons use low df (fat tails that assign more
    probability to extreme outcomes, reducing overconfident bets).

    Args:
        forecast_temp:        forecast temperature (in the same unit as bucket)
        bucket_low:           lower bound of bucket (None = -infinity)
        bucket_high:          upper bound of bucket (None = +infinity)
        unit:                 "F" or "C"
        model_uncertainty_deg: override sigma (degrees). If None, computed
                              dynamically from market_date and unit.
        market_date:          ISO date string for dynamic sigma/df calculation

    Returns:
        probability float in [0.001, 0.999]
    """
    if model_uncertainty_deg is not None:
        sigma = model_uncertainty_deg
        # When sigma is overridden, use moderate df (no horizon info available)
        df = DF_BY_HORIZON.get(2, DF_DEFAULT)
    elif market_date is not None:
        sigma, df = _get_params(market_date, unit, city=city, market_type=market_type)
    else:
        # Fallback: use unit-aware default (assumes ~2 day horizon)
        sigma = 5.5 if unit.upper() == "F" else 5.5 / 1.8
        df = DF_BY_HORIZON.get(2, DF_DEFAULT)

    # WU pathway: double sigma to correct overconfidence in WU forecasts.
    # WU same-day sigma=3.5°F is too tight → 99.9% for modest buffers.
    # 2x multiplier (→7.0°F same-day) aligns t-dist probs with empirical.
    if is_wunderground:
        sigma *= 2.0

    mu = forecast_temp + forecast_bias

    low_p  = t_dist.cdf(bucket_low,  df, loc=mu, scale=sigma) if bucket_low  is not None else 0.0
    high_p = t_dist.cdf(bucket_high, df, loc=mu, scale=sigma) if bucket_high is not None else 1.0

    prob = high_p - low_p

    # Open-ended adverse cap: when the point forecast is on the wrong side of
    # an open-ended bucket boundary, sigma inflation can push prob to 30-35%
    # even though the forecast is 5°C into enemy territory.  Cap at
    # OPEN_ENDED_ADVERSE_PROB_CAP so no fake edge passes the entry floor.
    if bucket_low is None and bucket_high is not None and mu > bucket_high:
        prob = min(prob, OPEN_ENDED_ADVERSE_PROB_CAP)
    elif bucket_high is None and bucket_low is not None and mu < bucket_low:
        prob = min(prob, OPEN_ENDED_ADVERSE_PROB_CAP)

    # Clamp to valid probability range
    return max(0.001, min(0.999, prob))


def ensemble_probability(model_forecasts: dict, bucket_low: float | None,
                         bucket_high: float | None, unit: str = "F",
                         market_date: str = None, city: str = None,
                         live_bias: float = 0.0,
                         **kwargs) -> tuple[float, dict]:
    """Compute per-model probabilities and return the average of the top 2.

    Args:
        model_forecasts: {model_label: {date_str: temp_celsius, ...}}
                         as returned by get_ensemble_forecast() or
                         get_station_ensemble_forecast().
        bucket_low:      lower bound of bucket (None = -inf)
        bucket_high:     upper bound of bucket (None = +inf)
        unit:            "F" or "C" (the unit of bucket_low/bucket_high)
        market_date:     ISO date string for dynamic sigma/df
        city:            city name for city-specific sigma multiplier
        **kwargs:        forwarded to forecast_probability()

    Returns:
        (combined_prob, details_dict)

        combined_prob:  simple average of the 2 highest per-model probabilities.
                        If only 1 model is available, returns that single probability.

        details_dict:   {"per_model": {label: {"temp": t, "prob": p}, ...},
                         "top2_labels": [label1, label2],
                         "combined_prob": float,
                         "n_models": int}
    """
    if not model_forecasts:
        raise ValueError("model_forecasts is empty; cannot compute ensemble probability")

    # Extract the forecast temp for market_date from each model
    per_model: dict = {}
    for label, date_temps in model_forecasts.items():
        if market_date and market_date in date_temps:
            temp_c = date_temps[market_date]
        else:
            # If no specific date requested, use the first available date
            if date_temps:
                temp_c = next(iter(date_temps.values()))
            else:
                continue

        # Convert to market unit for probability calculation
        if unit.upper() == "F":
            temp_market = celsius_to_fahrenheit(temp_c)
        else:
            temp_market = temp_c

        # Convert live_bias to market unit if needed (bias is always in Celsius)
        bias_market = live_bias
        if unit.upper() == "F" and live_bias != 0.0:
            bias_market = celsius_to_fahrenheit(live_bias)
        
        prob = forecast_probability(
            temp_market, bucket_low, bucket_high,
            unit=unit, market_date=market_date, city=city,
            forecast_bias=bias_market, **kwargs
        )
        per_model[label] = {"temp_c": temp_c, "temp_market": temp_market, "prob": prob}

    if not per_model:
        raise ValueError("No model produced a usable forecast for the requested date")

    # ── Guard: require at least 2 models for a valid ensemble ──
    # A single-model "ensemble" is just a single point of failure.
    # If only 1 model returned data, skip the market entirely.
    if len(per_model) < 2:
        available = list(per_model.keys())
        raise ValueError(
            f"Insufficient models for ensemble: only {len(per_model)} model(s) "
            f"returned data ({available}). Need at least 2. Skipping market."
        )

    # ── Weighted ensemble (Item 3 fix) ─────────────────────────────────
    # Use per-city model preference weights instead of generic top-2 average.
    # Weights: 50% for 1st choice, 30% for 2nd, 20% for 3rd.
    # Only uses models that returned data; renormalizes if some are missing.
    city_prefs = CITY_MODEL_PREFERENCE.get(city, MODEL_PREFERENCE_DEFAULT)

    # Build weight lookup: {model_label: weight}
    raw_weights: dict[str, float] = {}
    for i, model_label in enumerate(city_prefs):
        if model_label in per_model:
            if i == 0:
                raw_weights[model_label] = 0.50
            elif i == 1:
                raw_weights[model_label] = 0.30
            elif i == 2:
                raw_weights[model_label] = 0.20

    # If none of the preferred models returned data, fall back to equal weight
    if not raw_weights:
        for label in per_model:
            raw_weights[label] = 1.0 / len(per_model)

    # Renormalize weights to sum to 1.0
    weight_sum = sum(raw_weights.values())
    weights = {l: w / weight_sum for l, w in raw_weights.items()}

    # Compute weighted probability
    combined_prob = sum(
        per_model[label]["prob"] * w for label, w in weights.items()
    )

    # Sort by probability descending for logging
    sorted_models = sorted(per_model.items(), key=lambda x: x[1]["prob"], reverse=True)
    top2_labels = [label for label, _ in sorted_models[:2]]

    details = {
        "per_model": per_model,
        "top2_labels": top2_labels,
        "combined_prob": round(combined_prob, 6),
        "n_models": len(per_model),
        "weights": {l: round(w, 3) for l, w in weights.items()},
    }

    model_strs = ", ".join(
        f"{l}={d['temp_market']:.1f}°{unit.upper()}→{d['prob']:.3f}(w={weights.get(l, 0):.0%})"
        for l, d in sorted_models
    )
    logger.info(
        f"Ensemble: {model_strs} => weighted={combined_prob:.4f} | "
        f"n={len(per_model)} top={top2_labels}"
    )

    return combined_prob, details


def find_edge(forecast_prob: float, market_price: float) -> float:
    """
    Edge = forecast probability minus market-implied probability.
    Positive edge means market is underpricing our forecast outcome.
    """
    return forecast_prob - market_price


def classify_skip_reason(edge: float, forecast_prob: float = None,
                         market_date: str = None,
                         market_type: str = "highest") -> str | None:
    """
    Returns a short, stable label describing which gate failed, or None if
    should_trade would pass. Used so scan_log has a filterable reason column
    instead of everything being lumped into 'edge_too_small'.

    Labels:
      - 'negative_edge'         : edge < 0 (market prices the outcome MORE
                                   likely than our model does — do not buy)
      - 'prob_below_min'        : forecast prob below ABSOLUTE_MIN_PROB (20%)
      - 'prob_below_floor'      : edge is positive but forecast prob is below
                                   the horizon-dependent floor — low-confidence
                                   bucket, skip regardless of apparent edge
      - 'lead_time_exceeded'    : market too far out (>MAX_LEAD_TIME_DAYS)
      - 'edge_below_threshold'  : prob clears the floor but edge < graduated threshold
      - 'prob_above_max'        : forecast prob exceeds MAX_FORECAST_PROB (unrealistic)
      - 'edge_above_max'        : edge exceeds MAX_EDGE (unrealistic certainty)
    """
    lead_days = compute_lead_time_days(market_date) if market_date else 0
    if lead_days > MAX_LEAD_TIME_DAYS:
        return "lead_time_exceeded"
    if edge < 0:
        return "negative_edge"
    # ── Hard floor: reject probabilities below absolute minimum ──
    if forecast_prob is not None and forecast_prob < ABSOLUTE_MIN_PROB:
        return "prob_below_min"
    # ── Hard cap checks (must come before floor/threshold checks) ──
    if forecast_prob is not None and forecast_prob > MAX_FORECAST_PROB:
        return "prob_above_max"
    if edge > MAX_EDGE:
        return "edge_above_max"
    if forecast_prob is not None:
        floor = get_prob_floor(market_date, market_type=market_type)
        if forecast_prob < floor:
            return "prob_below_floor"
    threshold = get_entry_threshold(market_date)
    if edge < threshold:
        return "edge_below_threshold"
    return None


def should_trade(edge: float, forecast_prob: float = None,
                  market_date: str = None,
                  market_type: str = "highest") -> bool:
    """
    Returns True if edge exceeds the graduated entry threshold AND forecast
    probability meets the dynamic floor for the given horizon.

    Graduated thresholds: same-day requires 12% edge, day 4 requires 35%.
    Markets 5+ days out are blocked entirely.

    The probability floor prevents the bot from buying buckets that
    the model itself considers unlikely.

    Hard caps (2026-05-17):
      - MAX_FORECAST_PROB (40%): No single bucket should exceed this. Market
        prices of 2-15% reflect genuine uncertainty the model must respect.
      - MAX_EDGE (50%): Edge above 50% means near-certainty on a temperature
        bucket — physically unrealistic. Cap prevents snake-oil signals.
      - ABSOLUTE_MIN_PROB (20%): No trade below 20% model probability (Boss 2026-05-18).
        Overrides all horizon-dependent floors. Combined with 40% cap, range is 20-40%.
    """
    lead_days = compute_lead_time_days(market_date) if market_date else 0
    if lead_days > MAX_LEAD_TIME_DAYS:
        return False

    # ── Hard floor: reject probabilities below absolute minimum ──
    if forecast_prob is not None and forecast_prob < ABSOLUTE_MIN_PROB:
        logger.info(
            f"SKIP (prob < MIN {ABSOLUTE_MIN_PROB:.0%}): "
            f"prob={forecast_prob:.1%} — too low confidence for any trade"
        )
        return False

    # ── Hard cap: reject absurd probabilities ──
    if forecast_prob is not None and forecast_prob > MAX_FORECAST_PROB:
        logger.info(
            f"SKIP (prob > MAX {MAX_FORECAST_PROB:.0%}): "
            f"prob={forecast_prob:.1%} is unrealistic for a single bucket"
        )
        return False

    # ── Hard cap: reject absurd edges ──
    if edge > MAX_EDGE:
        logger.info(
            f"SKIP (edge > MAX {MAX_EDGE:.0%}): "
            f"edge={edge:+.1%} implies unrealistic certainty"
        )
        return False

    threshold = get_entry_threshold(market_date)
    if edge < threshold:
        return False
    if forecast_prob is not None:
        floor = get_prob_floor(market_date, market_type=market_type)
        if forecast_prob < floor:
            return False
    return True


# -----------------------------------------------------------------------
# LOWER EDGE SYSTEM (Defensive-only engagement)
# Only engages when the *lower-bound* edge (conservative prob with
# increased sigma) still exceeds a threshold. Implements "lower edge cases"
# filter for more defensive posture post-drawdown. Uses higher uncertainty
# to compute a pessimistic probability before allowing trade.
# -----------------------------------------------------------------------
LOWER_EDGE_THRESHOLD = 0.10   # Must have edge even under conservative assumptions
LOWER_SIGMA_FACTOR = 1.35     # Increase uncertainty for lower-bound prob

def get_conservative_probability(forecast_temp: float, bucket_low: float | None,
                                 bucket_high: float | None, unit: str = "F",
                                 market_date: str = None, city: str = None, **kwargs) -> float:
    """Computes a conservative (lower) probability by inflating sigma."""
    sigma_mult = kwargs.get('sigma_mult', LOWER_SIGMA_FACTOR)
    cons_kwargs = kwargs.copy()
    if 'model_uncertainty_deg' in cons_kwargs and cons_kwargs['model_uncertainty_deg'] is not None:
        cons_kwargs['model_uncertainty_deg'] *= sigma_mult
    else:
        # Will trigger dynamic with multiplied sigma in _get_params if we extend, but for now override
        cons_kwargs['model_uncertainty_deg'] = None
    # Call with higher uncertainty (lower prob for extreme buckets, more realistic for defense)
    return forecast_probability(forecast_temp, bucket_low, bucket_high, unit=unit,
                                market_date=market_date, city=city, **cons_kwargs)

def get_lower_edge(forecast_prob: float, market_price: float, forecast_temp: float = None,
                   bucket_low: float | None = None, bucket_high: float | None = None,
                   market_date: str = None, city: str = None, **kwargs) -> float:
    """Computes lower-bound edge using conservative probability."""
    if forecast_temp is None or bucket_low is None or market_price is None:
        return forecast_prob - market_price  # fallback to point estimate
    cons_prob = get_conservative_probability(forecast_temp, bucket_low, bucket_high,
                                             market_date=market_date, city=city, **kwargs)
    return cons_prob - market_price

def should_trade_lower_edge(edge: float, forecast_prob: float = None, forecast_temp: float = None,
                            bucket_low: float | None = None, bucket_high: float | None = None,
                            market_price: float = None, market_date: str = None,
                            city: str = None, use_lower_only: bool = True) -> bool:
    """
    Enhanced should_trade that ONLY engages on lower edge cases.
    Requires the conservative lower-edge to still be positive and above threshold.
    This is the core of the new defensive system.
    """
    if not use_lower_only:
        return should_trade(edge, forecast_prob, market_date)

    # First pass normal check
    if not should_trade(edge, forecast_prob, market_date):
        return False

    # Lower edge filter - only engage if conservative estimate still has edge
    if forecast_temp is not None and bucket_low is not None and market_price is not None:
        lower_e = get_lower_edge(forecast_prob or 0.5, market_price, forecast_temp,
                                 bucket_low, bucket_high, market_date, city)
        if lower_e < LOWER_EDGE_THRESHOLD:
            logger.info(f"LOWER_EDGE_FILTER: rejected (lower_e={lower_e:+.1%} < {LOWER_EDGE_THRESHOLD:.1%})")
            return False
        logger.debug(f"Lower edge passed: {lower_e:+.1%}")
    return True


def kelly_position_size(bankroll: float, edge: float, win_prob: float,
                        market_price: float = 0.5) -> float:
    """
    Calculates position size using fractional Kelly criterion.

    Kelly fraction = (b*p - q) / b
      where b = (1/market_price - 1)  <- payout odds from market price
            p = win_prob              <- our forecast probability
            q = 1 - win_prob

    market_price must be passed for correct odds calculation.
    Using win_prob for b is a common mistake that collapses kelly to ~0.

    Returns USDC amount to spend, clamped to [MIN_POSITION_USDC, MAX_POSITION_USDC].
    Returns 0 if kelly is negative (bet has no edge) or inputs are degenerate.
    """
    if win_prob <= 0.0 or win_prob >= 1.0 or bankroll <= 0.0:
        return 0.0
    if market_price <= 0.0 or market_price >= 1.0:
        return 0.0

    lose_prob = 1.0 - win_prob

    # b = net payout odds: how much you win per dollar risked if correct
    # At market_price=0.50 you risk $0.50 to win $1.00, so b=1.0 (even money)
    b = (1.0 / market_price) - 1.0
    if b <= 0.0:
        return 0.0

    kelly_full = (b * win_prob - lose_prob) / b
    if kelly_full <= 0.0:
        logger.debug(f"Negative Kelly ({kelly_full:.3f}) at prob={win_prob:.3f} price={market_price:.3f}, skipping")
        return 0.0

    position = bankroll * kelly_full * KELLY_FRACTION

    # Clamp to allowed range
    position = max(MIN_POSITION_USDC, min(MAX_POSITION_USDC, position))
    return round(position, 2)


def convert_forecast_to_market_unit(forecast_celsius: float, market_unit: str) -> float:
    """Converts Open-Meteo Celsius forecast to the unit used by the market."""
    if market_unit.upper() == "F":
        return celsius_to_fahrenheit(forecast_celsius)
    return forecast_celsius  # Already Celsius


if __name__ == "__main__":
    from datetime import timedelta

    print("Strategy module test - Dynamic probability floor")
    print("=" * 60)

    # Show floor values
    print("\nProbability floors by horizon:")
    for d in range(7):
        dt = (date.today() + timedelta(days=d)).isoformat()
        print(f"  Day {d} ({dt}): floor = {get_prob_floor(dt):.0%}")

    # Test scenarios with different horizons
    tests = [
        # (label, forecast, low, high, market_price, days_ahead, description)
        ("Same-day centered", 72.0, 71.0, 73.0, 0.10, 0,
         "Same-day, centered: prob ~38%, floor=30% => TRADE"),
        ("Same-day adjacent", 72.0, 69.0, 71.0, 0.05, 0,
         "Same-day, adjacent bucket: prob ~17%, floor=30% => NO TRADE"),
        ("1-day centered", 72.0, 71.0, 73.0, 0.10, 1,
         "1-day, centered: prob ~30%, floor=20% => TRADE"),
        ("1-day far bucket", 72.0, 66.0, 68.0, 0.02, 1,
         "1-day, far bucket: prob ~5%, floor=20% => NO TRADE (the trap)"),
        ("3-day centered", 72.0, 71.0, 73.0, 0.08, 3,
         "3-day, centered: prob ~16%, floor=15% => TRADE"),
        ("3-day adjacent", 72.0, 69.0, 71.0, 0.03, 3,
         "3-day, adjacent: prob ~14%, floor=15% => NO TRADE"),
        ("Open-ended high", 65.0, 60.0, None, 0.40, 1,
         "1-day, open-ended above: prob ~90%, floor=20% => TRADE"),
    ]

    for label, fc, lo, hi, mp, days, desc in tests:
        dt = (date.today() + timedelta(days=days)).isoformat()
        prob = forecast_probability(fc, lo, hi, unit="F", market_date=dt)
        edge = find_edge(prob, mp)
        floor = get_prob_floor(dt)
        trade = should_trade(edge, forecast_prob=prob, market_date=dt)
        size = kelly_position_size(bankroll=200.0, edge=edge, win_prob=prob, market_price=mp)

        print(f"\n--- {label} (day {days}) ---")
        print(f"  {desc}")
        print(f"  Forecast: {fc}F | Bucket: [{lo}, {hi}] | Date: {dt}")
        print(f"  Prob: {prob:.1%} | Market: {mp:.1%} | Edge: {edge:+.1%}")
        print(f"  Floor: {floor:.0%} | Floor pass: {prob >= floor} | Edge pass: {edge >= ENTRY_THRESHOLD}")
        print(f"  TRADE: {trade} | Size: ${size:.2f}")

    print("\n" + "=" * 60)
    print("Dynamic probability floor test complete.")


# -----------------------------------------------------------------------
# Bayesian METAR update for same-day markets
# Uses conditional probability: P(max > X | observed_temp = Y, time = T)
#
# Key insight: On a same-day market, live METAR observations dramatically
# narrow uncertainty. If it's 2pm and current temp is 28°C:
#   - The daily max MUST be >= 28°C (it's already been observed)
#   - Remaining heating potential depends on hours until peak (~3-4pm local)
#   - After peak, the observed max IS the daily max (sigma -> tiny)
#
# This replaces the crude "if METAR > forecast, use METAR" override with
# a proper Bayesian posterior distribution.
# -----------------------------------------------------------------------

# Hours after local midnight when peak heating typically occurs.
# Most cities peak between 2-4pm local; 15.0 (3pm) is a solid default.
PEAK_HEATING_HOUR = 15.0

# After this many hours past peak, we treat observed max as final.
# 3 hours past peak (6pm) = essentially no more heating expected.
POST_PEAK_WINDOW_H = 3.0

# Minimum sigma (degrees F) even with perfect observation timing.
# Prevents overconfidence from rounding / station-to-station variance.
# RAISED 2026-05-04: was 0.8°F (0.44°C) — absurdly narrow, produced 95% probabilities
# for 1°C buckets that hit at 23%. Now 2.5°F (1.39°C) — max P(bucket) ≈ 28%.
# Boss directive: 2.5°F compromise between safety (3.0) and signal (2.0).
MIN_SAME_DAY_SIGMA_F = 2.5


def _estimate_remaining_heating(
    current_temp_f: float,
    forecast_max_f: float,
    local_hour: float,
) -> tuple[float, float]:
    """
    Estimates the expected additional heating and reduced sigma based on
    current observation time and temperature.

    Returns:
        (adjusted_forecast_f, adjusted_sigma_f)

    The adjusted forecast is a blend of the observed running max and the
    original forecast, weighted by how much heating potential remains.

    As local_hour approaches PEAK_HEATING_HOUR:
      - If temp is already near/above forecast, forecast shifts UP
      - Sigma shrinks because less time = less variability

    After PEAK_HEATING_HOUR + POST_PEAK_WINDOW_H:
      - Observed max IS the daily max
      - Sigma is minimal (just measurement uncertainty)
    """
    hours_to_peak = max(0.0, PEAK_HEATING_HOUR - local_hour)
    hours_past_peak = max(0.0, local_hour - PEAK_HEATING_HOUR)

    # time_factor: 1.0 = full day ahead, 0.0 = past peak window
    if hours_past_peak >= POST_PEAK_WINDOW_H:
        # Well past peak: observed max is essentially the daily max
        time_factor = 0.0
    elif local_hour >= PEAK_HEATING_HOUR:
        # Past peak but within window: rapidly decaying uncertainty
        time_factor = max(0.0, 1.0 - hours_past_peak / POST_PEAK_WINDOW_H)
        time_factor *= 0.15  # Only 15% uncertainty even right at peak
    else:
        # Before peak: uncertainty proportional to hours remaining
        # Morning (8am, 7h to peak) = high uncertainty
        # Early afternoon (1pm, 2h to peak) = much less
        time_factor = min(1.0, hours_to_peak / 8.0)

    # Adjusted forecast: blend observed max with original forecast
    # If current temp already exceeds forecast, the forecast shifts up
    if current_temp_f >= forecast_max_f:
        # Already exceeded forecast — daily max will be at least current
        # Small additional heating still possible if before peak
        additional_heating = time_factor * 2.0  # Up to 2°F more if early
        adjusted_forecast = current_temp_f + additional_heating
    else:
        # Below forecast: blend between observed and forecast
        # Weight toward observed as we approach peak
        obs_weight = 1.0 - time_factor
        adjusted_forecast = (
            obs_weight * max(current_temp_f, forecast_max_f * 0.95)
            + time_factor * forecast_max_f
        )
        # But never go below observed max
        adjusted_forecast = max(adjusted_forecast, current_temp_f)

    # Adjusted sigma: shrinks as observation time approaches/passes peak
    base_sigma = SIGMA_BY_HORIZON_F[0]  # Same-day base: 2.0°F
    adjusted_sigma = base_sigma * time_factor
    adjusted_sigma = max(adjusted_sigma, MIN_SAME_DAY_SIGMA_F)

    return adjusted_forecast, adjusted_sigma


def bayesian_metar_probability(
    forecast_temp: float,
    observed_temp: float,
    local_hour: float,
    bucket_low: float | None,
    bucket_high: float | None,
    unit: str = "F",
    market_date: str = None,
) -> float:
    """
    Computes P(daily_max in bucket | observed_temp, local_hour) using
    Bayesian-style conditional probability with METAR observations.

    This replaces the simple override used previously. The key improvements:
    1. Sigma shrinks dynamically based on time of day
    2. Forecast shifts toward observed temp as certainty increases
    3. After peak heating, observed max IS the daily max (near-deterministic)
    4. Handles the case where observed temp already exceeds bucket bounds

    Args:
        forecast_temp:  Original model forecast (blended, in market unit)
        observed_temp:  Current observed running max from METAR (same unit)
        local_hour:     Local time as decimal hours (e.g. 14.5 = 2:30pm)
        bucket_low:     Lower bucket bound (None = -inf)
        bucket_high:    Upper bucket bound (None = +inf)
        unit:           "F" or "C"
        market_date:    ISO date string for the market

    Returns:
        Updated probability in [0.001, 0.999]
    """
    # Convert to Fahrenheit for internal calculation
    if unit.upper() == "C":
        forecast_f = celsius_to_fahrenheit(forecast_temp)
        observed_f = celsius_to_fahrenheit(observed_temp)
        bucket_low_f = celsius_to_fahrenheit(bucket_low) if bucket_low is not None else None
        bucket_high_f = celsius_to_fahrenheit(bucket_high) if bucket_high is not None else None
    else:
        forecast_f = forecast_temp
        observed_f = observed_temp
        bucket_low_f = bucket_low
        bucket_high_f = bucket_high

    # Get adjusted forecast center and sigma from observation
    adj_forecast_f, adj_sigma_f = _estimate_remaining_heating(
        current_temp_f=observed_f,
        forecast_max_f=forecast_f,
        local_hour=local_hour,
    )

    # Convert back to market unit for probability calculation
    if unit.upper() == "C":
        adj_forecast = fahrenheit_to_celsius(adj_forecast_f)
        adj_sigma = adj_sigma_f / 1.8
    else:
        adj_forecast = adj_forecast_f
        adj_sigma = adj_sigma_f

    # Hard constraint: daily max >= observed max (it already happened)
    # This is the key Bayesian insight — truncate the distribution below observed
    df = DF_BY_HORIZON.get(0, 20.0)  # Same-day df

    # P(bucket | max >= observed) = P(bucket AND max >= observed) / P(max >= observed)
    # Since we know max >= observed, the distribution is truncated at observed_temp

    # Compute raw CDF values
    mu = adj_forecast
    sigma = adj_sigma

    low_cdf = t_dist.cdf(bucket_low, df, loc=mu, scale=sigma) if bucket_low is not None else 0.0
    high_cdf = t_dist.cdf(bucket_high, df, loc=mu, scale=sigma) if bucket_high is not None else 1.0
    obs_cdf = t_dist.cdf(observed_temp, df, loc=mu, scale=sigma)

    # Truncated probability: P(low < X < high | X >= observed)
    # = P(max(low, observed) < X < high) / P(X >= observed)
    effective_low_cdf = max(low_cdf, obs_cdf)
    denominator = 1.0 - obs_cdf

    if denominator < 0.001:
        # Almost all mass is below observed temp — distribution is poorly
        # calibrated. Fall back: if observed is in bucket, prob ≈ 1.
        if bucket_high is None or observed_temp < bucket_high:
            if bucket_low is None or observed_temp >= bucket_low:
                return 0.95  # Very likely — observed is in bucket and past peak
        return 0.05

    if bucket_high is not None and observed_temp >= bucket_high:
        # Observed max already blew past the bucket ceiling.
        # Daily max will be >= observed, so this bucket loses.
        return 0.001

    truncated_prob = (high_cdf - effective_low_cdf) / denominator
    return max(0.001, min(0.999, truncated_prob))
 
# Lower Edge Defensive System (from risk de-risking patch - exported for bot/sniper)
LOWER_EDGE_ONLY = True
LOWER_EDGE_THRESHOLD = 0.10  # Only trade if conservative lower-edge still > this
LOWER_SIGMA_FACTOR = 1.35    # Inflates sigma for pessimistic prob in lower-edge mode
def wu_normal_probability(forecast_temp: float, bucket_low: float | None,
                           bucket_high: float | None, unit: str = "F",
                           city: str = None,
                           market_date: str = None,
                           market_type: str = "highest",
                           forecast_bias: float = 0.0) -> float:
    """
    WU-specific probability using Normal distribution + WU-specific sigma.

    Unlike forecast_probability (t-distribution, df=12), WU forecasts
    have a narrower error distribution that is well-approximated by a
    Normal. Uses per-city sigma from WU_CITY_SIGMA_F when available,
    or falls back to 2x the standard horizon-based sigma per Option A.

    Args:
        forecast_temp: WU forecast temp (in market unit)
        bucket_low:    lower bound of bucket (None = -infinity)
        bucket_high:   upper bound of bucket (None = +infinity)
        unit:          "F" or "C"
        city:          city name for per-city sigma lookup
        market_date:   ISO date string (for horizon-based fallback sigma)
        market_type:   'highest' or 'lowest'
        forecast_bias: bias offset to apply to forecast

    Returns:
        probability float in [0.001, 0.999]
    """
    # Compute horizon (days ahead) for sigma scaling
    try:
        from datetime import datetime as _dt, date as _date
        _mdate = _dt.strptime(market_date, "%Y-%m-%d").date() if market_date else None
        _days = max(0, (_mdate - _date.today()).days) if _mdate else 0
    except Exception:
        _days = 0
    _horizon_scale = WU_HORIZON_SCALE.get(_days, WU_HORIZON_SCALE_DEFAULT)

    if city and city in WU_CITY_SIGMA_F:
        # Table value is the same-day sigma — scale up for future horizons
        sigma_f = WU_CITY_SIGMA_F[city] * _horizon_scale
    else:
        # Fallback: use _get_params (horizon-based) then double per Option A
        if market_date is not None:
            sigma_f, _ = _get_params(market_date, unit, city=city, market_type=market_type)
        else:
            sigma_f = 5.5 if unit.upper() == "F" else 5.5 / 1.8
        sigma_f *= 2.0  # Option A: double WU sigma
        # Apply horizon scale on top of doubling for additional conservatism
        sigma_f *= _horizon_scale

    if unit.upper() == "C":
        sigma = sigma_f / 1.8
    else:
        sigma = sigma_f

    mu = forecast_temp + forecast_bias

    low_p  = norm_dist.cdf(bucket_low,  loc=mu, scale=sigma) if bucket_low  is not None else 0.0
    high_p = norm_dist.cdf(bucket_high, loc=mu, scale=sigma) if bucket_high is not None else 1.0

    prob = high_p - low_p
    return max(0.001, min(0.999, prob))


def wu_empirical_or_normal_probability(
    forecast_temp: float, bucket_low: float | None,
    bucket_high: float | None, unit: str = "F",
    city: str = None,
    market_date: str = None,
    market_type: str = "highest",
    forecast_bias: float = 0.0) -> float:
    """
    WU probability: try empirical table first, fallback to Normal.

    When a city has 30+ historical WU resolves for the same buffer band,
    uses the empirical hit rate directly (distribution-free). Otherwise
    falls back to wu_normal_probability().

    This is the canonical entry point for WU trades after Option C+B.
    """
    # Convert forecast and bucket to Celsius for API consistency
    fc_c = forecast_temp if unit.upper() == "C" else (forecast_temp - 32.0) / 1.8
    bl_c = bucket_low if unit.upper() == "C" else (bucket_low - 32.0) / 1.8 if bucket_low is not None else None
    bh_c = bucket_high if unit.upper() == "C" else (bucket_high - 32.0) / 1.8 if bucket_high is not None else None

    # Try empirical table
    try:
        from wu_empirical import get_empirical_prob
        emp_prob = get_empirical_prob(
            city, market_type, fc_c, bl_c, bh_c
        )
        if emp_prob is not None:
            logger.info(
                f"WU EMPIRICAL: {city} {market_date} "
                f"buf={bl_c:.1f}-{bh_c or 'inf'}°C "
                f"empirical={emp_prob:.1%}"
            )
            return max(0.001, min(0.999, emp_prob))
    except Exception as e:
        logger.debug(f"WU empirical lookup failed (falling back to Normal): {e}")

    # Fallback to Normal
    return wu_normal_probability(
        forecast_temp, bucket_low, bucket_high,
        unit=unit, city=city,
        market_date=market_date,
        market_type=market_type,
        forecast_bias=forecast_bias,
    )
