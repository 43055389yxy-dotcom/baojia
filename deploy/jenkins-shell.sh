#!/bin/bash
set -euo pipefail

APP_DIR=/home/ec2-user/astraquote/source
REPO_URL=https://github.com/43055389yxy-dotcom/baojia.git

mkdir -p /home/ec2-user/astraquote/data /home/ec2-user/astraquote/config

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi

git -C "$APP_DIR" fetch --prune origin
git -C "$APP_DIR" checkout main
git -C "$APP_DIR" pull --ff-only origin main

docker compose -f "$APP_DIR/deploy/compose.production.yml" build --pull
docker compose -p astraquote -f "$APP_DIR/deploy/compose.production.yml" up -d
docker image prune -f --filter "until=168h"

for attempt in {1..24}; do
  if docker exec astraquote curl -fsS http://127.0.0.1:3000/api/backend/api/health >/dev/null; then
    echo "AstraQuote deployment succeeded"
    exit 0
  fi
  sleep 5
done

docker logs --tail 120 astraquote
exit 1
