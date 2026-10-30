#!/bin/sh
# 前台运行，不启动后台管理器或分享入口；依赖安装失败即停止。
set -u
cd "$(dirname "$0")/.." || exit 1
if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' '未找到 uv。请先按 docs/本机使用.md 安装 uv，再重新启动。' >&2
    exit 127
fi
printf '%s\n' '正在准备 Python 3.13 与锁定依赖；首次运行需要联网，请等待。'
uv sync --frozen --no-dev || exit $?
printf '%s\n' '启动本机工作台；退出请在此终端按 Ctrl+C。'
exec uv run --frozen --no-dev --offline --no-env-file python scripts/run_local.py --personal "$@"
