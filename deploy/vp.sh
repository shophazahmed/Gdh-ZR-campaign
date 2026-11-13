#!/usr/bin/env bash
# Manage candidate portals on one VPS. Each candidate runs in its own container with its own data.
#
#   deploy/vp.sh build <version>                       build image vote-portal:<version> from git tag/branch
#   deploy/vp.sh new <slug> <domain> "<Campaign name>" [version]
#                                                      create candidates/<slug>/.env and start it
#   deploy/vp.sh up <slug>                             (re)start a candidate with its current settings
#   deploy/vp.sh upgrade <slug> <version>              move one candidate to another version
#   deploy/vp.sh list                                  every candidate: domain, version, state
#   deploy/vp.sh logs <slug>
#   deploy/vp.sh stop <slug>                           stop (data is kept)
#   deploy/vp.sh flask <slug> create-user <name> --role admin|user|agent
#   deploy/vp.sh flask <slug> set-password <name>
#   deploy/vp.sh ingest <slug> <folder> [ingest options]   import voter files from the server
#   deploy/vp.sh backup <slug>|all                     database + photos -> $VP_BACKUP_DIR (default /var/backups/vote)
#   deploy/vp.sh adopt <slug> <old-volume>             copy an existing portal's data volume into this
#                                                      candidate (create it first with VP_NO_START=1 ... new)
#
# Run from the repository root on the VPS (e.g. /opt/vote). Needs docker compose v2 and the shared
# Traefik that already holds ports 80/443.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAND_DIR="$ROOT/candidates"
COMPOSE="$ROOT/deploy/candidate-compose.yml"
BACKUP_DIR="${VP_BACKUP_DIR:-/var/backups/vote}"
SLUG_RE='^[a-z0-9][a-z0-9-]{1,29}$'

die() { echo "error: $*" >&2; exit 1; }
need_slug() {
  [[ "${1:-}" =~ $SLUG_RE ]] || die "slug must be 2-30 characters: a-z, 0-9 and '-' (got '${1:-}')"
  [[ -f "$CAND_DIR/$1/.env" ]] || die "no candidate '$1' (expected $CAND_DIR/$1/.env)"
}
env_of() { grep -E "^$2=" "$CAND_DIR/$1/.env" | tail -1 | cut -d= -f2-; }
dc() { local slug="$1"; shift; docker compose -p "vp-$slug" --env-file "$CAND_DIR/$slug/.env" -f "$COMPOSE" "$@"; }
random() { python3 -c "import secrets,sys; print(secrets.token_${1}(int(sys.argv[1])))" "$2"; }

build() {
  local version="${1:?usage: vp.sh build <git tag or branch>}"
  git -C "$ROOT" rev-parse --verify --quiet "$version^{commit}" >/dev/null \
    || die "'$version' is not a tag, branch or commit here (try: git fetch --tags)"
  local commit; commit="$(git -C "$ROOT" rev-parse "$version^{commit}")"
  local built; built="$(docker image inspect -f '{{index .Config.Labels "vp.commit"}}' "vote-portal:$version" 2>/dev/null || true)"
  if [[ "$built" == "$commit" ]]; then
    echo "vote-portal:$version already built (${commit:0:7})"; return
  fi
  if docker image inspect "vote-portal:$version" >/dev/null 2>&1; then
    echo "vote-portal:$version was built from other code; rebuilding from ${commit:0:7}"
  fi
  # Build from exactly that version, not from whatever is checked out
  git -C "$ROOT" archive "$version" | docker build --label "vp.commit=$commit" \
    --build-arg "VP_BUILD=$version (${commit:0:7})" -t "vote-portal:$version" -
}

set_env() {  # set_env <slug> KEY VALUE
  local file="$CAND_DIR/$1/.env"
  if grep -qE "^$2=" "$file"; then
    python3 - "$file" "$2" "$3" <<'PY'
import sys
path, key, value = sys.argv[1:]
lines = open(path).read().splitlines()
open(path, "w").write("\n".join(f"{key}={value}" if l.startswith(key + "=") else l for l in lines) + "\n")
PY
  else
    echo "$2=$3" >> "$file"
  fi
}

new() {
  local slug="${1:-}" domain="${2:-}" name="${3:-}" version="${4:-}"
  [[ "$slug" =~ $SLUG_RE ]] || die "usage: vp.sh new <slug> <domain> \"<Campaign name>\" [version]"
  [[ "$domain" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]] || die "domain looks wrong: '$domain'"
  [[ -n "$name" ]] || die "give the campaign name, e.g. \"Gadhdhoo\""
  [[ ! -e "$CAND_DIR/$slug" ]] || die "candidate '$slug' already exists"
  if [[ -z "$version" ]]; then
    version="$(git -C "$ROOT" describe --tags --abbrev=0 2>/dev/null || true)"
    [[ -n "$version" ]] || die "no git tag yet: pass a version (e.g. main) or tag a release first"
  fi
  grep -rqsxF "VP_DOMAIN=$domain" "$CAND_DIR"/*/.env && die "$domain is already used by another candidate"
  build "$version"

  local admin_pw; admin_pw="$(random urlsafe 12)"
  install -d -m 700 "$CAND_DIR/$slug"
  umask 077
  cat > "$CAND_DIR/$slug/.env" <<EOF
# Candidate portal: $name. Secrets: keep this file chmod 600 and out of git.
VP_SLUG=$slug
VP_DOMAIN=$domain
VP_VERSION=$version
VOTER_PORTAL_CAMPAIGN_NAME=$name
VOTER_PORTAL_SECRET_KEY=$(random hex 32)
# Used once, on the first start, to create the 'admin' account (the candidate's own account).
VOTER_PORTAL_ADMIN_PASSWORD=$admin_pw
GUNICORN_WORKERS=2
VP_MEMORY=512m
TRAEFIK_ENTRYPOINT=websecure
TRAEFIK_CERTRESOLVER=letsencrypt
EOF
  if [[ -n "${VP_NO_START:-}" ]]; then
    echo "Created candidates/$slug/.env (not started). Next: deploy/vp.sh adopt $slug <old-volume>"
    return
  fi
  dc "$slug" up -d
  cat <<EOF

Started vp-$slug  ->  https://$domain   (version $version)
  Sign in as:  admin
  Password:    $admin_pw     (asked to change it on first sign-in)

DNS: $domain must point at this server (A record, or a wildcard *.${domain#*.}).
The first visit may take a minute while Traefik gets the certificate.
EOF
}

list() {
  printf '%-16s %-34s %-14s %s\n' SLUG DOMAIN VERSION STATE
  shopt -s nullglob
  for f in "$CAND_DIR"/*/.env; do
    local slug; slug="$(basename "$(dirname "$f")")"
    local state; state="$(docker inspect -f '{{.State.Status}}{{if .State.Health}} ({{.State.Health.Status}}){{end}}' "vp-$slug" 2>/dev/null | head -1 || true)"
    [[ -n "${state//[[:space:]]/}" ]] || state="not created"
    printf '%-16s %-34s %-14s %s\n' "$slug" "$(env_of "$slug" VP_DOMAIN)" "$(env_of "$slug" VP_VERSION)" "$state"
  done
}

backup_one() {
  local slug="$1" stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  install -d -m 700 "$BACKUP_DIR/$slug"
  local out="$BACKUP_DIR/$slug/vp-$slug-$stamp.tar.gz"
  # sqlite3 .backup gives a consistent copy while the portal keeps running
  docker exec "vp-$slug" sh -c 'set -e; sqlite3 /data/voter_portal.db ".backup /tmp/voter_portal.db";
    tar -czf - -C /tmp voter_portal.db -C /data --exclude=./voter_portal.db* --exclude=./imports .;
    rm -f /tmp/voter_portal.db' > "$out"
  chmod 600 "$out"
  # keep the newest 14
  ls -1t "$BACKUP_DIR/$slug"/vp-"$slug"-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
  echo "$out ($(du -h "$out" | cut -f1))"
}

adopt() {
  local slug="$1" old="${2:-}"
  [[ -n "$old" ]] || die "usage: vp.sh adopt <slug> <old-volume>   (see: docker volume ls)"
  docker volume inspect "$old" >/dev/null 2>&1 || die "no docker volume '$old'"
  local users; users="$(docker ps -q --filter "volume=$old")"
  [[ -z "$users" ]] || die "volume '$old' is still used by a running container; stop the old portal first"
  docker inspect "vp-$slug" >/dev/null 2>&1 && [[ "$(docker inspect -f '{{.State.Running}}' "vp-$slug")" == true ]] \
    && die "vp-$slug is running; adopt only into a candidate that hasn't started"
  local version; version="$(env_of "$slug" VP_VERSION)"
  build "$version"
  dc "$slug" create   # creates the vp-<slug>_data volume (and container) without starting
  echo "Copying $old -> vp-${slug}_data ..."
  docker run --rm -u 0 --entrypoint sh -v "$old":/from:ro -v "vp-${slug}_data":/to "vote-portal:$version" \
    -c 'cp -a /from/. /to/ && chown -R voterportal:voterportal /to && chmod 700 /to && ls -la /to'
  dc "$slug" up -d
  echo "vp-$slug started on the copied data. The old volume '$old' is untouched; remove it once you're happy."
}

cmd="${1:-}"; shift || true
case "$cmd" in
  build) build "$@" ;;
  new) new "$@" ;;
  up) need_slug "${1:-}"; build "$(env_of "$1" VP_VERSION)"; dc "$1" up -d ;;
  upgrade)
    need_slug "${1:-}"; [[ -n "${2:-}" ]] || die "usage: vp.sh upgrade <slug> <version>"
    backup_one "$1"
    build "$2"; set_env "$1" VP_VERSION "$2"; dc "$1" up -d
    echo "vp-$1 now on $2 (backup taken first; to go back: vp.sh upgrade $1 <old version>)" ;;
  list) list ;;
  adopt) need_slug "${1:-}"; adopt "$@" ;;
  logs) need_slug "${1:-}"; docker logs --tail 200 -f "vp-$1" ;;
  stop) need_slug "${1:-}"; dc "$1" stop ;;
  flask) need_slug "${1:-}"; slug="$1"; shift; docker exec -it "vp-$slug" flask --app wsgi "$@" ;;
  ingest)
    need_slug "${1:-}"; [[ -d "${2:-}" ]] || die "usage: vp.sh ingest <slug> <folder> [options]"
    slug="$1" src="$2"; shift 2
    work="/data/import-cli-$(date +%s)"
    # Stream the files in as the container's own user, run the merge, then remove the copy
    tar -C "$src" -cf - . | docker exec -i "vp-$slug" sh -c "mkdir -m 700 '$work' && tar -xf - -C '$work'"
    docker exec "vp-$slug" python scripts/ingest.py --data-dir "$work" --log /data/merge_conflicts.log --no-seed-users "$@" \
      || { docker exec "vp-$slug" rm -rf "$work"; exit 1; }
    docker exec "vp-$slug" rm -rf "$work"
    echo "Conflict log inside the container: /data/merge_conflicts.log" ;;
  backup)
    [[ -n "${1:-}" ]] || die "usage: vp.sh backup <slug>|all"
    if [[ "$1" == all ]]; then
      shopt -s nullglob
      for f in "$CAND_DIR"/*/.env; do backup_one "$(basename "$(dirname "$f")")"; done
    else need_slug "$1"; backup_one "$1"; fi ;;
  *) sed -n '2,17p' "$0"; exit 1 ;;
esac
