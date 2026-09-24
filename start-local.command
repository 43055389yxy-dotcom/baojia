#!/bin/zsh

set -eu

PROJECT_DIR="${0:A:h}"
MCP_DIR="$PROJECT_DIR/deploy/astraquote-mcp"
RUNTIME_DIR="$PROJECT_DIR/.astraquote"

if [[ ! -d "$MCP_DIR/node_modules" ]]; then
  echo "首次使用请先在 deploy/astraquote-mcp 目录执行 npm install。"
  exit 1
fi

if [[ ! -x "$PROJECT_DIR/backend/.venv/bin/python" ]]; then
  echo "未找到 backend/.venv/bin/python，请先安装后端 Python 依赖。"
  exit 1
fi

mkdir -p "$RUNTIME_DIR"
export ASTRAQUOTE_LOCAL_MODE=1
export ASTRAQUOTE_DELIVERY_MODE=local
export ASTRAQUOTE_MCP_HOST=127.0.0.1
export ASTRAQUOTE_MCP_PORT=8200
export ASTRAQUOTE_PUBLIC_BASE_URL="http://127.0.0.1:8200"
export ASTRAQUOTE_V2_STATE_DIR="$RUNTIME_DIR/state"
export ASTRAQUOTE_ARTIFACT_DIR="$RUNTIME_DIR/artifacts"
export ASTRAQUOTE_DOWNLOAD_DIR="$RUNTIME_DIR/downloads"

echo "AstraQuote 本地 MCP 正在启动：http://127.0.0.1:8200/mcp"
echo "不启动前端、独立后端或远程桌面。关闭这个窗口即停止 MCP。"
cd "$MCP_DIR"
exec node server.js
