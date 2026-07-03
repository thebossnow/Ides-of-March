#!/usr/bin/env bash
#
# validation_weekly_check.sh — Stage B paper-campaign progress + lock status.
#
# Reports, without ever unlocking anything:
#   1. Current live-trading lock status (validation_gate.py --check).
#   2. How many RESOLVED paper trades have accumulated since the Stage B
#      cutoff (default 2026-07-02), vs the n>=100 target.
#   3. When the target is reached, runs the full make-or-break evaluation
#      with the same --since cutoff and reports pass/fail. A certificate is
#      only issued by validation_gate.py itself, on a genuine pass.
#
# This is a read-only monitor. It never sets DRY_RUN=false and never edits
# the certificate. The executor lockout remains the sole gate to live trading.
#
# Cron (Sundays 05:00 UTC):
#   0 5 * * 0 /root/weatherbot/ops/validation_weekly_check.sh >> /var/log/validation_check.log 2>&1
#
# Config via env (all optional):
#   BOT_DIR             bot directory (default: parent of this script)
#   SINCE               Stage B cutoff, ISO date (default: 2026-07-02)
#   TARGET_N            resolved-trade target (default: 100)
#   POSITIONS_DB_PATH   DB path (default: $BOT_DIR/positions.db)
#   TELEGRAM_TOKEN / TELEGRAM_CHAT_ID   if both set, a summary is sent
#
set -euo pipefail

# ── Resolve paths ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="${BOT_DIR:-$(dirname "$SCRIPT_DIR")}"
SINCE="${SINCE:-2026-07-02}"
TARGET_N="${TARGET_N:-100}"
DB_PATH="${POSITIONS_DB_PATH:-$BOT_DIR/positions.db}"

cd "$BOT_DIR"

# Prefer the project venv if present (matches weatherbot.service).
if [[ -x "$BOT_DIR/venv/bin/python3" ]]; then
    PY="$BOT_DIR/venv/bin/python3"
else
    PY="python3"
fi

STAMP="$(date -u '+%Y-%m-%d %H:%M:%SZ')"
echo "=================================================================="
echo "Ides-of-March validation check — $STAMP"
echo "  bot_dir=$BOT_DIR  db=$DB_PATH  since=$SINCE  target=$TARGET_N"
echo "=================================================================="

# ── 1. Lock status ───────────────────────────────────────────────────────
# --check exits non-zero when locked; that is expected during Stage B, so
# don't let `set -e` abort the script.
LOCK_STATUS="$("$PY" validation_gate.py --check 2>&1 || true)"
echo "$LOCK_STATUS"

# ── 2. Resolved-trade progress since the cutoff ──────────────────────────
# entry_time excludes the pre-fix June live trades so they cannot inflate
# the paper record (see VALIDATION_PLAN.md, Stage B data hygiene).
RESOLVED_SINCE="$(
    "$PY" - "$DB_PATH" "$SINCE" <<'PYEOF'
import sqlite3, sys
db, since = sys.argv[1], sys.argv[2]
try:
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM positions "
        "WHERE status IN ('resolved_won','resolved_lost') AND entry_time >= ?",
        (since,),
    ).fetchone()[0]
    print(n)
except Exception as e:
    print(f"ERR:{e}")
PYEOF
)"

if [[ "$RESOLVED_SINCE" == ERR:* ]]; then
    echo "Progress: could not read DB (${RESOLVED_SINCE#ERR:})"
    SUMMARY="⚠️ Ides validation: DB read error — ${RESOLVED_SINCE#ERR:}"
    RESOLVED_SINCE=-1
else
    echo "Progress: $RESOLVED_SINCE / $TARGET_N resolved paper trades since $SINCE"
    SUMMARY="Ides Stage B: $RESOLVED_SINCE/$TARGET_N resolved since $SINCE — ${LOCK_STATUS}"
fi

# ── 3. Full evaluation once the target is reached ────────────────────────
if [[ "$RESOLVED_SINCE" -ge "$TARGET_N" ]]; then
    echo "------------------------------------------------------------------"
    echo "Target reached — running full make-or-break evaluation:"
    if "$PY" validation_gate.py --evaluate "$DB_PATH" --since "$SINCE"; then
        echo "RESULT: PASS — certificate issued. Live trading MAY be enabled at min size."
        SUMMARY="✅ Ides Stage B PASSED ($RESOLVED_SINCE trades) — certificate issued."
    else
        echo "RESULT: FAIL — live trading stays locked. No demonstrated edge."
        SUMMARY="❌ Ides Stage B evaluation FAILED ($RESOLVED_SINCE trades) — still locked."
    fi
fi

# ── Optional Telegram summary ────────────────────────────────────────────
if [[ -n "${TELEGRAM_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -sS --max-time 15 \
        "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${SUMMARY}" >/dev/null 2>&1 \
        && echo "Telegram summary sent." \
        || echo "Telegram summary failed (non-fatal)."
fi

echo "Done."
