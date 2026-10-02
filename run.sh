#!/usr/bin/env bash
# 一条本地命令：检查 Python 3 → 初始化 SQLite 并预置移栽案例 → 启动服务。
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "需要 Python 3.10+"; exit 1; }

if [[ "$*" != *"--port"* ]]; then
  set -- "$@" --port "${PORT:-8000}"
fi
echo "▶ 公园树木认养系统 → http://127.0.0.1:${PORT:-8000}/"
exec "$PY" main.py "$@"
