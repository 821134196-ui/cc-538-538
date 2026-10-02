#!/usr/bin/env bash
# 一条本地命令：初始化 SQLite、预置演示数据（含移栽案例）、启动服务。
# 用法：./run.sh [端口]
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8000}"
DB="data/park.db"

PY="$(command -v python3 || command -v python)"
echo ">> 使用 $($PY --version)"
echo ">> 初始化数据库并预置数据：$DB （含 T006 国槐移栽案例）"
exec $PY -u -m app.main --port "$PORT" --db "$DB" --photos data/photos
