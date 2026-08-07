#!/usr/bin/env bash
# 拉取 Ollama 模型并记录 digest（v6 §11.5「模型完整性」：digest 固定，
# healthcheck 与应用启动双重校验，防止模型被替换）。
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

bash "$(dirname "${BASH_SOURCE[0]}")/start_ollama.sh"
OLLAMA_BIN="$FTS_TOOLS/ollama/bin/ollama"
export OLLAMA_HOST

fts_info "拉取 $FTS_LLM_MODEL"
"$OLLAMA_BIN" pull "$FTS_LLM_MODEL"

# digest 取 manifest 文件的 sha256（与 `ollama list` 的 ID 列同源）——
# v0.6.8 的 /api/show 不返回 digest 字段。
MF="$OLLAMA_MODELS/manifests/registry.ollama.ai/library/${FTS_LLM_MODEL%%:*}/${FTS_LLM_MODEL##*:}"
DIGEST="sha256:$(sha256sum "$MF" | awk '{print $1}')"
fts_ok "$FTS_LLM_MODEL digest = $DIGEST"
echo "$FTS_LLM_MODEL $DIGEST" > "$FTS_DATA/model_digests.txt"
fts_info "已写入 $FTS_DATA/model_digests.txt —— 请同步到 .env.example 的 LLM_MODEL_DIGEST"
