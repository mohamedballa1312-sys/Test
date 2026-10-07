#!/usr/bin/env bash
# One-shot setup on a fresh Ubuntu 22.04/24.04 VM (4 vCPU / 8 GB RAM recommended).
#   curl -fsSL https://raw.githubusercontent.com/mohamedballa1312-sys/Test/main/deploy/setup-server.sh | sudo bash -s -- [DOMAIN]
# DOMAIN is optional: with it you get HTTPS (point the DNS A record to this server first); without it, HTTP on the IP.
set -euo pipefail
DOMAIN="${1:-}"
REPO="https://github.com/mohamedballa1312-sys/Test.git"
BRANCH="main"
APP_DIR="/opt/iqama-screener"

echo "==> Installing Docker"
if ! command -v docker >/dev/null; then
  apt-get update -qq && apt-get install -y -qq ca-certificates curl git ufw >/dev/null
  curl -fsSL https://get.docker.com | sh >/dev/null
fi

echo "==> Firewall: allow SSH, HTTP, HTTPS only"
ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw --force enable >/dev/null

if [ -z "$DOMAIN" ] && [ "${ALLOW_HTTP:-0}" != "1" ]; then
  echo "ERROR: a domain is required for HTTPS (P0-02). For a throw-away test without TLS run: ALLOW_HTTP=1 $0" >&2; exit 2
fi

echo "==> Fetching the application"
if [ -d "$APP_DIR/.git" ]; then git -C "$APP_DIR" pull -q; else git clone -q -b "$BRANCH" "$REPO" "$APP_DIR"; fi
cd "$APP_DIR"; mkdir -p data

if [ ! -f .env ]; then
  echo "==> Generating secrets (.env)"
  ENC_KEY=$(python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())" 2>/dev/null || openssl rand -base64 32)
  API_KEY=$(openssl rand -hex 24)
  UI_PASSWORD=$(openssl rand -base64 12 | tr -d '/+=' | cut -c1-14)
  UI_HASH=$(docker run --rm caddy:2 caddy hash-password --plaintext "$UI_PASSWORD")
  BACKUP_PASSPHRASE=$(openssl rand -base64 24 | tr -d '/+=')
  cat > .env <<ENV
DOMAIN=$DOMAIN
SITE_ADDRESS=${DOMAIN:-:80}
IQAMA_PUBLIC_URL=${DOMAIN:+https://$DOMAIN}
IQAMA_OCR_PROVIDER=easyocr
IQAMA_WORKERS=2
IQAMA_PURGE_INTERVAL_HOURS=24
IQAMA_MAX_FILES_PER_UPLOAD=200
IQAMA_ENC_KEY=$ENC_KEY
IQAMA_API_KEY=$API_KEY
UI_USER=admin
UI_PASSWORD_HASH=$UI_HASH
# --- backups (P0-03): set BACKUP_AGE_RECIPIENT (age public key, preferred) or keep the generated passphrase;
#     set BACKUP_REMOTE to an rclone remote (e.g. s3:my-bucket/iqama) for off-host copies
BACKUP_PASSPHRASE=$BACKUP_PASSPHRASE
BACKUP_REMOTE=
BACKUP_KEEP_DAYS=30
ENV
  chmod 600 .env
  echo "$UI_PASSWORD" > .ui_password && chmod 600 .ui_password
fi

echo "==> Installing the nightly backup job"
APP_DIR="$APP_DIR" deploy/install-cron.sh

echo "==> Building and starting (first build downloads OCR models; allow 5-10 minutes)"
docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d --build

IP=$(curl -fsS -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "=============================================================="
echo " Iqama Screener is starting."
if [ -n "$DOMAIN" ]; then echo " URL      : https://$DOMAIN"; else echo " URL      : http://$IP   (HTTP only - add a domain for HTTPS)"; fi
echo " Login    : admin  /  $(cat .ui_password)"
echo " API docs : /docs   (same login)"
echo " Data     : $APP_DIR/data   Rules: $APP_DIR/config   Secrets: $APP_DIR/.env"
echo " Logs     : docker compose -f $APP_DIR/deploy/docker-compose.prod.yml logs -f"
echo " Backups  : nightly 02:00 UTC -> $APP_DIR/backups (encrypted); set BACKUP_REMOTE in .env for off-host copies"
echo "=============================================================="
echo
echo " ACTION REQUIRED (P0-04 key escrow): copy IQAMA_ENC_KEY and BACKUP_PASSPHRASE from $APP_DIR/.env"
echo " into your cloud provider's secret manager AND a sealed envelope held by a second officer."
echo " Losing IQAMA_ENC_KEY makes the database and every backup unreadable."
