#!/usr/bin/env bash
# ⚠️ 本脚本**不在 M0 执行**，留待 W12（M7 微调窗口）再跑。
#
# 下载 Qwen2.5-14B-Instruct BF16 基座（约 28GB），供 §15 的 QLoRA 微调使用。
# 推理侧用的是 Ollama 的 q4_K_M 量化版，与本基座是两份不同的权重，不要混用。
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

DEST="$MODELS_DIR/Qwen2.5-14B-Instruct"
export HF_ENDPOINT HF_HOME HUGGINGFACE_HUB_CACHE

AVAIL_GB=$(df -BG --output=avail "$MODELS_DIR" | tail -1 | tr -dc '0-9')
if [ "$AVAIL_GB" -lt 60 ]; then
  fts_err "可用磁盘 ${AVAIL_GB}GB < 60GB，拒绝下载 28GB 基座"; exit 1
fi

fts_warn "即将下载约 28GB 的 BF16 基座到 $DEST"
fts_info "按 Ctrl-C 取消，5 秒后开始"
sleep 5

conda run --no-capture-output -n "$FTS_PY_ENV" python - "$FTS_SFT_BASE" "$DEST" <<'PY'
import sys
from huggingface_hub import snapshot_download
repo, dest = sys.argv[1], sys.argv[2]
snapshot_download(repo_id=repo, local_dir=dest,
                  ignore_patterns=["*.onnx", "*.msgpack", "*.h5", "original/*"])
print("done:", dest)
PY

fts_info "计算 SHA256"
( cd "$DEST" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) \
  > "$MODELS_DIR/Qwen2.5-14B-Instruct.SHA256SUMS.txt"
fts_ok "基座已就位：$DEST"
