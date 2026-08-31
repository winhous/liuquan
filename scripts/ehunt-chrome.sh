#!/usr/bin/env bash
# eHunt 关键词工具页调试 Chrome 启动脚本（ehunt_keyword 连接器前置件，v0.5 平移自广成）。
#
# 用途：起一个带登录态的独立 Chrome 实例，CDP 监听 127.0.0.1:9222，
#       供刘全 ehunt_keyword 连接器（engine/connectors/ehunt_keyword.py）
#       connect_over_cdp 读 ehunt.ai/cn/keyword-tool 指标。
# 登录态：持久 profile ~/.local/share/ehunt-chrome-profile（首次需人工登录一次，
#         之后常驻不丢）。配额：eHunt Free 关键词搜索每日有限次，升级 Elite 无限。
set -euo pipefail

PROFILE_DIR="${HOME}/.local/share/ehunt-chrome-profile"
PORT=9222

if curl -s --max-time 2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  echo "调试 Chrome 已在 127.0.0.1:${PORT} 运行，无需重复启动。"
  exit 0
fi

mkdir -p "${PROFILE_DIR}"
google-chrome \
  --remote-debugging-port="${PORT}" \
  --user-data-dir="${PROFILE_DIR}" \
  --no-first-run \
  --no-default-browser-check \
  >/tmp/ehunt-chrome.log 2>&1 &

for _ in $(seq 1 10); do
  if curl -s --max-time 2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
    echo "调试 Chrome 已启动：CDP=127.0.0.1:${PORT}，profile=${PROFILE_DIR}"
    echo "首次使用请在该浏览器中登录 ehunt.ai（账号沿用广成时期登录态，profile 已复用）。"
    exit 0
  fi
  sleep 1
done

echo "启动失败，日志见 /tmp/ehunt-chrome.log" >&2
exit 1
