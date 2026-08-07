#!/usr/bin/env bash
# 下载 bge-m3 与 bge-reranker-v2-m3 权重到 .data/models/，并记录 SHA256。
# 走 HF 镜像；下载后权重即离线可用，应用运行期不再出网。
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

export HF_ENDPOINT HF_HOME HUGGINGFACE_HUB_CACHE

# 本机的 HTTP(S)_PROXY (127.0.0.1:17890) 会在长连接上损坏 TLS 记录
# （curl 报 "decryption failed or bad record mac"、ollama 报 "tls: bad record MAC"），
# 大文件必然中断。直连正常，故这里显式绕过代理。
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

# hf-mirror 不提供 Xet CAS 后端（cas-server.xethub.hf.co 返回 401），
# 关掉 Xet 走普通 HTTP 下载路径。
export HF_HUB_DISABLE_XET=1

for repo in BAAI/bge-m3 BAAI/bge-reranker-v2-m3; do
  name="${repo##*/}"
  fts_info "下载 $repo → $MODELS_DIR/$name"
  conda run --no-capture-output -n "$FTS_PY_ENV" python - "$repo" "$MODELS_DIR/$name" <<'PY'
import sys, time
from huggingface_hub import snapshot_download

repo, dest = sys.argv[1], sys.argv[2]
for attempt in range(1, 11):          # 断点续传，网络抖动时重试
    try:
        snapshot_download(
            repo_id=repo, local_dir=dest,
            max_workers=4,
            ignore_patterns=[
                # onnx/openvino 等推理导出目录体积大且本项目用不到
                "*.onnx", "onnx/*", "openvino/*", "*.msgpack", "*.h5",
                # 仓库里的插图与 macOS 垃圾文件；hf-mirror 对 .DS_Store 返回 403，
                # 不排除会让整个 snapshot 下载失败
                "imgs/*", "*.DS_Store", "*.png", "*.jpg",
            ],
        )
        print("done:", dest)
        break
    except Exception as exc:
        print(f"  attempt {attempt} failed: {type(exc).__name__}: {exc}")
        if attempt == 10:
            raise
        time.sleep(5)
PY
done

fts_info "计算 SHA256 清单"
: > "$MODELS_DIR/SHA256SUMS.txt"
for name in bge-m3 bge-reranker-v2-m3; do
  ( cd "$MODELS_DIR/$name" && find . -type f ! -path './.cache/*' -print0 \
      | sort -z | xargs -0 sha256sum ) | sed "s|\./|$name/|" >> "$MODELS_DIR/SHA256SUMS.txt"
done
fts_ok "SHA256 清单已写入 $MODELS_DIR/SHA256SUMS.txt（$(wc -l < "$MODELS_DIR/SHA256SUMS.txt") 个文件）"
