#!/usr/bin/env bash
# 启动 Redis7：127.0.0.1:6380
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

if "$FTS_SVC_BIN/redis-cli" -h "$REDIS_HOST" -p "$REDIS_PORT" ping >/dev/null 2>&1; then
  fts_ok "Redis 已在 $REDIS_HOST:$REDIS_PORT 运行"; exit 0
fi

"$FTS_SVC_BIN/redis-server" \
    --port "$REDIS_PORT" --bind "$REDIS_HOST" \
    --dir "$REDIS_DATA" --dbfilename fts.rdb \
    --appendonly no --save "900 1" \
    --pidfile "$FTS_RUN/redis.pid" \
    --logfile "$FTS_LOGS/redis.log" \
    --daemonize yes

for _ in $(seq 1 30); do
  "$FTS_SVC_BIN/redis-cli" -h "$REDIS_HOST" -p "$REDIS_PORT" ping >/dev/null 2>&1 && break
  sleep 1
done
"$FTS_SVC_BIN/redis-cli" -h "$REDIS_HOST" -p "$REDIS_PORT" ping \
  && fts_ok "Redis 已启动 $REDIS_HOST:$REDIS_PORT（日志 $FTS_LOGS/redis.log）"
