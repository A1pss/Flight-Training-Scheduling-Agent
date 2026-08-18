#!/usr/bin/env bash
# 停应用的三个进程（与 start_all_app.sh 配对）。
#
# 只停**本脚本起的**那些（按 pidfile），不 `pkill -f uvicorn` —— 那会连同一台机器上
# 别人的 uvicorn 一起杀掉，而这是共享开发机。
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

for name in frontend worker api; do
  pidfile="$FTS_RUN/$name.pid"
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      # 先 TERM：worker 收到后会跑完手上那个任务再退，避免留下半截的排班运行
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
      fts_ok "$name 已停（pid $pid）"
    else
      fts_info "$name 未在运行"
    fi
    rm -f "$pidfile"
  else
    fts_info "$name 没有 pidfile"
  fi
done
