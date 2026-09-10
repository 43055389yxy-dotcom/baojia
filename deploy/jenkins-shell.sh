#!/bin/bash
set -euo pipefail

APP_DIR="${WORKSPACE:?Jenkins workspace is unavailable}"

test -f /home/ec2-user/astraquote/config/backend.env
test -d /home/ec2-user/astraquote/data

# Jenkins 已通过“源码管理”检出代码。将工作区打包送入 Docker，
# 避免容器内工作区路径与宿主机路径不同导致构建失败。
tar -C "$APP_DIR" \
  --exclude='./.git' \
  --exclude='./backend/.venv' \
  --exclude='./backend/.pytest_cache' \
  --exclude='./backend/.ruff_cache' \
  --exclude='./backend/.cache' \
  --exclude='./backend/artifacts' \
  --exclude='./outputs' \
  --exclude='./**/node_modules' \
  --exclude='./**/.next' \
  --exclude='./**/dist' \
  --exclude='./**/.wrangler' \
  --exclude='./**/coverage' \
  --exclude='./**/__pycache__' \
  --exclude='./**/*.pyc' \
  -cf - . \
  | docker build --pull -f deploy/Dockerfile -t astraquote:production -

docker compose -p astraquote \
  -f "$APP_DIR/deploy/compose.production.yml" \
  up -d --no-build

for attempt in {1..24}; do
  if docker exec astraquote curl -fsS http://127.0.0.1:3000/api/backend/api/health >/dev/null \
    && docker exec astraquote curl -fsS http://127.0.0.1:8200/readyz >/dev/null \
    && docker exec astraquote curl -fsS http://127.0.0.1:8001/readyz >/dev/null; then
    # Only after the new application is healthy, remove stopped AstraQuote
    # containers and unused images carrying our explicit label. Never prune
    # another application's containers, images, volumes, or network.
    docker container prune -f --filter "label=com.docker.compose.project=astraquote"
    docker image prune -a -f --filter "label=com.astraquote.image=true"
    echo "AstraQuote deployment succeeded"
    exit 0
  fi
  sleep 5
done

docker logs --tail 120 astraquote
exit 1
