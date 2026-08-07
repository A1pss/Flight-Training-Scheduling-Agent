#!/usr/bin/env bash
# 铁律 1 的执行性检查：不留半成品。
# 在 backend/ frontend/ tests/ 下扫描占位符标记，命中即失败。
#
# 用法：bash deploy/scripts/check_no_placeholders.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PATTERN='TODO|FIXME|NotImplementedError|待实现|待补充|后续补'
TARGETS=(backend frontend tests)

# 只扫实际存在的目录
EXISTING=()
for d in "${TARGETS[@]}"; do
  [ -d "$d" ] && EXISTING+=("$d")
done

if [ ${#EXISTING[@]} -eq 0 ]; then
  echo "[check_no_placeholders] 无可扫描目录，跳过"
  exit 0
fi

if command -v rg >/dev/null 2>&1; then
  HITS=$(rg -n "$PATTERN" "${EXISTING[@]}" || true)
else
  HITS=$(grep -rnE "$PATTERN" "${EXISTING[@]}" || true)
fi

if [ -n "$HITS" ]; then
  echo "❌ 检出占位符（违反 CLAUDE.md 铁律 1「不留半成品」）："
  echo "$HITS"
  exit 1
fi

echo "✅ 无 TODO / FIXME / NotImplementedError / 待实现 / 待补充 / 后续补"
