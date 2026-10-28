#!/usr/bin/env bash
#
# ops/redeploy.sh — Redeploy the bot dir from the merged git master, making
# git the source of truth and preserving the live campaign's runtime state.
#
# WHY THIS EXISTS: the VPS was previously updated by rsync from personal
# clones, so "what's committed" and "what's running" kept drifting (the
# stale /root/Ides-of-March checkout was one symptom). This script replaces
# that: it fast-forwards the deploy dir to origin/master WITHOUT touching the
# gitignored runtime files (.env, positions.db, venv, logs), so the running
# code is always exactly what's in git.
#
# It NEVER unlocks live trading. DRY_RUN stays whatever .env says (must be
# true during Stage B), and executor.py's certificate check is untouched.
#
# Run ON THE VPS as root:
#   /root/weatherbot/ops/redeploy.sh
# Override defaults via env: BOT_DIR, REMOTE, BRANCH, SERVICE.
#
set -euo pipefail

BOT_DIR="${BOT_DIR:-/root/weatherbot}"
REMOTE="${REMOTE:-https://github.com/thebossnow/Ides-of-March.git}"
BRANCH="${BRANCH:-master}"
SERVICE="${SERVICE:-weatherbot}"
TS="$(date -u +%Y%m%d-%H%M%S)"

echo "== Ides redeploy $TS =="
echo "   bot_dir=$BOT_DIR  branch=$BRANCH  service=$SERVICE"

# 0. Preconditions — refuse if this doesn't look like the real deploy dir.
command -v git >/dev/null || { echo "ERROR: git not installed"; exit 1; }
if [ ! -f "$BOT_DIR/.env" ]; then
    echo "REFUSING: $BOT_DIR/.env not found — wrong directory? (won't risk a bad deploy)"
    exit 1
fi

# 1. Stop the service so SQLite checkpoints its WAL and releases positions.db.
systemctl stop "$SERVICE" 2>/dev/null || echo "   (service not running / not managed — continuing)"

# 2. Safety backup of the irreplaceable runtime state.
BACKUP="/root/weatherbot-state.bak.$TS"
mkdir -p "$BACKUP"
cp -a "$BOT_DIR/.env"          "$BACKUP/" 2>/dev/null || true
cp -a "$BOT_DIR"/positions.db* "$BACKUP/" 2>/dev/null || true
echo "   runtime state backed up to $BACKUP"

# 3. Fast-forward TRACKED code to origin/$BRANCH.
#    reset --hard only rewrites tracked files. .env, *.db, venv/, *.log, *.csv
#    are gitignored and NOT in the master tree, so they are left untouched.
#    We deliberately DO NOT run `git clean` — it would delete exactly those
#    untracked runtime files we must preserve.
cd "$BOT_DIR"
if [ ! -d .git ]; then
    echo "   $BOT_DIR is not a git checkout — initialising against $REMOTE"
    git init -q
    git remote add origin "$REMOTE" 2>/dev/null || git remote set-url origin "$REMOTE"
fi
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"
echo "   code now at $(git rev-parse --short HEAD): $(git log -1 --format=%s)"

# 4. Refresh dependencies (master now declares cachetools + httpx).
if [ -x "$BOT_DIR/venv/bin/pip" ]; then
    "$BOT_DIR/venv/bin/pip" install -q -r requirements.txt && echo "   dependencies refreshed"
else
    echo "   WARNING: no venv at $BOT_DIR/venv — create it and run: pip install -r requirements.txt"
fi

# 5. Pre-flight checks BEFORE the service starts.
PY="$BOT_DIR/venv/bin/python3"; [ -x "$PY" ] || PY=python3
"$PY" -c "import trade_gates, validation_gate; print('   new modules import OK')"
grep -q "from trade_gates import" bot.py \
    && echo "   bot.py has the sanity gates" \
    || { echo "   ERROR: bot.py is missing the gates — aborting before restart"; exit 1; }
if grep -qiE '^DRY_RUN=true' .env; then
    echo "   .env DRY_RUN=true (paper mode — correct for Stage B)"
else
    echo "   *** WARNING: DRY_RUN is not true in .env — Stage B must stay in paper mode ***"
fi
echo -n "   lock status: "; "$PY" validation_gate.py --check || true   # expect LOCKED

# 6. Install the corrected systemd unit and restart.
install -m 0644 "$BOT_DIR/ops/weatherbot.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl start "$SERVICE"
sleep 3
systemctl show "$SERVICE" -p ActiveState -p WorkingDirectory -p ExecStart

# 7. Ensure the weekly validation cron exists (idempotent).
CRON_LINE="0 5 * * 0 $BOT_DIR/ops/validation_weekly_check.sh >> /var/log/validation_check.log 2>&1"
if ! crontab -l 2>/dev/null | grep -qF "validation_weekly_check.sh"; then
    ( crontab -l 2>/dev/null; echo "$CRON_LINE" ) | crontab -
    echo "   weekly validation cron installed"
else
    echo "   weekly validation cron already present"
fi

echo "== redeploy complete — campaign resumes in DRY_RUN; live trading remains locked =="
echo "   old stale checkout (if any) at /root/Ides-of-March can now be removed."
