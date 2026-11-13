#!/usr/bin/env bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
    echo "[!] 未找到 python3,请先安装 Python 3.10+"
    exit 1
fi
echo "[1/2] 安装依赖..."
python3 -m pip install -q -r requirements.txt
echo "[2/2] 启动面板与代理..."
python3 main.py
