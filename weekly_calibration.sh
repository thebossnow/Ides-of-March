#!/bin/bash
# weekly_calibration.sh — Sunday 04:30 UTC weekly calibration run
#
# 1. Backfills observed temperatures for newly-resolved markets
# 2. Runs full sigma_calibration_v2 on the updated dataset
# 3. Saves dated outputs so we can track cohort stability week-over-week
#
# Output: /root/weatherbot/logs/calibration_weekly_YYYY-MM-DD.log
# Data:   /root/weatherbot/calibration_history/calibration_YYYY-MM-DD.json
#
# Added 2026-06-08 — needed to track when cohort calibration becomes
# stable enough (≥ 30 days clean data) to trust strategy changes.

set -u
set -o pipefail

DATE=$(date -u +%Y-%m-%d)
START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
LOG_DIR=/root/weatherbot/logs
DATA_DIR=/root/weatherbot/calibration_history
OUT_LOG=$LOG_DIR/calibration_weekly_$DATE.log
JSON_OUT=$DATA_DIR/calibration_$DATE.json

mkdir -p "$LOG_DIR" "$DATA_DIR"

cd /root/weatherbot

{
  echo "════════════════════════════════════════════════════════════"
  echo "Weekly calibration run — started $START_TS"
  echo "════════════════════════════════════════════════════════════"
  echo ""
  echo "── Step 1/2: Backfill ground truth (catch newly resolved markets) ──"
  echo ""
  if ./venv/bin/python3 backfill_ground_truth.py; then
    echo ""
    echo "✓ Backfill OK"
  else
    echo ""
    echo "✗ Backfill FAILED — continuing to calibration with existing data"
  fi

  echo ""
  echo "── Step 2/2: Run sigma_calibration_v2 ──"
  echo ""
  if ./venv/bin/python3 sigma_calibration_v2.py --save "$JSON_OUT"; then
    echo ""
    echo "✓ Calibration OK — JSON saved to $JSON_OUT"
  else
    echo ""
    echo "✗ Calibration FAILED"
    exit 1
  fi

  echo ""
  echo "════════════════════════════════════════════════════════════"
  echo "Weekly calibration run — finished $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "════════════════════════════════════════════════════════════"
} >> "$OUT_LOG" 2>&1
