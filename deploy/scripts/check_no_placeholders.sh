#!/usr/bin/env bash
# 铁律 1 的执行性检查：不留半成品。
# 在 backend/ frontend/ tests/ 下扫描占位符标记，命中即失败。
#
# 用法：bash deploy/scripts/check_no_placeholders.sh
#
# ── 两处踩过的坑，改动前先读 ────────────────────────────────────────
#
# ① **只扫受版本控制的文件（`git ls-files`），不扫工作区。**
#    早先直接 `grep -r backend frontend tests`，于是 CI 里 pytest 步骤留下的
#    `__pycache__/*.pyc` 被当成命中报出来（`grep: ...pyc: binary file matches`）——
#    `.pyc` 里恰好带着那几个字节就能让 CI 红，而那和「有没有半成品」毫无关系。
#    顺带也把 `.data/`、覆盖率产物等一切未入库的东西排除在外。
#    没有 git 时退回目录扫描，但加 `-I` 跳过二进制。
#
# ② **允许逐行豁免：同一行带 `placeholder-scan: allow` 即跳过。**
#    有些文件**必须**字面包含这些标记 —— 最典型的就是「检查这些标记」的那个
#    护栏测试自己（`tests/guardrail/test_solver_isolation.py`）。
#    豁免刻意做成**逐行**而不是**按文件**：
#      · 按文件豁免 = 整个文件从此成为盲区，里面真藏了一个 TODO 也查不出来；
#      · 逐行豁免可审计 —— `rg "placeholder-scan: allow"` 一眼看全部用处，
#        且 code review 时它就摆在那一行上。
#    **它不是「让 CI 过去」的开关**：真要写半成品，写的人得在同一行显式声明豁免，
#    那在评审里藏不住。
# ────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PATTERN='TODO|FIXME|NotImplementedError|待实现|待补充|后续补'
ALLOW_MARK='placeholder-scan: allow'
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

# 受版本控制的文件清单（坑 ①）
FILES=""
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  FILES=$(git ls-files -z -- "${EXISTING[@]}" | tr '\0' '\n')
fi

if [ -n "$FILES" ]; then
  RAW=$(printf '%s\n' "$FILES" | tr '\n' '\0' \
        | xargs -0 --no-run-if-empty grep -nIE "$PATTERN" -- || true)
else
  echo "[check_no_placeholders] 不在 git 仓库内，退回目录扫描（跳过二进制）"
  RAW=$(grep -rnIE "$PATTERN" "${EXISTING[@]}" || true)
fi

# 逐行豁免（坑 ②）
HITS=$(printf '%s\n' "$RAW" | grep -vF "$ALLOW_MARK" | grep -v '^$' || true)

if [ -n "$HITS" ]; then
  echo "❌ 检出占位符（违反 CLAUDE.md 铁律 1「不留半成品」）："
  echo "$HITS"
  echo
  echo "如果某一行**必须**字面包含这些标记（例如它本身就是检查这些标记的代码），"
  echo "在该行末尾加注释 '${ALLOW_MARK}' 显式豁免 —— 逐行、可审计。"
  echo "但**不要**用它来放过真正的半成品：当前窗口范围内的每个模块都必须完整可运行。"
  exit 1
fi

echo "✅ 无 TODO / FIXME / NotImplementedError / 待实现 / 待补充 / 后续补"
