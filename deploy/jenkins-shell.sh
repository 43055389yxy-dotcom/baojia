#!/bin/bash
set -euo pipefail

APP_DIR="${WORKSPACE:?Jenkins workspace is unavailable}"
RELAY_SOURCE_DIR="/home/ec2-user/astraquote/source"
RELAY_SERVICE="astraquote-gpt-relay.service"
RELAY_VENV="/home/ec2-user/astraquote/gpt-relay-venv"

run_as_relay_user() {
  if [ "$(id -un)" = "ec2-user" ]; then
    "$@"
  else
    sudo -n -u ec2-user "$@"
  fi
}

run_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

update_host_browser_relay() {
  command -v rsync >/dev/null
  test -x "$RELAY_VENV/bin/python"

  run_as_root install -d -o ec2-user -g ec2-user \
    "$RELAY_SOURCE_DIR/backend" \
    "$RELAY_SOURCE_DIR/tools" \
    "$RELAY_SOURCE_DIR/policies"

  # Run rsync as root so a locked-down Jenkins workspace remains readable;
  # --chown keeps every runtime file owned by the desktop relay account.
  run_as_root rsync -a --delete --chown=ec2-user:ec2-user \
    --exclude='.venv/' \
    --exclude='.pytest_cache/' \
    --exclude='.ruff_cache/' \
    --exclude='.cache/' \
    --exclude='artifacts/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$APP_DIR/backend/" "$RELAY_SOURCE_DIR/backend/"
  run_as_root rsync -a --delete --chown=ec2-user:ec2-user \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$APP_DIR/tools/" "$RELAY_SOURCE_DIR/tools/"
  run_as_root rsync -a --delete --chown=ec2-user:ec2-user \
    "$APP_DIR/policies/" "$RELAY_SOURCE_DIR/policies/"

  run_as_relay_user "$RELAY_VENV/bin/python" -m compileall -q \
    "$RELAY_SOURCE_DIR/backend/app" \
    "$RELAY_SOURCE_DIR/tools"

  run_as_root install -m 0644 \
    "$APP_DIR/deploy/desktop/astraquote-gpt-relay.service" \
    "/etc/systemd/system/$RELAY_SERVICE"
  run_as_root systemctl daemon-reload
  run_as_root systemctl restart "$RELAY_SERVICE"
  run_as_root systemctl is-active --quiet "$RELAY_SERVICE"
}

for config_file in \
  /home/ec2-user/astraquote/config/backend.env \
  /home/ec2-user/astraquote/config/mcp.env \
  /home/ec2-user/astraquote/config/oauth.env; do
  test -f "$config_file"
done
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
    # The logged-in ChatGPT browser worker is a host systemd service, not a
    # container. Keep its source and policy on exactly the same revision as
    # the frontend/backend/MCP before declaring the deployment successful.
    update_host_browser_relay
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
