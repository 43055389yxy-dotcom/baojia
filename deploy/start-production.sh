#!/bin/sh
set -eu

CACHE_DIR=/app/backend/.cache
CATALOG_DB="$CACHE_DIR/aws_catalog.sqlite3"

mkdir -p "$CACHE_DIR"
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

shutdown() {
  kill -TERM "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
}

trap shutdown INT TERM EXIT

while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 2
done

exit 1
