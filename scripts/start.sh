#!/usr/bin/env bash
# ============================================
# workbuddy2api-python 一键启动脚本 (Linux/macOS)
# ============================================
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
    echo "[i] 未检测到虚拟环境，正在创建..."
    python3 -m venv .venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    echo "[i] 正在安装依赖..."
    python -m pip install --upgrade pip
    pip install -r requirements.txt
else
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

if [ ! -f "config.json" ]; then
    echo "[i] 未检测到 config.json，从模板创建..."
    cp config.example.json config.json
    echo "[!] 请先编辑 config.json 修改 api_key，然后再运行本脚本。"
    exit 1
fi

echo "[i] 启动服务..."
exec python cli/server.py -config config.json
