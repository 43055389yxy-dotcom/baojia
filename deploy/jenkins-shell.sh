#!/bin/bash
set -euo pipefail

APP_DIR="${WORKSPACE:?Jenkins workspace is unavailable}"
RELAY_HOST_ROOT="/home/ec2-user/astraquote"
RELAY_STAGE_NAME=".relay-stage-${GIT_COMMIT:-manual}"
RELAY_WORKER_COMMAND="/home/ec2-user/astraquote/gpt-relay-venv/bin/python /home/ec2-user/astraquote/source/tools/gpt_quote_relay_worker.py"
CODEX_CONTAINER="astraquote-chatgpt-desktop"
CODEX_IMAGE="astraquote/chatgpt-desktop:26.908.40834-zh"
OAUTH_CLIENT_COUNT_BEFORE=""

oauth_client_count() {
  docker exec astraquote python -c '
import sqlite3
connection = sqlite3.connect("file:/data/oauth/oauth.db?mode=ro", uri=True)
try:
    print(connection.execute("SELECT count(*) FROM clients").fetchone()[0])
finally:
    connection.close()
'
}

snapshot_oauth_database() {
  if ! docker inspect astraquote >/dev/null 2>&1 \
    || ! docker exec astraquote test -f /data/oauth/oauth.db; then
    echo "No running OAuth database to snapshot before this deployment"
    return 0
  fi

  OAUTH_CLIENT_COUNT_BEFORE="$(oauth_client_count)"
  case "$OAUTH_CLIENT_COUNT_BEFORE" in
    ''|*[!0-9]*)
      echo "Could not verify the existing OAuth client registry" >&2
      return 1
      ;;
  esac

  oauth_backup_path="/data/oauth/backups/oauth-predeploy-$(date -u +%Y%m%dT%H%M%SZ).db"
  docker exec \
    -e OAUTH_BACKUP_PATH="$oauth_backup_path" \
    astraquote python -c '
import os
import sqlite3
from pathlib import Path

source = sqlite3.connect("file:/data/oauth/oauth.db?mode=ro", uri=True)
target_path = Path(os.environ["OAUTH_BACKUP_PATH"])
target_path.parent.mkdir(parents=True, exist_ok=True)
target = sqlite3.connect(target_path)
try:
    source.backup(target)
finally:
    target.close()
    source.close()
target_path.chmod(0o600)
'
  echo "OAuth registry snapshot saved before container replacement"
}

verify_oauth_database_continuity() {
  if test -z "$OAUTH_CLIENT_COUNT_BEFORE"; then
    return 0
  fi
  oauth_client_count_after="$(oauth_client_count)"
  case "$oauth_client_count_after" in
    ''|*[!0-9]*)
      echo "The deployed OAuth client registry could not be verified" >&2
      return 1
      ;;
  esac
  if (( oauth_client_count_after < OAUTH_CLIENT_COUNT_BEFORE )); then
    echo "OAuth client registry lost entries during deployment; refusing to report success" >&2
    return 1
  fi
}

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
      deploy/desktop/relay-requirements.txt \
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
        test -f "$stage/deploy/desktop/relay-requirements.txt"
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
      mv "$stage/deploy/desktop/relay-requirements.txt" \
        /host/astraquote/relay-requirements.txt.next
      rmdir "$stage/deploy/desktop" "$stage/deploy"
      rmdir "$stage"
      chown -R 1000:1000 "$target/backend" "$target/tools" "$target/policies" \
        /host/astraquote/astraquote-gpt-relay.service.next \
        /host/astraquote/relay-requirements.txt.next
    '
}

host_codex_cdp_ready() {
  # Jenkins itself runs in a container, so its 127.0.0.1 is not the Docker
  # host.  Probe Codex from a short-lived container sharing the host network.
  docker run --rm --network host \
    --entrypoint /usr/bin/curl \
    astraquote:production \
    -fsS --max-time 3 http://127.0.0.1:9222/json/list
}

ensure_host_codex_chat_desktop() {
  echo "Ensuring the authenticated Codex Chat desktop is running"
  if ! docker image inspect "$CODEX_IMAGE" >/dev/null 2>&1; then
    echo "Required Codex desktop image is missing: $CODEX_IMAGE" >&2
    return 1
  fi

  recreate=0
  if ! docker inspect "$CODEX_CONTAINER" >/dev/null 2>&1; then
    recreate=1
  else
    configured_image="$(docker inspect "$CODEX_CONTAINER" --format '{{.Config.Image}}')"
    configured_shm="$(docker inspect "$CODEX_CONTAINER" --format '{{.HostConfig.ShmSize}}')"
    configured_network="$(docker inspect "$CODEX_CONTAINER" --format '{{.HostConfig.NetworkMode}}')"
    configured_command="$(docker inspect "$CODEX_CONTAINER" --format '{{json .Config.Cmd}}')"
    configured_binds="$(docker inspect "$CODEX_CONTAINER" --format '{{json .HostConfig.Binds}}')"
    if test "$configured_image" != "$CODEX_IMAGE" \
      || test "$configured_shm" -lt 1073741824 \
      || test "$configured_network" != host \
      || [[ "$configured_command" != *'codex://threads/new?mode=chat'* ]] \
      || [[ "$configured_command" != *'--remote-debugging-port=9222'* ]] \
      || [[ "$configured_binds" != *'/home/ec2-user/.chatgpt-desktop-home:/home/chatgpt'* ]]; then
      recreate=1
    fi
  fi

  previous_name="${CODEX_CONTAINER}-pre-${GIT_COMMIT:-manual}"
  if test "$recreate" -eq 1; then
    if docker inspect "$CODEX_CONTAINER" >/dev/null 2>&1; then
      docker stop "$CODEX_CONTAINER" >/dev/null
      docker rename "$CODEX_CONTAINER" "$previous_name"
    fi
    if ! docker run -d \
      --name "$CODEX_CONTAINER" \
      --user 1000:1000 \
      --network host \
      --shm-size 1g \
      --restart unless-stopped \
      -e DISPLAY=:1 \
      -e HOME=/home/chatgpt \
      -e XAUTHORITY=/home/chatgpt/.Xauthority \
      -e XDG_RUNTIME_DIR=/run/user/1000 \
      -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
      -e LANG=zh_CN.UTF-8 \
      -e LC_ALL=zh_CN.UTF-8 \
      -e LANGUAGE=zh_CN:zh \
      -v /home/ec2-user/.chatgpt-desktop-home:/home/chatgpt \
      -v /home/ec2-user/.Xauthority:/home/chatgpt/.Xauthority:ro \
      -v /tmp/.X11-unix:/tmp/.X11-unix \
      -v /run/user/1000:/run/user/1000 \
      -v /home/ec2-user/.local/libexec/chatgpt-xdg-open:/usr/bin/xdg-open:ro \
      "$CODEX_IMAGE" \
      /usr/bin/chatgpt \
      --ozone-platform=x11 \
      --disable-gpu \
      --no-sandbox \
      --lang=zh-CN \
      --remote-debugging-address=127.0.0.1 \
      --remote-debugging-port=9222 \
      --remote-allow-origins=http://127.0.0.1:9222 \
      --force-renderer-accessibility \
      'codex://threads/new?mode=chat'; then
      docker rm -f "$CODEX_CONTAINER" >/dev/null 2>&1 || true
      if docker inspect "$previous_name" >/dev/null 2>&1; then
        docker rename "$previous_name" "$CODEX_CONTAINER"
        docker start "$CODEX_CONTAINER" >/dev/null
      fi
      return 1
    fi
  else
    docker start "$CODEX_CONTAINER" >/dev/null
  fi

  for attempt in {1..30}; do
    if host_codex_cdp_ready \
      | grep -F 'app://-/index.html' >/dev/null; then
      return 0
    fi
    sleep 2
  done
  echo "Codex Chat desktop did not expose its local control endpoint" >&2
  docker logs --tail 100 "$CODEX_CONTAINER" || true
  if test "$recreate" -eq 1; then
    docker rm -f "$CODEX_CONTAINER" >/dev/null 2>&1 || true
    if docker inspect "$previous_name" >/dev/null 2>&1; then
      docker rename "$previous_name" "$CODEX_CONTAINER"
      docker start "$CODEX_CONTAINER" >/dev/null
    fi
  fi
  return 1
}

install_host_relay_dependencies() {
  echo "Installing the versioned desktop relay dependencies"
  /home/ec2-user/astraquote/gpt-relay-venv/bin/pip install --disable-pip-version-check \
    -r /home/ec2-user/astraquote/relay-requirements.txt.next
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
    /usr/bin/systemctl enable --now astraquote-gpt-relay.service
}

diagnose_host_browser_relay() {
  echo "Desktop relay service diagnostics"
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
      systemctl --no-pager --full status astraquote-gpt-relay.service || true
      journalctl --no-pager -u astraquote-gpt-relay.service -n 120 || true
    '
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
  ensure_host_codex_chat_desktop
  install_host_relay_dependencies
  install_host_browser_relay_service
  restart_host_browser_relay
  if ! wait_for_host_browser_relay; then
    diagnose_host_browser_relay
    return 1
  fi
}

for config_file in \
  /home/ec2-user/astraquote/config/backend.env \
  /home/ec2-user/astraquote/config/mcp.env \
  /home/ec2-user/astraquote/config/oauth.env; do
  test -f "$config_file"
done
test -d /home/ec2-user/astraquote/data

snapshot_oauth_database

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
    verify_oauth_database_continuity
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
