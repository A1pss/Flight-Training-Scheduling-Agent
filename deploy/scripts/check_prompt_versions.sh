#!/usr/bin/env bash
# 提示词版本治理的执行性检查（v6 §7.7.1 第 8 行）。
#
# 用法：bash deploy/scripts/check_prompt_versions.sh
#
# 核对两件事：
#   ① prompts/ 下每份提示词的正文 sha256 与 PROMPTS.lock.json 一致；
#      正文改了而 prompt_version 没递增 → 失败。
#      （trace 与 manifest 记的是版本号；同一个版本号对应过两份正文，
#        那些 trace 就再也复现不了 —— 这是可复现性问题，不是洁癖。）
#   ② 六个 LLM 组件各自都有 system 提示词。
#
# CI 里还有一步与本脚本配套：`prompts/**` 有改动时跑 `pytest -m prompt_eval`。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

exec python deploy/scripts/prompt_lock.py check "$ROOT/prompts"
