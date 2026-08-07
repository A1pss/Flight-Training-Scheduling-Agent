#!/usr/bin/env bash
# v6 §12.5.4 的 E2 / E3 静态扫描。
#
#   E2  全仓库 grep import requests / httpx / urllib.request
#       → 除 backend/core/http.py 外零命中，否则构建失败
#   E3  源码中的 URL 字面量
#       → 仅允许 127.0.0.1 与内网段，检出任何外部 URL 即构建失败
#
# 与 import-linter 的禁令三互为补充：import-linter 查模块依赖图，本脚本查
# 文本形态（含被注释掉又复活的写法、动态 import 字符串）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

FAIL=0
SCAN_DIRS=(backend frontend)
EXISTING=()
for d in "${SCAN_DIRS[@]}"; do [ -d "$d" ] && EXISTING+=("$d"); done
if [ ${#EXISTING[@]} -eq 0 ]; then
  echo "[check_egress] 无可扫描目录，跳过"; exit 0
fi

grep_all() {
  if command -v rg >/dev/null 2>&1; then rg -n "$1" "${EXISTING[@]}" || true
  else grep -rnE "$1" "${EXISTING[@]}" --include='*.py' || true; fi
}

# ── E2：HTTP 客户端库的 import ───────────────────────────────────────
echo "── E2 egress 库 import 扫描 ──"
E2_HITS=$(grep_all '^\s*(import|from)\s+(requests|httpx|urllib\.request)\b' \
          | grep -v '^backend/core/http.py:' || true)
if [ -n "$E2_HITS" ]; then
  echo "❌ E2 失败：除 backend/core/http.py 外检出 HTTP 客户端 import"
  echo "$E2_HITS"
  FAIL=1
else
  echo "✅ E2 通过：仅 backend/core/http.py 可 import httpx"
fi

# ── E3：URL 字面量 ───────────────────────────────────────────────────
# 允许：127.0.0.1 / localhost / ::1 / RFC1918 内网段。
echo "── E3 URL 字面量扫描 ──"
E3_HITS=$(grep_all 'https?://[A-Za-z0-9._~:/?#@!$&*+,;=%-]+' \
          | grep -vE 'https?://(127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0|10\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)' \
          || true)
if [ -n "$E3_HITS" ]; then
  echo "❌ E3 失败：源码中检出外部 URL 字面量"
  echo "$E3_HITS"
  FAIL=1
else
  echo "✅ E3 通过：源码中无外部 URL 字面量"
fi

exit "$FAIL"
