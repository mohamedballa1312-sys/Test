#!/usr/bin/env bash
# P0-03/P0-07: nightly backup at 02:00 UTC; retention purge runs inside the app (IQAMA_PURGE_INTERVAL_HOURS).
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/iqama-screener}"
LINE="0 2 * * * APP_DIR=$APP_DIR $APP_DIR/deploy/backup.sh >> $APP_DIR/backups/backup.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'deploy/backup.sh' ; echo "$LINE" ) | crontab -
mkdir -p "$APP_DIR/backups"; echo "cron installed: $LINE"
