#!/bin/bash
# backup.sh — daily encrypted snapshot of irreplaceable state.
# Runs from cron at 03:00 UTC.
#
# Contents: positions.db + .env + calibration_history/
# Output:   /root/backups/weatherbot-YYYY-MM-DD.tar.gz.enc  (AES-256-CBC)
# Decrypt with:
#   openssl enc -d -aes-256-cbc -pbkdf2 -pass file:/root/.backup_passphrase \
#       -in weatherbot-YYYY-MM-DD.tar.gz.enc | tar xz
#
# To pull off-VPS from your workstation:
#   scp root@107.191.61.132:/root/backups/weatherbot-*.tar.gz.enc ~/local-path/
#
# Retention: 14 days local. Older snapshots auto-pruned.
# Added 2026-06-08 per disaster-recovery plan.

set -u
set -o pipefail

DATE=$(date -u +%Y-%m-%d)
BACKUP_DIR=/root/backups
PASS_FILE=/root/.backup_passphrase
TMP=$(mktemp -d /tmp/wbbackup.XXXXXX)
OUT="$BACKUP_DIR/weatherbot-$DATE.tar.gz.enc"
LOG="$BACKUP_DIR/backup.log"

mkdir -p "$BACKUP_DIR"

if [ ! -s "$PASS_FILE" ]; then
  echo "$(date -u +%FT%TZ) ERROR: passphrase file missing or empty: $PASS_FILE" >> "$LOG"
  exit 1
fi

{
  echo "── $(date -u +%FT%TZ) backup run ──"

  # Stage files to tmp
  mkdir -p "$TMP/snapshot"
  cp -p /root/weatherbot/positions.db    "$TMP/snapshot/" || { echo "FAIL: copy positions.db"; exit 1; }
  cp -p /root/weatherbot/.env            "$TMP/snapshot/" || { echo "FAIL: copy .env"; exit 1; }
  if [ -d /root/weatherbot/calibration_history ]; then
    cp -rp /root/weatherbot/calibration_history "$TMP/snapshot/"
  fi

  # Tar + gzip + encrypt in one pipeline
  tar -C "$TMP" -czf - snapshot \
    | openssl enc -aes-256-cbc -pbkdf2 -salt \
        -pass file:"$PASS_FILE" -out "$OUT"

  if [ ! -s "$OUT" ]; then
    echo "FAIL: encrypted output empty"
    exit 1
  fi

  SIZE=$(du -h "$OUT" | cut -f1)
  echo "OK: $OUT ($SIZE)"

  # Prune older than 14 days
  find "$BACKUP_DIR" -name 'weatherbot-*.tar.gz.enc' -mtime +14 -delete

  # Show what's currently retained
  echo "Retained snapshots:"
  ls -lh "$BACKUP_DIR"/weatherbot-*.tar.gz.enc 2>/dev/null | awk '{print "  " $9 " " $5}'

} >> "$LOG" 2>&1

# Cleanup
rm -rf "$TMP"
