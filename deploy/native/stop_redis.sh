#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
if "$FTS_SVC_BIN/redis-cli" -h "$REDIS_HOST" -p "$REDIS_PORT" ping >/dev/null 2>&1; then
  "$FTS_SVC_BIN/redis-cli" -h "$REDIS_HOST" -p "$REDIS_PORT" shutdown nosave 2>/dev/null || true
  fts_ok "Redis 已停止"
else
  fts_warn "Redis 未在运行"
fi
