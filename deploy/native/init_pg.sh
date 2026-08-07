#!/usr/bin/env bash
# initdb 在项目目录下起独立 PG16 实例（非系统服务，不要求 root），并建库建角色。
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

if [ -f "$PGDATA/PG_VERSION" ]; then
  fts_warn "PG 实例已存在于 $PGDATA（版本 $(cat "$PGDATA/PG_VERSION")），跳过 initdb"
else
  fts_info "initdb → $PGDATA"
  mkdir -p "$PGDATA"
  chmod 700 "$PGDATA"
  "$FTS_SVC_BIN/initdb" -D "$PGDATA" -U "$PG_USER" \
      --auth-local=trust --auth-host=trust -E UTF8 --locale=C >/dev/null
  fts_ok "initdb 完成"
fi

bash "$(dirname "${BASH_SOURCE[0]}")/start_pg.sh"

if "$FTS_SVC_BIN/psql" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres \
     -tAc "SELECT 1 FROM pg_database WHERE datname='$PG_DATABASE'" | grep -q 1; then
  fts_warn "数据库 $PG_DATABASE 已存在"
else
  "$FTS_SVC_BIN/createdb" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_DATABASE"
  fts_ok "已建库 $PG_DATABASE"
fi

"$FTS_SVC_BIN/psql" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" \
    -c "SELECT version();"
