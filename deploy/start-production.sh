#!/bin/sh
set -eu

DATA_DIR=${ASTRAQUOTE_DATA_DIR:-/data}
mkdir -p "$DATA_DIR/oauth" "$DATA_DIR/v2-quotes/artifacts" "$DATA_DIR/downloads"

cd /app/mcp
node server.js &
MCP_PID=$!

cd /app/oauth
uvicorn app:app --host 0.0.0.0 --port "${OAUTH_PORT:-8001}" &
OAUTH_PID=$!

shutdown() {
  kill -TERM "$MCP_PID" "$OAUTH_PID" 2>/dev/null || true
  wait "$MCP_PID" "$OAUTH_PID" 2>/dev/null || true
}

trap shutdown INT TERM EXIT

while kill -0 "$MCP_PID" 2>/dev/null \
  && kill -0 "$OAUTH_PID" 2>/dev/null; do
  sleep 2
done

exit 1
