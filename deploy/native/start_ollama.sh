#!/usr/bin/env bash
# 启动 Ollama：127.0.0.1:11434，**绑死 GPU 0**（CLAUDE.md §2 硬约束）。
# 模型 blob 落在项目内 .data/ollama，不占根分区。
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

export CUDA_VISIBLE_DEVICES=0          # ← 写死，不接受覆盖

# 本机 HTTP(S)_PROXY (127.0.0.1:17890) 会损坏长连接的 TLS 记录（"tls: bad record
# MAC"），导致 `ollama pull` 反复重试、几乎拉不动模型。**服务端进程会继承这些
# 变量**，所以必须在启动脚本里清掉，只在 CLI 侧清是没用的——真正下载的是服务端。
# 应用运行期本就只连 127.0.0.1，不需要代理。
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
export NO_PROXY="localhost,127.0.0.1,::1"
export OLLAMA_HOST OLLAMA_MODELS
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-4}"

OLLAMA_BIN="$FTS_TOOLS/ollama/bin/ollama"
[ -x "$OLLAMA_BIN" ] || { fts_err "未找到 $OLLAMA_BIN，先跑 install_ollama.sh"; exit 1; }

if curl -sf "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  fts_ok "Ollama 已在 $OLLAMA_HOST 运行"; exit 0
fi

export LD_LIBRARY_PATH="$FTS_TOOLS/ollama/lib/ollama:${LD_LIBRARY_PATH:-}"
nohup "$OLLAMA_BIN" serve >"$FTS_LOGS/ollama.log" 2>&1 &
echo $! > "$FTS_RUN/ollama.pid"

for _ in $(seq 1 60); do
  curl -sf "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1 && break
  sleep 1
done
if curl -sf "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  fts_ok "Ollama 已启动 $OLLAMA_HOST (CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES)"
  fts_info "版本 $(curl -s "http://$OLLAMA_HOST/api/version")"
else
  fts_err "Ollama 启动失败，见 $FTS_LOGS/ollama.log"; exit 1
fi
