#!/bin/bash
set -euo pipefail

APP_DIR="${WORKSPACE:?Jenkins workspace is unavailable}"

test -f /home/ec2-user/astraquote/config/backend.env
test -d /home/ec2-user/astraquote/data

# Jenkins 已通过“源码管理”检出代码。将工作区打包送入 Docker，
# 避免容器内工作区路径与宿主机路径不同导致构建失败。
tar -C "$APP_DIR" -cf - . \
  | docker build --pull -f deploy/Dockerfile -t astraquote:production -

docker compose -p astraquote \
  -f "$APP_DIR/deploy/compose.production.yml" \
  up -d --no-build
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
