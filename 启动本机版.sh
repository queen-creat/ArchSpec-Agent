#!/bin/sh
# 从解压目录调用共同前台入口；不读取开发机后台配置。
cd "$(dirname "$0")" || exit 1
exec sh scripts/start_local.sh "$@"
