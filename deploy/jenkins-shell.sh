#!/bin/bash
set -euo pipefail

APP_DIR="${WORKSPACE:?Jenkins workspace is unavailable}"
RELAY_HOST_ROOT="/home/ec2-user/astraquote"
RELAY_STAGE_NAME=".relay-stage-${GIT_COMMIT:-manual}"
RELAY_WORKER_COMMAND="/home/ec2-user/astraquote/gpt-relay-venv/bin/python /home/ec2-user/astraquote/source/tools/gpt_quote_relay_worker.py"

stage_host_browser_relay() {
  echo "Staging the desktop relay source on the Docker host"
  tar -C "$APP_DIR" \
    --exclude='backend/.venv' \
    --exclude='backend/.pytest_cache' \
    --exclude='backend/.ruff_cache' \
    --exclude='backend/.cache' \
    --exclude='backend/artifacts' \
    --exclude='**/__pycache__' \
    --exclude='**/*.pyc' \
    -cf - backend tools policies deploy/desktop/astraquote-gpt-relay.service \
    | docker run --rm -i \
      -e RELAY_STAGE_NAME="$RELAY_STAGE_NAME" \
      -v "$RELAY_HOST_ROOT:/host/astraquote" \
      --entrypoint /bin/sh \
      astraquote:production -ceu '
        stage="/host/astraquote/$RELAY_STAGE_NAME"
        rm -rf "$stage"
        mkdir -p "$stage"
        tar -xf - -C "$stage"
        test -f "$stage/tools/gpt_quote_relay_worker.py"
        test -f "$stage/backend/app/services/gpt_quote_prompt.py"
        test -f "$stage/policies/sales-selection-policy.json"
        test -f "$stage/deploy/desktop/astraquote-gpt-relay.service"
        chown -R 1000:1000 "$stage"
      '
}

activate_host_browser_relay() {
  echo "Activating the staged desktop relay source"
  docker run --rm \
    -e RELAY_STAGE_NAME="$RELAY_STAGE_NAME" \
    -v "$RELAY_HOST_ROOT:/host/astraquote" \
    --entrypoint /bin/sh \
    astraquote:production -ceu '
      stage="/host/astraquote/$RELAY_STAGE_NAME"
      target=/host/astraquote/source
      test -d "$stage/backend"
      test -d "$stage/tools"
      test -d "$stage/policies"
      mkdir -p "$target"
      for name in backend tools policies; do
        previous="/host/astraquote/.relay-previous-$name"
        rm -rf "$previous"
        if test -e "$target/$name"; then
          mv "$target/$name" "$previous"
        fi
        mv "$stage/$name" "$target/$name"
        rm -rf "$previous"
      done
      mv "$stage/deploy/desktop/astraquote-gpt-relay.service" \
        /host/astraquote/astraquote-gpt-relay.service.next
      rmdir "$stage/deploy/desktop" "$stage/deploy"
      rmdir "$stage"
      chown -R 1000:1000 "$target/backend" "$target/tools" "$target/policies" \
        /host/astraquote/astraquote-gpt-relay.service.next
    '
}

install_host_browser_relay_service() {
  echo "Installing the versioned desktop relay systemd unit"
  docker run --rm --privileged --pid=host \
    --entrypoint /usr/bin/nsenter \
    astraquote:production \
    --target 1 \
    --mount \
    --uts \
    --ipc \
    --net \
    --pid \
    --root=/proc/1/root \
    --wd=/ \
    /bin/sh -ceu '
      install -m 0644 \
        /home/ec2-user/astraquote/astraquote-gpt-relay.service.next \
        /etc/systemd/system/astraquote-gpt-relay.service
      systemctl daemon-reload
    '
}

restart_host_browser_relay() {
  echo "Restarting the desktop relay through the Docker host systemd"
  docker run --rm --privileged --pid=host \
    --entrypoint /usr/bin/nsenter \
    astraquote:production \
    --target 1 \
    --mount \
    --uts \
    --ipc \
    --net \
    --pid \
    --root=/proc/1/root \
    --wd=/ \
    /usr/bin/systemctl restart astraquote-gpt-relay.service
}

wait_for_host_browser_relay() {
  echo "Waiting for the restarted desktop relay worker to remain stable"
  docker run --rm --pid=host \
    -e RELAY_WORKER_COMMAND="$RELAY_WORKER_COMMAND" \
    --entrypoint /bin/sh \
    astraquote:production -ceu '
      for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
        candidate=""
        for cmdline in /proc/[0-9]*/cmdline; do
          test -r "$cmdline" || continue
          command=$(tr "\000" " " < "$cmdline")
          case "$command" in
            "$RELAY_WORKER_COMMAND "*)
              candidate=${cmdline#/proc/}
              candidate=${candidate%/cmdline}
              break
              ;;
          esac
        done
        if test -n "$candidate"; then
          sleep 5
          if test -r "/proc/$candidate/cmdline"; then
            command=$(tr "\000" " " < "/proc/$candidate/cmdline")
            case "$command" in
              "$RELAY_WORKER_COMMAND "*) exit 0 ;;
            esac
          fi
        else
          sleep 5
        fi
      done
      echo "AstraQuote desktop relay worker was not restarted by systemd" >&2
      exit 1
    '
}

update_host_browser_relay() {
  stage_host_browser_relay
  activate_host_browser_relay
  install_host_browser_relay_service
  restart_host_browser_relay
  wait_for_host_browser_relay
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
  if docker exec astraquote curl -fsS http://127.0.0.1:3000/api/backend/api/health >/dev/null 2>&1 \
    && docker exec astraquote curl -fsS http://127.0.0.1:8200/readyz >/dev/null 2>&1 \
    && docker exec astraquote curl -fsS http://127.0.0.1:8001/readyz >/dev/null 2>&1; then
    echo "AstraQuote container endpoints are ready"
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

echo "AstraQuote container endpoints did not become ready" >&2
docker ps -a --filter "name=^/astraquote$"
docker logs --tail 120 astraquote
exit 1
