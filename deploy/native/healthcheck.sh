#!/usr/bin/env bash
# deploy/native/healthcheck.sh —— 全栈体检。
#
# **每个开发窗口的开工前置**（CLAUDE.md §10 / v6 §11.1）。必须覆盖：
#   OS/CPU/内存 · GPU（只暴露 1 块卡且为物理 3 号）· conda 与 schedule 环境
#   · 磁盘余量 ≥50GB · 端口占用 · PG 可连 · Redis 可连 · Ollama 可连且
#   模型 digest 匹配 · bge 权重就位 · 外网连通性
#
# 退出码：0 = 全绿；1 = 有 FAIL 项。WARN 不影响退出码。
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"

PASS=0; WARN=0; FAIL=0
ok()   { fts_ok   "$*"; PASS=$((PASS+1)); }
warn() { fts_warn "$*"; WARN=$((WARN+1)); }
bad()  { fts_err  "$*"; FAIL=$((FAIL+1)); }
hdr()  { printf '\n\033[1m── %s ─────────────────────────────\033[0m\n' "$*"; }

MIN_DISK_GB="${MIN_DISK_GB:-50}"

# ─────────────────────────────────────────────────────────────────────
hdr "1. 操作系统与硬件"
fts_info "OS      : $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
fts_info "内核    : $(uname -r)"
fts_info "CPU     : $(nproc) 逻辑核 · $(lscpu 2>/dev/null | grep 'Model name' | sed 's/.*: *//')"
fts_info "内存    : $(free -h | awk '/^Mem:/{print $2" 总 / "$7" 可用"}')"
ok "硬件信息采集完成"

# ─────────────────────────────────────────────────────────────────────
hdr "2. GPU（只用第 4 块卡，CUDA_VISIBLE_DEVICES=3）"
if ! command -v nvidia-smi >/dev/null 2>&1; then
  bad "未找到 nvidia-smi"
else
  TOTAL_GPUS=$(nvidia-smi --list-gpus | wc -l)
  fts_info "物理 GPU 数：$TOTAL_GPUS"
  nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu \
             --format=csv,noheader | sed 's/^/   /'
  if [ "$TOTAL_GPUS" -lt 4 ]; then
    bad "物理 GPU 少于 4 块，无法定位第 4 块卡"
  else
    USED3=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i 3)
    if [ "$USED3" -le 512 ]; then
      ok "GPU 3 空闲（已用 ${USED3} MiB）"
    else
      # 常见且正常的情形：Ollama 把模型常驻显存（OLLAMA_KEEP_ALIVE=5m）。
      if curl -sf "http://$OLLAMA_HOST/api/ps" 2>/dev/null | grep -q '"name"'; then
        ok "GPU 3 已用 ${USED3} MiB —— 系 Ollama 常驻模型，属预期"
      else
        warn "GPU 3 已占用 ${USED3} MiB 且非本项目 Ollama 所持，训练/推理可能显存不足"
      fi
    fi
    # 验证屏蔽后确实只暴露 1 块，且是物理 3 号。
    # ⚠️ nvidia-smi 是驱动层工具，**不认 CUDA_VISIBLE_DEVICES**，拿它验证屏蔽
    #    永远会看到全部 4 块。必须用 CUDA runtime（torch）才测得准。
    UUID3=$(nvidia-smi --query-gpu=uuid --format=csv,noheader -i 3 | tr -d ' ')
    PROBE=$(CUDA_VISIBLE_DEVICES=3 conda run -n "$FTS_PY_ENV" python -c '
import torch
n = torch.cuda.device_count()
u = torch.cuda.get_device_properties(0).uuid if n else ""
print(f"{n}|GPU-{u}")
' 2>/dev/null | grep -v '^[[:space:]]*$' | tail -1)
    VIS="${PROBE%%|*}"; UUIDV="${PROBE##*|}"
    if [ "$VIS" = "1" ] && [ "$UUID3" = "$UUIDV" ]; then
      ok "CUDA_VISIBLE_DEVICES=3 下 CUDA 只暴露 1 块卡，且为物理 3 号（$UUID3）"
    elif [ "$VIS" = "1" ]; then
      bad "GPU 屏蔽指向了错误的卡：期望 $UUID3，实际 $UUIDV"
    else
      bad "GPU 屏蔽异常：CUDA 暴露 ${VIS:-?} 块（期望 1）"
    fi
  fi
fi

# ─────────────────────────────────────────────────────────────────────
hdr "3. conda 与 schedule 环境"
if command -v conda >/dev/null 2>&1; then
  fts_info "conda   : $(conda --version)"
  PYBIN=$(conda run -n "$FTS_PY_ENV" python -c 'import sys; print(sys.executable)' 2>/dev/null | grep -v '^[[:space:]]*$' | tail -1)
  if [ -n "$PYBIN" ]; then
    ok "环境 $FTS_PY_ENV 存在：$(conda run -n "$FTS_PY_ENV" python --version 2>&1 | grep -v '^[[:space:]]*$' | tail -1)"
    fts_info "解释器  : $PYBIN"
  else
    bad "conda 环境 $FTS_PY_ENV 不可用（检查 ~/.condarc 的 envs_dirs）"
  fi
else
  bad "未找到 conda"
fi
if [ -x "$FTS_SVC_BIN/postgres" ]; then
  ok "服务环境 fts-svc 存在：$("$FTS_SVC_BIN/postgres" --version), $("$FTS_SVC_BIN/redis-server" --version | awk '{print $1,$2,$3}')"
else
  bad "服务环境 fts-svc 不存在（$FTS_SVC_ENV_PATH）"
fi

# ─────────────────────────────────────────────────────────────────────
hdr "4. 磁盘余量（要求 ≥${MIN_DISK_GB}GB）"
AVAIL_GB=$(df -BG --output=avail "$FTS_ROOT" | tail -1 | tr -dc '0-9')
fts_info "$(df -h "$FTS_ROOT" | tail -1)"
if [ "$AVAIL_GB" -ge "$MIN_DISK_GB" ]; then
  ok "项目分区可用 ${AVAIL_GB}GB ≥ ${MIN_DISK_GB}GB"
else
  bad "项目分区仅剩 ${AVAIL_GB}GB < ${MIN_DISK_GB}GB"
fi
ROOT_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
if [ "$ROOT_GB" -lt 20 ]; then
  warn "根分区仅剩 ${ROOT_GB}GB —— 已把 HF/pip/conda/Paddle 缓存重定向到 /shares2（见 env.sh）"
fi

# ─────────────────────────────────────────────────────────────────────
hdr "5. 端口"
for spec in "5433:PostgreSQL" "6380:Redis" "11434:Ollama" "8000:FastAPI" "8501:Streamlit"; do
  port="${spec%%:*}"; name="${spec##*:}"
  if ss -ltn 2>/dev/null | grep -q ":$port "; then
    fts_info "$port ($name) 已监听"
  else
    fts_info "$port ($name) 空闲"
  fi
done
ok "端口状态采集完成"

# ─────────────────────────────────────────────────────────────────────
hdr "6. PostgreSQL 16 @ $PG_HOST:$PG_PORT"
if "$FTS_SVC_BIN/pg_isready" -h "$PG_HOST" -p "$PG_PORT" -q 2>/dev/null; then
  VER=$("$FTS_SVC_BIN/psql" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" \
        -tAc "SHOW server_version" 2>/dev/null)
  ok "PG 可连，server_version=$VER，库=$PG_DATABASE"
  SCHEMA=$("$FTS_SVC_BIN/psql" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" \
           -tAc "SELECT version_num FROM alembic_version" 2>/dev/null || true)
  if [ -n "$SCHEMA" ]; then
    fts_info "alembic schema 版本：$SCHEMA"
  else
    warn "alembic_version 表不存在 —— 迁移内容由 M1 窗口交付，M0 属预期状态"
  fi
else
  bad "PG 不可连（先跑 start_pg.sh）"
fi

# ─────────────────────────────────────────────────────────────────────
hdr "7. Redis 7 @ $REDIS_HOST:$REDIS_PORT"
if "$FTS_SVC_BIN/redis-cli" -h "$REDIS_HOST" -p "$REDIS_PORT" ping 2>/dev/null | grep -q PONG; then
  RV=$("$FTS_SVC_BIN/redis-cli" -h "$REDIS_HOST" -p "$REDIS_PORT" INFO server \
       | grep redis_version | tr -d '\r' | cut -d: -f2)
  ok "Redis 可连，version=$RV"
else
  bad "Redis 不可连（先跑 start_redis.sh）"
fi

# ─────────────────────────────────────────────────────────────────────
hdr "8. Ollama @ $OLLAMA_HOST（GPU 3）"
if curl -sf "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  ok "Ollama 可连，version=$(curl -s "http://$OLLAMA_HOST/api/version" | tr -d '{}\"')"
  MODELS=$(curl -s "http://$OLLAMA_HOST/api/tags" | tr ',' '\n' | grep -o '"name":"[^"]*"' | cut -d'"' -f4)
  if echo "$MODELS" | grep -qx "$FTS_LLM_MODEL"; then
    # Ollama v0.6.8 的 /api/show 不返回 digest，改用 manifest 文件的 sha256
    # （与 `ollama list` 的 ID 列同源）。
    MF="$OLLAMA_MODELS/manifests/registry.ollama.ai/library/${FTS_LLM_MODEL%%:*}/${FTS_LLM_MODEL##*:}"
    DIGEST=""
    [ -f "$MF" ] && DIGEST="sha256:$(sha256sum "$MF" | awk '{print $1}')"
    ok "模型 $FTS_LLM_MODEL 已就位，digest=${DIGEST:0:16}…"
    EXPECT=$(grep -E '^LLM_MODEL_DIGEST=' "$FTS_ROOT/.env.example" 2>/dev/null | cut -d= -f2)
    if [ -n "$EXPECT" ] && [ -n "$DIGEST" ] && [ "$EXPECT" != "$DIGEST" ]; then
      bad "模型 digest 与 .env.example 不符（期望 $EXPECT）—— 模型可能已被替换"
    fi
  else
    bad "模型 $FTS_LLM_MODEL 未拉取（跑 pull_models.sh）"
  fi
else
  bad "Ollama 不可连（先跑 start_ollama.sh）"
fi

# ─────────────────────────────────────────────────────────────────────
hdr "9. 本地嵌入 / 重排模型权重"
for spec in "bge-m3:BGE_M3" "bge-reranker-v2-m3:BGE_RERANKER"; do
  name="${spec%%:*}"
  if [ -d "$MODELS_DIR/$name" ] && [ -n "$(ls -A "$MODELS_DIR/$name" 2>/dev/null)" ]; then
    SZ=$(du -sh "$MODELS_DIR/$name" | cut -f1)
    ok "$name 权重就位（$SZ）"
  else
    bad "$name 权重缺失（$MODELS_DIR/$name）"
  fi
done
if [ -d "$PADDLE_HOME" ] && [ -n "$(find "$PADDLE_HOME" -name '*.pdmodel' -o -name '*.pdiparams' -o -name 'inference.*' 2>/dev/null | head -1)" ]; then
  ok "PaddleOCR 中文模型已预下载（$PADDLE_HOME）"
else
  warn "PaddleOCR 模型未预下载 —— 离线环境下摄取管线的 OCR 分支将不可用"
fi

# ─────────────────────────────────────────────────────────────────────
hdr "10. 外网连通性（仅用于装依赖与拉模型；应用代码必须全离线可运行）"
for url in https://pypi.org/simple/ https://ollama.com https://hf-mirror.com; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$url" 2>/dev/null)
  if [ "$CODE" = "200" ] || [ "$CODE" = "301" ] || [ "$CODE" = "302" ]; then
    fts_info "$url → $CODE"
  else
    fts_info "$url → $CODE（不可达）"
  fi
done
ok "外网连通性采集完成（不影响退出码）"

# ─────────────────────────────────────────────────────────────────────
printf '\n\033[1m═══ 体检汇总 ═══\033[0m\n'
printf '   PASS %d · WARN %d · FAIL %d\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
  fts_ok "全栈体检通过"
  exit 0
fi
fts_err "体检未通过，有 $FAIL 项 FAIL"
exit 1
