#!/bin/bash
# Weatherbot watchdog — runs every 5 min via cron.
# Loads TELEGRAM_TOKEN and TELEGRAM_CHAT_ID from the bot's .env so the
# token is not hardcoded in the repo. The committed copy here is the
# sanitized template; the deployed copy at /root/watchdog.sh sources
# the .env identically.
set -a
. /root/weatherbot/.env 2>/dev/null
set +a
SERVICE="weatherbot"
TELEGRAM_TOKEN="${TELEGRAM_TOKEN:-MISSING_TELEGRAM_TOKEN}"
CHAT_ID="${TELEGRAM_CHAT_ID:-MISSING_TELEGRAM_CHAT_ID}"
LOG="/var/log/watchdog.log"

if ! systemctl is-active --quiet "$SERVICE"; then
    TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M UTC')
    echo "[$TIMESTAMP] $SERVICE was down — restarting" >> "$LOG"

    systemctl start "$SERVICE"
    sleep 10

    if systemctl is-active --quiet "$SERVICE"; then
        STATUS="✅ Restarted successfully"
    else
        STATUS="❌ Still down after restart attempt"
    fi

    echo "[$TIMESTAMP] $STATUS" >> "$LOG"

    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage"       -H "Content-Type: application/json"       -d "{\"chat_id\":\"${CHAT_ID}\",\"text\":\"⚠️ <b>[Watchdog] ${SERVICE} was DOWN</b>\n${STATUS}\n${TIMESTAMP}\",\"parse_mode\":\"HTML\"}"       >> "$LOG" 2>&1
fi
