#!/bin/bash
set -euo pipefail

APP_DIR="${WORKSPACE:?Jenkins workspace is unavailable}"
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

update_caddy_route() {
  echo "Updating the AstraQuote MCP and download routes"
  docker run --rm -i \
    -v /home/ec2-user/caddy-gateway/managed:/host/caddy-managed \
    --entrypoint /bin/sh \
    astraquote:production -ceu '
      umask 022
      target=/host/caddy-managed/astraquote.caddy
      next=/host/caddy-managed/astraquote.caddy.next
      previous=/host/caddy-managed/astraquote.caddy.previous
      cat > "$next"
      rm -f "$previous"
      if test -f "$target"; then
        cp -p "$target" "$previous"
      fi
      mv "$next" "$target"
    ' < "$APP_DIR/deploy/caddy-astraquote.caddy"

  if ! docker exec caddy-gateway \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
    docker run --rm \
      -v /home/ec2-user/caddy-gateway/managed:/host/caddy-managed \
      --entrypoint /bin/sh \
      astraquote:production -ceu '
        target=/host/caddy-managed/astraquote.caddy
        previous=/host/caddy-managed/astraquote.caddy.previous
        test -f "$previous"
        mv "$previous" "$target"
      '
    echo "The AstraQuote Caddy route was invalid and has been rolled back" >&2
    return 1
  fi

  docker exec caddy-gateway \
    caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
  docker run --rm \
    -v /home/ec2-user/caddy-gateway/managed:/host/caddy-managed \
    --entrypoint /bin/sh \
    astraquote:production -ceu \
    'rm -f /host/caddy-managed/astraquote.caddy.previous'
}

retire_legacy_quote_relays() {
  echo "Stopping the retired remote GPT and desktop quote relays"
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
    /bin/sh -ceu '
      systemctl disable --now astraquote-gpt-relay.service 2>/dev/null || true
      systemctl disable --now astraquote-gemini-relay.service 2>/dev/null || true
    '

  if docker inspect astraquote-chatgpt-desktop >/dev/null 2>&1; then
    docker stop astraquote-chatgpt-desktop >/dev/null 2>&1 || true
  fi
  stale_relays="$(docker ps -aq --filter label=com.astraquote.codex-relay=true)"
  if test -n "$stale_relays"; then
    docker rm -f $stale_relays >/dev/null
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
  if docker exec astraquote curl -fsS http://127.0.0.1:8200/readyz >/dev/null 2>&1 \
    && docker exec astraquote curl -fsS http://127.0.0.1:8001/readyz >/dev/null 2>&1 \
    && docker exec astraquote /bin/sh -ceu '
      test -n "$ASTRAQUOTE_INTERNAL_TOKEN"
      curl -fsS --max-time 10 \
        -H "Authorization: Bearer $ASTRAQUOTE_INTERNAL_TOKEN" \
        -H "Accept: application/json, text/event-stream" \
        -H "Content-Type: application/json" \
        --data "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{},\"clientInfo\":{\"name\":\"jenkins-health\",\"version\":\"1\"}}}" \
        http://127.0.0.1:8200/v2/mcp >/dev/null
    '; then
    echo "AstraQuote MCP and OAuth gateway are ready"
    verify_oauth_database_continuity
    update_caddy_route
    retire_legacy_quote_relays
    docker container prune -f --filter "label=com.docker.compose.project=astraquote"
    docker image prune -a -f --filter "label=com.astraquote.image=true"
    echo "AstraQuote single-MCP deployment succeeded"
    exit 0
  fi
  sleep 5
done

echo "AstraQuote MCP container did not become ready" >&2
docker ps -a --filter "name=^/astraquote$"
docker logs --tail 160 astraquote
exit 1
