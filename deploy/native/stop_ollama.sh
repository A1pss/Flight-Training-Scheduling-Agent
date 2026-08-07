#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
if [ -f "$FTS_RUN/ollama.pid" ] && kill -0 "$(cat "$FTS_RUN/ollama.pid")" 2>/dev/null; then
  kill "$(cat "$FTS_RUN/ollama.pid")" && rm -f "$FTS_RUN/ollama.pid"
  fts_ok "Ollama 已停止"
else
  pkill -f "ollama serve" 2>/dev/null && fts_ok "Ollama 已停止" || fts_warn "Ollama 未在运行"
fi
