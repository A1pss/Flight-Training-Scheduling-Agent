#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# deploy/native/start_all_app.sh —— 起应用的**三个进程**（v6 §11.1 / CLAUDE.md §9）
#
#   uvicorn   API          :8000
#   rq worker 任务执行器    （无端口）
#   streamlit 前端          :8501
#
# ## ⚠️ 缺 worker 时任务永远停在 QUEUED，而且不报错
#
# M6 收工报告 §9.3 特意把这条留给 M8：API 收到排班请求后只是入队就返回 202，
# 真正跑图的是 worker。没起 worker 时前端会一直转圈、`GET /jobs` 一直是
# `QUEUED`、**没有任何错误日志** —— 因为从系统的角度看没有任何东西出错，
# 只是没人来干活。所以这个脚本把三个进程绑在一起起，且**起完真的去问一遍
# 队列里有没有 worker**（见最后的自检）。
#
# ## 为什么用 SimpleWorker
#
# `--worker-class rq.SimpleWorker`：同进程执行，不 fork。M6 §9.1 第 6 条写着
# fork 型 `Worker` **没测过**，而没测过的东西不写进交付脚本。要换先跑一遍
# `pytest tests/e2e`。
#
# ## 用法
#
# ```bash
# bash deploy/native/start_all_app.sh          # 起三个
# bash deploy/native/start_all_app.sh --api    # 只起 API（排障用）
# bash deploy/native/stop_all_app.sh           # 停
# ```
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

WHICH="${1:-all}"
mkdir -p "$FTS_LOGS" "$FTS_RUN"

APP_PORT="${APP_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-8501}"
RQ_QUEUE="${RQ_QUEUE:-fts}"
# ⚠️ **不用 `conda run`**：它会再 fork 一层，pidfile 记的是那个包装进程，
# `stop_all_app.sh` 杀掉它之后**真正的 uvicorn / rq worker 还活着**（实测在开发机
# 上留下过一批孤儿 worker，而队列自检还因此报「有 worker」）。直接用环境里的
# 可执行文件，一个 pid 对一个进程。
ENV_BIN="$(conda run -n "$FTS_PY_ENV" python -c 'import sys,pathlib;print(pathlib.Path(sys.executable).parent)' | tr -d "\r" | tail -1)"
[ -x "$ENV_BIN/python" ] || { fts_err "解析不出 $FTS_PY_ENV 的 bin 目录"; exit 1; }

# 起进程前先确认依赖服务在（起不来的原因九成在这里，早说比晚说好）
"$FTS_SVC_BIN/pg_isready" -h "$PG_HOST" -p "$PG_PORT" -q 2>/dev/null \
  || { fts_err "PG 不可连 —— 先跑 start_pg.sh"; exit 1; }
"$FTS_SVC_BIN/redis-cli" -h "$REDIS_HOST" -p "$REDIS_PORT" ping >/dev/null 2>&1 \
  || { fts_err "Redis 不可连 —— 先跑 start_redis.sh"; exit 1; }

# 迁移没跑过的话，集成路径会以 `relation "data_snapshots" does not exist` 全线失败
if ! "$FTS_SVC_BIN/psql" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" \
      -tAc "SELECT 1 FROM alembic_version LIMIT 1" >/dev/null 2>&1; then
  fts_err "数据库尚未迁移 —— 先跑：conda run -n $FTS_PY_ENV alembic upgrade head"
  exit 1
fi

if [ -z "${API_TOKENS:-}" ] && ! grep -qE '^API_TOKENS=.+' "$FTS_ROOT/.env" 2>/dev/null; then
  fts_warn "API_TOKENS 为空 —— 全部请求会被 401 拒绝（这是设计行为，不是 bug）"
fi

start_one() {
  local name="$1"; shift
  local pidfile="$FTS_RUN/$name.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    fts_ok "$name 已在运行（pid $(cat "$pidfile")）"; return 0
  fi
  nohup "$@" > "$FTS_LOGS/$name.log" 2>&1 &
  echo $! > "$pidfile"
  fts_ok "$name 已启动（pid $!，日志 $FTS_LOGS/$name.log）"
}

case "$WHICH" in
  all|--all)
    start_one api "$ENV_BIN/uvicorn" backend.api.main:app --host "$APP_HOST" --port "$APP_PORT"
    start_one worker "$ENV_BIN/rq" worker \
        --url "redis://$REDIS_HOST:$REDIS_PORT" --worker-class rq.SimpleWorker "$RQ_QUEUE"
    start_one frontend "$ENV_BIN/streamlit" run frontend/app.py \
        --server.port "$FRONTEND_PORT" --server.address 0.0.0.0
    ;;
  --api)      start_one api "$ENV_BIN/uvicorn" backend.api.main:app --host "$APP_HOST" --port "$APP_PORT" ;;
  --worker)   start_one worker "$ENV_BIN/rq" worker --url "redis://$REDIS_HOST:$REDIS_PORT" --worker-class rq.SimpleWorker "$RQ_QUEUE" ;;
  --frontend) start_one frontend "$ENV_BIN/streamlit" run frontend/app.py --server.port "$FRONTEND_PORT" --server.address 0.0.0.0 ;;
  *) echo "用法：$0 [all|--api|--worker|--frontend]" >&2; exit 2 ;;
esac

# ── 自检：worker 真的连上队列了吗 ────────────────────────────────────
# 这一步是整个脚本存在的理由。`nohup ... &` 成功只说明进程被 fork 出来了，
# 说明不了它有没有连上 Redis、有没有注册到队列上。
if [ "$WHICH" = "all" ] || [ "$WHICH" = "--all" ] || [ "$WHICH" = "--worker" ]; then
  for _ in $(seq 1 20); do
    COUNT=$("$ENV_BIN/python" - <<'PY' 2>/dev/null | tail -1
import os
from datetime import datetime, timedelta, timezone

from redis import Redis
from rq import Worker

# ⚠️ **不能只数 `Worker.all()` 的长度**：Redis 里会留着已经死掉的 worker 的
# 注册键（TTL 没到期），实测一台开发机上数出过 8 个而实际只有 1 个活的。
# 那样这条自检就会在「worker 其实没起来」时照样绿 —— 而它存在的全部理由
# 就是抓这种情况。判据改成「心跳在 60 秒以内」。
conn = Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]))
queue = os.environ.get("RQ_QUEUE", "fts")
fresh = datetime.now(timezone.utc) - timedelta(seconds=60)
alive = 0
for worker in Worker.all(connection=conn):
    beat = worker.last_heartbeat
    if beat is None:
        continue
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    if beat >= fresh and queue in worker.queue_names():
        alive += 1
print(alive)
PY
)
    [ "${COUNT:-0}" -ge 1 ] && break
    sleep 1
  done
  if [ "${COUNT:-0}" -ge 1 ]; then
    fts_ok "队列 $RQ_QUEUE 上有 ${COUNT} 个**心跳新鲜的** worker —— 提交的任务会被执行"
  else
    fts_err "队列上没有 worker —— **任务会永远停在 QUEUED 且不报错**。看 $FTS_LOGS/worker.log"
    exit 1
  fi
fi

fts_ok "API http://$PG_HOST:$APP_PORT/docs · 前端 http://$PG_HOST:$FRONTEND_PORT"
