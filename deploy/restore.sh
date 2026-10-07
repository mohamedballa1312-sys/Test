#!/usr/bin/env bash
# P0-03: restore a backup made by deploy/backup.sh onto a (fresh or existing) install.
#   deploy/restore.sh /path/to/iqama-2026-10-07-0200.tar.gz.age   (needs BACKUP_AGE_IDENTITY=/path/to/key.txt)
#   deploy/restore.sh /path/to/iqama-....tar.gz.gpg                (needs BACKUP_PASSPHRASE)
# Stops the stack, restores data/ config/ .env, starts the stack, prints the health check.
set -euo pipefail
SRC="${1:?backup file}"; APP_DIR="${APP_DIR:-/opt/iqama-screener}"
cd "$APP_DIR"
[ -f "$SRC.sha256" ] && (cd "$(dirname "$SRC")" && sha256sum -c "$(basename "$SRC").sha256")
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
case "$SRC" in
  *.age) age -d -i "${BACKUP_AGE_IDENTITY:?set BACKUP_AGE_IDENTITY}" -o "$TMP/bundle.tgz" "$SRC" ;;
  *.gpg) gpg --batch --yes --passphrase "${BACKUP_PASSPHRASE:?set BACKUP_PASSPHRASE}" -o "$TMP/bundle.tgz" -d "$SRC" ;;
  *) cp "$SRC" "$TMP/bundle.tgz" ;;
esac
tar -C "$TMP" -xzf "$TMP/bundle.tgz"
[ "${SKIP_DOCKER:-0}" = "1" ] || docker compose -f deploy/docker-compose.prod.yml down 2>/dev/null || true
[ -d data ] && mv data "data.before-restore-$(date -u +%s)"
tar -C "$APP_DIR" -xzf "$TMP/payload.tgz"
[ -f "$TMP/data/iqama.db" ] && cp "$TMP/data/iqama.db" data/iqama.db
chmod 600 .env
if [ "${SKIP_DOCKER:-0}" = "1" ]; then echo "restored files from $SRC (SKIP_DOCKER=1: stack not started)"; exit 0; fi
docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d
echo "restored from $SRC; waiting for health..."
for i in $(seq 1 60); do curl -sf http://127.0.0.1/health >/dev/null 2>&1 && { echo "health ok after ${i}s"; exit 0; }; sleep 2; done
echo "health check did not pass in 120 s - inspect: docker compose -f deploy/docker-compose.prod.yml logs api" >&2; exit 1
