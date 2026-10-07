#!/usr/bin/env bash
# P0-03: nightly encrypted backup of data/, config/ and .env to off-host storage.
#   deploy/backup.sh                -> writes /opt/iqama-screener/backups/iqama-YYYY-MM-DD-HHMM.tar.gz.age
#   BACKUP_REMOTE=s3:bucket/path    -> also copies it off-host with rclone (any rclone remote: S3, Azure, GCS, SFTP)
# Encryption: age (https://age-encryption.org) with the public key in BACKUP_AGE_RECIPIENT; the private key is
# kept OFF the server (P0-04 escrow). Without age installed, falls back to gpg symmetric with BACKUP_PASSPHRASE.
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/iqama-screener}"
OUT_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
STAMP=$(date -u +%Y-%m-%d-%H%M)
mkdir -p "$OUT_DIR"
cd "$APP_DIR"
[ -f .env ] && set -a && . ./.env && set +a

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
# SQLite: take a consistent copy via the online backup API instead of copying a live file.
# Tries, in order: python inside the api container, python3 on the host, the sqlite3 CLI.
if [ -f data/iqama.db ]; then
  mkdir -p "$TMP/data"
  PYCOPY='import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()'
  if ! docker compose -f deploy/docker-compose.prod.yml exec -T api python3 -c "$PYCOPY" /srv/data/iqama.db /tmp/iqama-bk.db >/dev/null 2>&1 \
     || ! docker compose -f deploy/docker-compose.prod.yml cp api:/tmp/iqama-bk.db "$TMP/data/iqama.db" >/dev/null 2>&1; then
    if command -v python3 >/dev/null; then python3 -c "$PYCOPY" data/iqama.db "$TMP/data/iqama.db"
    elif command -v sqlite3 >/dev/null; then sqlite3 data/iqama.db ".backup '$TMP/data/iqama.db'"
    else echo "ERROR: need python3 or sqlite3 on the host to copy the database safely" >&2; exit 4; fi
  fi
fi
# everything else in data/ (encrypted images, templates) + rules + secrets
tar -C "$APP_DIR" --exclude='data/iqama.db' --exclude='data/iqama.db-wal' --exclude='data/iqama.db-shm' \
    -czf "$TMP/payload.tgz" data config .env
tar -C "$TMP" -czf "$TMP/bundle.tgz" payload.tgz $( [ -d "$TMP/data" ] && echo data )

OUT="$OUT_DIR/iqama-$STAMP.tar.gz"
if command -v age >/dev/null && [ -n "${BACKUP_AGE_RECIPIENT:-}" ]; then
  age -r "$BACKUP_AGE_RECIPIENT" -o "$OUT.age" "$TMP/bundle.tgz"; OUT="$OUT.age"
elif [ -n "${BACKUP_PASSPHRASE:-}" ]; then
  gpg --batch --yes --symmetric --cipher-algo AES256 --passphrase "$BACKUP_PASSPHRASE" -o "$OUT.gpg" "$TMP/bundle.tgz"; OUT="$OUT.gpg"
else
  echo "ERROR: set BACKUP_AGE_RECIPIENT (preferred) or BACKUP_PASSPHRASE in .env - backups must be encrypted" >&2; exit 2
fi
sha256sum "$OUT" > "$OUT.sha256"
chmod 600 "$OUT"

if [ -n "${BACKUP_REMOTE:-}" ]; then
  command -v rclone >/dev/null || { echo "rclone not installed; install it or unset BACKUP_REMOTE" >&2; exit 3; }
  rclone copy "$OUT" "$BACKUP_REMOTE/" && rclone copy "$OUT.sha256" "$BACKUP_REMOTE/"
  rclone delete --min-age "${KEEP_DAYS}d" "$BACKUP_REMOTE/" || true
fi
find "$OUT_DIR" -name 'iqama-*' -mtime +"$KEEP_DAYS" -delete
echo "backup ok: $OUT ($(du -h "$OUT" | cut -f1))"
