# ops/

System-level configuration that lives **outside** the bot working
directory but is required for the bot to run as a managed service.

Backed up here so a fresh VPS can be brought up to operational state
without spelunking through chat history.

## Files

| Repo file | Deploy to | Purpose |
|---|---|---|
| `weatherbot.service` | `/etc/systemd/system/weatherbot.service` | Systemd unit for the bot |
| `logrotate-weatherbot` | `/etc/logrotate.d/weatherbot` | Daily log rotation (uses `copytruncate` — DOES NOT signal the process) |
| `watchdog.sh` | `/root/watchdog.sh` | 5-min cron check that restarts the bot if down and sends Telegram alert |
| `validation_weekly_check.sh` | runs in place from `ops/` | Weekly Stage B monitor: lock status + resolved-paper-trade progress; runs the full evaluation once n≥100 |

> **Canonical bot dir: `/root/weatherbot`.** Verified 2026-07-03 as the path
> the live `weatherbot.service` runs from, where the fixed post-PR#7 code
> lives, and where the Stage B `positions.db` accumulates. The service file,
> README install steps, and cron all use it. A `git clone` produces an
> `Ides-of-March/` directory, so deploy by cloning/rsyncing *into*
> `/root/weatherbot` (not by using the clone's own name) — otherwise the
> service launches a different (possibly stale) checkout than the one you
> updated.

## Install on a fresh VPS

```bash
# 1. Bot code + venv must already be at /root/weatherbot/
#    (clone this repo there, recreate venv, restore .env from backup)

# 2. Drop the system files into place
sudo install -m 0644 ops/weatherbot.service     /etc/systemd/system/weatherbot.service
sudo install -m 0644 ops/logrotate-weatherbot   /etc/logrotate.d/weatherbot
sudo install -m 0755 ops/watchdog.sh            /root/watchdog.sh

# 3. Activate
sudo systemctl daemon-reload
sudo systemctl enable --now weatherbot

# 4. Crontab — add the watchdog + the bot's own scheduled jobs
sudo crontab -l > /tmp/cron
cat >> /tmp/cron <<'EOF'
*/5 * * * * /root/watchdog.sh
30 4 * * 0  /root/weatherbot/weekly_calibration.sh
0  3 * * *  /root/weatherbot/backup.sh
EOF
sudo crontab /tmp/cron && rm /tmp/cron
```

## Stage B validation monitor

`validation_weekly_check.sh` is a **read-only** monitor for the paper
campaign (see `VALIDATION_PLAN.md`). It never unlocks live trading — the
`executor.py` certificate check remains the sole gate. It reports the lock
status, counts resolved paper trades entered on/after the Stage B cutoff
(`SINCE`, default `2026-07-02`, so the pre-fix June live trades are
excluded), and once the count reaches `TARGET_N` (default 100) runs the
full make-or-break evaluation and reports pass/fail.

```bash
# Weekly cron (Sundays 05:00 UTC). Uses the repo copy in place.
sudo crontab -l > /tmp/cron
echo '0 5 * * 0 /root/weatherbot/ops/validation_weekly_check.sh >> /var/log/validation_check.log 2>&1' >> /tmp/cron
sudo crontab /tmp/cron && rm /tmp/cron

# Manual run
BOT_DIR=/root/weatherbot ops/validation_weekly_check.sh
```

When the campaign is ready to be scored manually:

```bash
python3 validation_gate.py --evaluate positions.db --since 2026-07-02
```

## Why the SIGHUP postrotate was removed (2026-06-11)

The original `logrotate-weatherbot` had a `postrotate` hook that ran
`systemctl kill -s HUP weatherbot.service` after each rotation. Python's
default SIGHUP handler is to terminate the process, and `bot.py` had no
custom handler — so logrotate was assassinating the bot at 00:00 UTC
every day. `Restart=always` would normally bring it back, but on
2026-06-10 the unit file got replaced with a `/dev/null` symlink
(`systemctl mask`), so `Restart=always` and `watchdog.sh` both failed.

Two-layer fix:

1. **logrotate uses `copytruncate`** instead of signalling — file handle
   stays valid through rotation.
2. **`bot.py` ignores SIGHUP** explicitly (`signal.signal(SIGHUP,
   SIG_IGN)` at the top of `main()`) — belt-and-suspenders against any
   future config that re-introduces the signal.

## Secrets

`watchdog.sh` sources `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` from
`/root/weatherbot/.env`. The `.env` is **not** in the repo (gitignored)
— it lives in the daily encrypted backup at `/root/backups/`. To
restore: decrypt the latest snapshot and copy `.env` into place
before starting the bot.
