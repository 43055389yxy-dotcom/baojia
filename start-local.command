#!/bin/zsh

set -u

PROJECT_DIR="/Users/shier/Documents/ChatGPT/baojia"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
RUN_DIR="$PROJECT_DIR/.run"
BACKEND_PORT=8000
FRONTEND_PORT=3000

mkdir -p "$RUN_DIR"

echo "AstraQuote 本地开发版"
echo "当前代码版本：$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || echo 未知)"
echo "后端已开启自动更新：代码保存后会自动重启。"
echo ""

backend_started=0
frontend_started=0
backend_pid=""
frontend_pid=""

if lsof -nP -iTCP:"$BACKEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "后端已经在运行，不重复启动。"
else
  cd "$BACKEND_DIR" || exit 1
  .venv/bin/uvicorn app.aws_main:app \
    --host 127.0.0.1 \
    --port "$BACKEND_PORT" \
    --reload \
    --reload-dir app \
    >> "$RUN_DIR/backend.log" 2>&1 &
  backend_pid=$!
  backend_started=1
  echo "$backend_pid" > "$RUN_DIR/backend.pid"
  echo "后端已启动。"
fi

if lsof -nP -iTCP:"$FRONTEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "页面已经在运行，不重复启动。"
else
  cd "$FRONTEND_DIR" || exit 1
  npm run dev -- --host localhost --port "$FRONTEND_PORT" \
    >> "$RUN_DIR/frontend.log" 2>&1 &
  frontend_pid=$!
  frontend_started=1
  echo "$frontend_pid" > "$RUN_DIR/frontend.pid"
  echo "页面已启动。"
fi

sleep 1
open "http://localhost:$FRONTEND_PORT/"

if (( backend_started == 0 && frontend_started == 0 )); then
  echo "程序已是运行状态，可以直接使用。"
  exit 0
fi

cleanup() {
  if (( backend_started == 1 )) && [[ -n "$backend_pid" ]]; then
    kill "$backend_pid" 2>/dev/null || true
  fi
  if (( frontend_started == 1 )) && [[ -n "$frontend_pid" ]]; then
    kill "$frontend_pid" 2>/dev/null || true
  fi
}

trap cleanup INT TERM EXIT
echo "请保持这个窗口打开；关闭窗口会停止本次启动的程序。"
wait
