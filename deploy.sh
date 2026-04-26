#!/usr/bin/env bash
set -Eeuo pipefail

# xsolia one-command deployment script
# Usage:
#   ./deploy.sh
# Optional env overrides:
#   BRANCH=main REPO_DIR=/srv/xsolia API_DIR=/srv/xsolia/xsolia_backend \
#   FRONTEND_DIR=/srv/xsolia/xsolia_frontend WEB_ROOT=/var/www/xsolia \
#   SERVICE_NAME=xsolia-api API_HEALTH_URL=https://api.xsolia.com/health ./deploy.sh

BRANCH="${BRANCH:-main}"
REPO_DIR="${REPO_DIR:-/srv/xsolia}"
API_DIR="${API_DIR:-$REPO_DIR/xsolia_backend}"
FRONTEND_DIR="${FRONTEND_DIR:-$REPO_DIR/xsolia_frontend}"
WEB_ROOT="${WEB_ROOT:-/var/www/xsolia}"
SERVICE_NAME="${SERVICE_NAME:-xsolia-api}"
LOCAL_HEALTH_URL="${LOCAL_HEALTH_URL:-http://127.0.0.1:8000/health}"
API_HEALTH_URL="${API_HEALTH_URL:-https://api.xsolia.com/health}"

log() {
  printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd git
require_cmd rsync
require_cmd curl
require_cmd python3
require_cmd sudo
require_cmd nginx

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "Not a git repo: $REPO_DIR" >&2
  exit 1
fi

if [[ ! -d "$API_DIR" ]]; then
  echo "Backend directory not found: $API_DIR" >&2
  exit 1
fi

if [[ ! -d "$FRONTEND_DIR" ]]; then
  echo "Frontend directory not found: $FRONTEND_DIR" >&2
  exit 1
fi

log "Updating repository ($BRANCH)"
cd "$REPO_DIR"
git fetch --all --prune
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

log "Updating backend dependencies"
cd "$API_DIR"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
if [[ -f requirements.txt ]]; then
  pip install -q -r requirements.txt
fi

if [[ -f alembic.ini ]]; then
  log "Applying DB migrations (alembic upgrade head)"
  alembic upgrade head
else
  # Current schema cleanup runs inside FastAPI lifespan on service startup.
  log "No alembic.ini found; relying on application startup migrations."
fi

log "Restarting backend service ($SERVICE_NAME)"
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl --no-pager --full status "$SERVICE_NAME" | sed -n '1,12p'
SERVICE_EXEC_START="$(sudo systemctl show "$SERVICE_NAME" --property=ExecStart --value 2>/dev/null || true)"
if [[ "$SERVICE_EXEC_START" == *"gunicorn"* ]] && printf '%s' "$SERVICE_EXEC_START" | grep -Eq '(^|[[:space:]])(-w|--workers[= ]?)[2-9]'; then
  echo "Warning: $SERVICE_NAME appears to run multiple workers. In-memory auth rate limiting is per worker; use one worker or move rate limit state to Redis/database." >&2
fi

log "Publishing frontend"
if [[ -f "$FRONTEND_DIR/package.json" ]]; then
  require_cmd npm
  cd "$FRONTEND_DIR"
  npm ci
  npm run build
  if [[ -d dist ]]; then
    sudo rsync -av --delete --exclude='.DS_Store' dist/ "$WEB_ROOT/"
  elif [[ -d build ]]; then
    sudo rsync -av --delete --exclude='.DS_Store' build/ "$WEB_ROOT/"
  else
    echo "Frontend build output not found (dist/ or build/)." >&2
    exit 1
  fi
else
  sudo rsync -av --delete --exclude='.DS_Store' "$FRONTEND_DIR/" "$WEB_ROOT/"
fi

log "Reloading nginx"
sudo nginx -t
sudo systemctl reload nginx

log "Health checks"
curl -fsS "$LOCAL_HEALTH_URL"
echo
curl -fsS "$API_HEALTH_URL"
echo

log "Deploy completed successfully."
