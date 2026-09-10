#!/bin/sh
set -eu

DATA_DIR=${ASTRAQUOTE_DATA_DIR:-/data}
CACHE_DIR=${ASTRAQUOTE_AWS_DATA_DIR:-$DATA_DIR/aws}
CATALOG_DB="$CACHE_DIR/aws_catalog.sqlite3"

mkdir -p "$CACHE_DIR" "$DATA_DIR/oauth" "$DATA_DIR/v2-quotes"
if [ ! -s "$CATALOG_DB" ] && [ -s /app/cache-seed/aws_catalog.sqlite3.gz ]; then
  gzip -dc /app/cache-seed/aws_catalog.sqlite3.gz > "$CATALOG_DB.tmp"
  mv "$CATALOG_DB.tmp" "$CATALOG_DB"
fi

cd /app/backend
uvicorn app.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

cd /app/frontend
npm run start -- --hostname 0.0.0.0 --port 3000 &
FRONTEND_PID=$!

cd /app/mcp
node server.js &
MCP_PID=$!

cd /app/oauth
uvicorn app:app --host 0.0.0.0 --port "${OAUTH_PORT:-8001}" &
OAUTH_PID=$!

shutdown() {
  kill -TERM "$BACKEND_PID" "$FRONTEND_PID" "$MCP_PID" "$OAUTH_PID" 2>/dev/null || true
  wait "$BACKEND_PID" "$FRONTEND_PID" "$MCP_PID" "$OAUTH_PID" 2>/dev/null || true
}

trap shutdown INT TERM EXIT

while kill -0 "$BACKEND_PID" 2>/dev/null \
  && kill -0 "$FRONTEND_PID" 2>/dev/null \
  && kill -0 "$MCP_PID" 2>/dev/null \
  && kill -0 "$OAUTH_PID" 2>/dev/null; do
  sleep 2
done

exit 1
