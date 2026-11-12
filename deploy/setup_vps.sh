#!/usr/bin/env bash
# Install or update the voter portal on an Ubuntu VPS (22.04 / 24.04).
#
#   sudo bash deploy/setup_vps.sh --email you@example.com
#
# Safe to re-run: it updates the code, keeps the database, secret key and certificate.
set -euo pipefail

DOMAIN="vote.seenu.online"
APP_DIR="/opt/voter-portal"
DATA_DIR="/var/lib/voter-portal"
ENV_DIR="/etc/voter-portal"
APP_USER="voterportal"
EMAIL=""
SKIP_CERT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email) EMAIL="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --skip-cert) SKIP_CERT=1; shift ;;
    *) echo "Unknown option $1"; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
[[ -f "$SRC/wsgi.py" ]] || { echo "Run this from the voter-portal folder of the repository."; exit 1; }
say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

say "Checking ports 80/443"
owners="$(ss -Hltnp '( sport = :80 or sport = :443 )' 2>/dev/null | grep -oE 'users:\(\("[^"]+' | cut -d'"' -f2 | sort -u | tr '\n' ' ' || true)"
if [[ -n "$owners" && ! "$owners" =~ ^(nginx[[:space:]]*)+$ ]]; then
  echo "Ports 80/443 are already used by: $owners"
  echo "Another proxy (e.g. Traefik from Docker) owns them, so Nginx can't start."
  echo "Use the Docker + Traefik option in voter-portal/README.md instead, or stop that proxy first."
  exit 1
fi

say "Checking DNS for $DOMAIN"
server_ip="$(curl -4fsS https://api.ipify.org || true)"
dns_ip="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
echo "server: ${server_ip:-unknown}   $DOMAIN -> ${dns_ip:-not found}"
if [[ -n "$server_ip" && "$server_ip" != "$dns_ip" && $SKIP_CERT -eq 0 ]]; then
  echo "Point an A record for $DOMAIN at $server_ip first (certbot will fail otherwise)."
  exit 1
fi

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq python3 python3-venv python3-pip nginx certbot sqlite3 rsync curl

say "Creating user and folders"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$DATA_DIR" "$DATA_DIR/photos"
install -d -o root -g "$APP_USER" -m 750 "$ENV_DIR"
install -d -m 755 /var/www/certbot
install -d -o root -g root -m 700 /var/backups/voter-portal

say "Copying code to $APP_DIR"
install -d -m 755 "$APP_DIR"
rsync -a --delete --exclude '.venv' --exclude 'instance' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '*.db' --exclude '*.log' --exclude 'data' --exclude 'Data files' --exclude '*.xls*' --exclude '*.csv' \
  "$SRC/" "$APP_DIR/"
chown -R root:root "$APP_DIR"
chmod -R u=rwX,go=rX "$APP_DIR"

say "Python virtualenv"
[[ -d "$APP_DIR/.venv" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [[ ! -f "$ENV_DIR/env" ]]; then
  say "Writing $ENV_DIR/env with a new secret key"
  cat > "$ENV_DIR/env" <<EOF
VOTER_PORTAL_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
VOTER_PORTAL_DATA_DIR=$DATA_DIR
GUNICORN_BIND=127.0.0.1:8001
EOF
fi
chown root:"$APP_USER" "$ENV_DIR/env"
chmod 640 "$ENV_DIR/env"

say "Admin helper: /usr/local/bin/voter-portal"
install -m 755 "$APP_DIR/deploy/voter-portal-cli.sh" /usr/local/bin/voter-portal

say "systemd service"
install -m 644 "$APP_DIR/deploy/systemd/voter-portal.service" /etc/systemd/system/voter-portal.service
systemctl daemon-reload
systemctl enable voter-portal >/dev/null
systemctl restart voter-portal
sleep 2
systemctl is-active --quiet voter-portal || { journalctl -u voter-portal -n 40 --no-pager; exit 1; }
curl -fsS -o /dev/null -w "app answers: HTTP %{http_code}\n" http://127.0.0.1:8001/login

say "Nginx"
rm -f /etc/nginx/sites-enabled/default
CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
SITE="/etc/nginx/sites-available/$DOMAIN"
if [[ ! -f "$CERT" ]]; then
  # Port-80-only config so certbot can answer the HTTP challenge
  cat > "$SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 503; }
}
EOF
  ln -sf "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"
  nginx -t && systemctl reload nginx
  if [[ $SKIP_CERT -eq 1 ]]; then
    echo "Skipping the certificate (--skip-cert). The site is not served until you re-run without it."
    exit 0
  fi
  say "Requesting the Let's Encrypt certificate"
  if [[ -n "$EMAIL" ]]; then reg=(--email "$EMAIL"); else reg=(--register-unsafely-without-email); fi
  certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" --agree-tos --non-interactive "${reg[@]}"
fi
sed "s/vote\.seenu\.online/$DOMAIN/g" "$APP_DIR/deploy/nginx/vote.seenu.online.conf" > "$SITE"
ln -sf "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"
nginx -t
systemctl reload nginx

install -d /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
systemctl list-timers certbot.timer --no-pager >/dev/null 2>&1 && echo "certbot.timer renews the certificate automatically."

say "Nightly database backup (03:15, keeps 14 days)"
cat > /etc/cron.d/voter-portal-backup <<'EOF'
15 3 * * * root /usr/local/bin/voter-portal backup >/dev/null 2>&1
EOF

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  say "Firewall: allowing HTTP/HTTPS"
  ufw allow 'Nginx Full' >/dev/null
fi

say "Done: https://$DOMAIN"
echo "Next: copy the voter files to the server and run"
echo "  sudo voter-portal ingest /root/voter-data"
