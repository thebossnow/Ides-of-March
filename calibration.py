"""
calibration.py — Probability calibration for weather bot model.

STATUS: MASSIVELY OVERCONFIDENT — predicts 45.7% avg, actual hit rate 23.2%.
BOSS DIRECTIVE: Err UNDERconfident. Real money at stake.

Calibration curve (69 resolved positions, 13 wins):
  Bin:      0-20%  20-30%  30-40%  40-50%  50-60%  60-70%  70-80%  80-90%  90-100%
  Count:       3      10      24      10       7       3       9       1        2
  Hit%:        0%     50%    4.2%     20%   14.3%      0%   33.3%      0%      50%

KEY INSIGHT: The 20-30% bin is UNDERconfident (50% hit vs 25% predicted).
             Everything above 30% is OVERconfident — severely.
             We preserve 20-30% signals, crush everything else.

APPROACH: Bin-based calibration with minimum sample smoothing.
          - Bins with ≥5 samples: use empirical hit rate
          - Sparse bins: blend toward base rate (conservative)
          - Hard cap at 40% — nothing above has reliable calibration

Usage:
    from calibration import calibrate_probability, calibrate_edge

    raw_prob = bayesian_metar_probability(...)
    calibrated = calibrate_probability(raw_prob)
"""

import logging
import bisect

logger = logging.getLogger(__name__)

# ── Calibration bins [lower_bound, upper_bound) → calibrated_prob ──
# Format: (upper_bound, calibrated_prob, sample_count)
# Only bins with ≥5 samples use empirical hit rate; others interpolate
_BINS = [
    (0.20, 0.232, 3),    # 0-20%: sparse, use base rate
    (0.30, 0.500, 10),   # 20-30%: UNDERCONFIDENT — 50% hit rate! (PRESERVE)
    (0.40, 0.042, 24),   # 30-40%: MASSIVELY overconfident — crush to 4.2%
    (0.50, 0.200, 10),   # 40-50%: overconfident → 20%
    (0.60, 0.143, 7),    # 50-60%: overconfident → 14.3%
    (0.70, 0.232, 3),    # 60-70%: sparse → base rate
    (0.80, 0.250, 9),    # 70-80%: overconfident → 25% (blend toward base from 33% raw)
    (1.01, 0.232, 3),    # 80-100%: sparse → base rate
]

# Extract bin edges for bisect
_BIN_EDGES = [b[0] for b in _BINS]
_BIN_VALUES = [b[1] for b in _BINS]
_BIN_COUNTS = [b[2] for b in _BINS]

# Safety caps
MAX_CALIBRATED_PROB = 0.40
MIN_CALIBRATED_PROB = 0.01
BASE_RATE = 0.232

# Edge safety margin — subtracted from calibrated edge before threshold check
EDGE_SAFETY_MARGIN = 0.03


def calibrate_probability(raw_prob: float) -> float:
    """
    Bin-based calibration using empirical hit rates.
    
    Bins with ≥5 samples use their empirical hit rate directly.
    Sparse bins blend toward BASE_RATE weighted by sample count.
    """
    if raw_prob <= 0.0:
        return MIN_CALIBRATED_PROB
    if raw_prob >= 1.0:
        return MAX_CALIBRATED_PROB

    idx = bisect.bisect_left(_BIN_EDGES, raw_prob)
    if idx >= len(_BINS):
        idx = len(_BINS) - 1

    empirical = _BIN_VALUES[idx]
    count = _BIN_COUNTS[idx]

    if count >= 5:
        # Reliable bin — use empirical hit rate directly
        calibrated = empirical
    else:
        # Sparse bin — blend empirical toward base rate
        # Weight: more samples = more trust in empirical
        weight = min(1.0, count / 10.0)  # 0→0, 5→0.5, 10→1.0
        calibrated = weight * empirical + (1.0 - weight) * BASE_RATE

    calibrated = max(MIN_CALIBRATED_PROB, min(MAX_CALIBRATED_PROB, calibrated))

    if abs(calibrated - raw_prob) > 0.15:
        logger.info(
            f"CALIBRATION: raw={raw_prob:.1%} → cal={calibrated:.1%} "
            f"(bin {_BIN_EDGES[idx-1] if idx>0 else 0:.0%}-{_BIN_EDGES[idx]:.0%}, "
            f"n={count}, empirical={empirical:.1%})"
        )

    return calibrated


def calibrate_edge(raw_prob: float, market_prob: float) -> float:
    """Compute calibrated edge with safety margin."""
    cal_prob = calibrate_probability(raw_prob)
    return cal_prob - market_prob - EDGE_SAFETY_MARGIN


def should_trade(raw_prob: float, market_prob: float, 
                 entry_threshold: float = 0.10) -> tuple[bool, float, str]:
    """Full calibration gate."""
    cal_prob = calibrate_probability(raw_prob)
    cal_edge = cal_prob - market_prob - EDGE_SAFETY_MARGIN

    if cal_edge < entry_threshold:
        return False, cal_edge, (
            f"cal_edge={cal_edge:+.1%} < {entry_threshold:.1%} "
            f"(raw={raw_prob:.1%}→cal={cal_prob:.1%}, mkt={market_prob:.1%})"
        )
    return True, cal_edge, "ok"


# ── Self-test ────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("CALIBRATION — BIN-BASED (UNDERCONFIDENT)")
    print(f"MAX_CAL={MAX_CALIBRATED_PROB:.0%}  SAFETY={EDGE_SAFETY_MARGIN:.0%}  BASE={BASE_RATE:.1%}")
    print()
    header = f"{'Raw':>7} → {'Cal':>7}  {'Δ':>8}  {'Mkt=25%':>10}  {'Trade?':>8}"
    print(header)
    print("-" * len(header))

    for raw in [0.10, 0.15, 0.22, 0.28, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95]:
        cal = calibrate_probability(raw)
        delta = cal - raw
        trade, edge, reason = should_trade(raw, 0.25)
        print(f"{raw:>6.0%} → {cal:>6.0%}  {delta:>+7.0%}  "
              f"{edge:>+9.1%}  {'✅' if trade else '❌':>6}  {reason if not trade else ''}")

    print()
    print("BINS: 20-30%=50%(PRESERVED) | 30-40%=4.2%(CRUSHED) | 70-80%=25%(CAPPED)")
    print("Only the 20-30% sweet spot generates positive edge at typical market prices.")
