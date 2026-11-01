#!/bin/sh
# 首次自动准备官方 uv 与依赖，个人资料仍只保存在本机。
set -u
cd "$(dirname "$0")/.." || exit 1
UV_CMD=""
if [ -x "$PWD/.tools/uv" ]; then UV_CMD="$PWD/.tools/uv"
elif command -v uv >/dev/null 2>&1; then UV_CMD="$(command -v uv)"
elif [ -n "${HOME:-}" ] && [ -x "$HOME/.local/bin/uv" ]; then UV_CMD="$HOME/.local/bin/uv"
fi
if [ -z "$UV_CMD" ]; then
    printf '%s\n' '首次启动：正在从 uv 官方网站下载运行工具，需要联网。'
    mkdir -p .tools || exit 1
    installer="$(mktemp "${TMPDIR:-/tmp}/archspec-uv.XXXXXXXX")" || exit 1
    if ! curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 https://astral.sh/uv/0.13.6/install.sh -o "$installer"; then
        rm -f "$installer"; printf '%s\n' '下载失败，请检查网络后重新双击启动。' >&2; exit 1
    fi
    if ! UV_UNMANAGED_INSTALL="$PWD/.tools" sh "$installer"; then
        rm -f "$installer"; printf '%s\n' '运行工具安装失败，请检查网络后重试。' >&2; exit 1
    fi
    rm -f "$installer"
    UV_CMD="$PWD/.tools/uv"
    if [ ! -x "$UV_CMD" ]; then printf '%s\n' '运行工具未安装成功，请重试。' >&2; exit 1; fi
fi
printf '%s\n' '正在准备运行环境，首次可能需要几分钟，请保持此窗口打开。'
"$UV_CMD" sync --frozen --no-dev || { printf '%s\n' '依赖准备失败，请检查网络后重新启动。' >&2; exit 1; }
printf '%s\n' '请在设置窗口中填写自己的 DeepSeek Key。使用期间请保持此窗口打开。'
exec "$UV_CMD" run --frozen --no-dev --offline --no-env-file python scripts/run_local.py --personal --easy-start --open-browser "$@"
