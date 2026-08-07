#!/usr/bin/env bash
# 启动 PG16 独立实例：127.0.0.1:5433
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

if "$FTS_SVC_BIN/pg_isready" -h "$PG_HOST" -p "$PG_PORT" -q 2>/dev/null; then
  fts_ok "PG 已在 $PG_HOST:$PG_PORT 运行"; exit 0
fi

[ -f "$PGDATA/PG_VERSION" ] || { fts_err "PG 实例未初始化，先跑 init_pg.sh"; exit 1; }

"$FTS_SVC_BIN/pg_ctl" -D "$PGDATA" -l "$FTS_LOGS/pg.log" \
    -o "-p $PG_PORT -k $PGDATA -c listen_addresses=$PG_HOST" -w start

for _ in $(seq 1 30); do
  "$FTS_SVC_BIN/pg_isready" -h "$PG_HOST" -p "$PG_PORT" -q && break
  sleep 1
done
"$FTS_SVC_BIN/pg_isready" -h "$PG_HOST" -p "$PG_PORT" \
  && fts_ok "PG 已启动 $PG_HOST:$PG_PORT（日志 $FTS_LOGS/pg.log）"
