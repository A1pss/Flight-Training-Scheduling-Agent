#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
if "$FTS_SVC_BIN/pg_isready" -h "$PG_HOST" -p "$PG_PORT" -q 2>/dev/null; then
  "$FTS_SVC_BIN/pg_ctl" -D "$PGDATA" -m fast -w stop && fts_ok "PG 已停止"
else
  fts_warn "PG 未在运行"
fi
