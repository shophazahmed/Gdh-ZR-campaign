#!/usr/bin/env bash
# Admin helper installed as /usr/local/bin/voter-portal by setup_vps.sh.
#
#   sudo voter-portal ingest "/root/voter-data" [--dry-run] [--mode replace]
#   sudo voter-portal create-user alice --role admin
#   sudo voter-portal set-password alice
#   sudo voter-portal backup
#   sudo voter-portal logs
set -euo pipefail

APP_DIR="/opt/voter-portal"
DATA_DIR="/var/lib/voter-portal"
APP_USER="voterportal"
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

as_app() {
  # Run as the service user with the service's environment
  sudo -u "$APP_USER" --preserve-env=SEED_ADMIN_PASSWORD,SEED_USER_PASSWORD bash -c 'set -a; . /etc/voter-portal/env; set +a; cd /opt/voter-portal; exec "$@"' _ "$@"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  ingest)
    src="${1:?usage: voter-portal ingest <folder with the voter files> [ingest options]}"; shift
    # Copy the files where the service user can read them; the copy is removed afterwards.
    work="$DATA_DIR/import-$(date +%s)"
    install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$work"
    cp -r "$src"/. "$work"/
    chown -R "$APP_USER:$APP_USER" "$work"
    trap 'rm -rf "$work"' EXIT
    as_app "$APP_DIR/.venv/bin/python" scripts/ingest.py --data-dir "$work" \
      --log "$DATA_DIR/merge_conflicts.log" "$@"
    echo "Conflict log: $DATA_DIR/merge_conflicts.log  (view: sudo less $DATA_DIR/merge_conflicts.log)"
    ;;
  create-user|set-password)
    as_app "$APP_DIR/.venv/bin/flask" --app wsgi "$cmd" "$@"
    ;;
  backup)
    dest="/var/backups/voter-portal"
    stamp="$(date +%Y%m%d-%H%M%S)"
    sqlite3 "$DATA_DIR/voter_portal.db" ".backup '$dest/voter_portal-$stamp.db'"
    tar -czf "$dest/photos-$stamp.tar.gz" -C "$DATA_DIR" photos
    chmod 600 "$dest"/*-"$stamp".*
    find "$dest" -type f -mtime +14 -delete
    echo "Backed up to $dest ($stamp)"
    ;;
  logs)
    journalctl -u voter-portal -n 100 --no-pager
    ;;
  restart)
    systemctl restart voter-portal && systemctl --no-pager status voter-portal | head -5
    ;;
  *)
    sed -n '2,9p' "$0"; exit 1 ;;
esac
