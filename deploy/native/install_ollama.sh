#!/usr/bin/env bash
# 用户态解压安装 Ollama 到 .tools/ollama（不要求 root，不写系统目录）。
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# ⚠️ **版本刻意钉在 v0.6.8，不要随手升级。**
# 本机 NVIDIA 驱动为 535.230.02（CUDA 12.2）。Ollama ≥ v0.30 的 CUDA 运行时是
# 12.8，启动时直接判定 "NVIDIA driver too old, required_driver 550 or newer"，
# GPU 发现超时后**静默退化为 CPU 推理**——服务照常起来、接口照常返回，只是慢
# 十几倍，很容易被当成「跑通了」。v0.6.8 同时提供 cuda_v11(11.3) 与
# cuda_v12(12.4) 两套运行时，在 535 驱动上能正确识别到 GPU。
# 升级 Ollama 的前置条件是先把驱动升到 ≥550（需要 root）。
VERSION="${OLLAMA_VERSION:-v0.6.8}"
ARCHIVE="$FTS_TOOLS/ollama-$VERSION.tgz"
DEST="$FTS_TOOLS/ollama"

if [ ! -f "$ARCHIVE" ]; then
  fts_info "下载 Ollama $VERSION"
  # 代理会损坏长连接的 TLS 记录，这里显式绕过
  env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
      -u all_proxy -u ALL_PROXY \
    curl -fSL --retry 10 --retry-delay 3 -C - \
      -o "$ARCHIVE" \
      "https://github.com/ollama/ollama/releases/download/$VERSION/ollama-linux-amd64.tgz"
fi

fts_info "校验 SHA256（防止半截下载被当成完整包解压）"
SUMS="$FTS_TOOLS/sha256sum-$VERSION.txt"
[ -f "$SUMS" ] || env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  curl -fsSL "https://github.com/ollama/ollama/releases/download/$VERSION/sha256sum.txt" \
  -o "$SUMS"
EXPECT=$(grep 'ollama-linux-amd64.tgz' "$SUMS" | awk '{print $1}')
ACTUAL=$(sha256sum "$ARCHIVE" | awk '{print $1}')
[ "$EXPECT" = "$ACTUAL" ] || { fts_err "SHA256 不匹配：期望 $EXPECT 实际 $ACTUAL"; exit 1; }
fts_ok "SHA256 校验通过 $ACTUAL"

mkdir -p "$DEST"
tar -xzf "$ARCHIVE" -C "$DEST"
fts_ok "Ollama 已解压到 $DEST"
"$DEST/bin/ollama" --version 2>&1 | tail -1
