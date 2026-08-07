#!/usr/bin/env bash
# deploy/native/env.sh —— 所有裸装脚本的公共环境定义。
# 由其余脚本 `source` 引入，不单独执行。
#
# 设计要点：**所有缓存与数据目录都指向 /shares2**。本机根分区 `/` 只剩
# 27G（99% 占用），HF/pip/Paddle 的默认缓存都在 `~`，落在根分区上——
# 拉一个 14B 模型就能把它打爆。

# shellcheck disable=SC2155
export FTS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# ── conda 环境 ───────────────────────────────────────────────────────
# schedule：Python 应用环境（CLAUDE.md §2 硬约束）
# fts-svc ：PG16 / Redis7 的二进制环境。**刻意与 schedule 分开**——
#           postgresql/redis 会拖进 openssl/icu/krb5 等一堆 C 库，装进
#           schedule 有打断 torch/paddle 既有依赖的风险。
export FTS_PY_ENV="${FTS_PY_ENV:-schedule}"
export FTS_SVC_ENV_PATH="${FTS_SVC_ENV_PATH:-/shares2/mingde/miniconda3/envs/fts-svc}"
export FTS_SVC_BIN="$FTS_SVC_ENV_PATH/bin"

# ── 数据与工具目录（全部在 /shares2）───────────────────────────────
export FTS_DATA="$FTS_ROOT/.data"
export FTS_TOOLS="$FTS_ROOT/.tools"
export FTS_LOGS="$FTS_DATA/logs"
export FTS_RUN="$FTS_DATA/run"

export PGDATA="$FTS_DATA/pg"
export REDIS_DATA="$FTS_DATA/redis"
export OLLAMA_MODELS="$FTS_DATA/ollama"
export MODELS_DIR="$FTS_DATA/models"

# ── 缓存重定向（根分区只剩 27G，绝不能用默认的 ~/.cache）───────────
export HF_HOME="${HF_HOME:-/shares2/mingde/.hf-cache}"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-/shares2/mingde/.pip-cache}"
export CONDA_PKGS_DIRS="${CONDA_PKGS_DIRS:-/shares2/mingde/.conda-pkgs}"
export PADDLE_HOME="$FTS_DATA/paddleocr"
export PADDLE_PDX_CACHE_HOME="$FTS_DATA/paddleocr"
export PADDLEOCR_HOME="$FTS_DATA/paddleocr"

# ── 服务端口（避让系统既有服务，v6 §11.1）──────────────────────────
export PG_HOST="${PG_HOST:-127.0.0.1}"
export PG_PORT="${PG_PORT:-5433}"
export PG_USER="${PG_USER:-fts}"
export PG_DATABASE="${PG_DATABASE:-fts}"
export REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
export REDIS_PORT="${REDIS_PORT:-6380}"
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"

# ── GPU：只用第 4 块卡（CLAUDE.md §2 硬约束）────────────────────────
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-3}"

# ── 模型 ─────────────────────────────────────────────────────────────
export FTS_LLM_MODEL="${FTS_LLM_MODEL:-qwen2.5:14b-instruct-q4_K_M}"
export FTS_SFT_BASE="${FTS_SFT_BASE:-Qwen/Qwen2.5-14B-Instruct}"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

mkdir -p "$FTS_LOGS" "$FTS_RUN" "$MODELS_DIR" "$OLLAMA_MODELS" "$REDIS_DATA"

# 统一的着色输出
fts_ok()   { printf '\033[32m✅ %s\033[0m\n' "$*"; }
fts_warn() { printf '\033[33m⚠️  %s\033[0m\n' "$*"; }
fts_err()  { printf '\033[31m❌ %s\033[0m\n' "$*"; }
fts_info() { printf '   %s\n' "$*"; }
