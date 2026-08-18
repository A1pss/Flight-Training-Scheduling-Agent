#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# install.sh —— 离线机的一键安装（v6 §11.4）
#
# > 环境体检（CPU/内存/显存/**磁盘余量 ≥50 GB**）→ 建 conda 环境 → 校验 checksum
# > → 裸装 PG/Redis/Ollama → 初始化数据库 → 导入模型 → **自动跑一遍黄金用例，
# > 绿灯才算安装成功**
#
# 八个阶段严格按上面的顺序，**任一阶段失败即停**（`set -e`）。最后那条尤其重要：
# 装完不跑用例的安装脚本，回答不了「这台机器现在能不能排班」这个唯一重要的问题。
#
# ## 全程不需要 root
#
# PG/Redis/Ollama 全部用户态运行（CLAUDE.md §2 / v6 §11.1）。唯一要 root 的是
# compose 路径的 `iptables_egress_drop.sh`，那个**不在本脚本里**，由运维单独执行。
#
# ## 用法
#
# ```bash
# bash scripts/install.sh --prefix /opt/fts            # 全量安装
# bash scripts/install.sh --prefix /opt/fts --dry-run  # 只体检 + 校验，不动系统
# bash scripts/install.sh --prefix /opt/fts --skip-services   # 只装环境与代码
# ```
#
# 退出码：0 = 装完且黄金用例全绿；非 0 = 见最后一行的失败阶段。
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${FTS_PREFIX:-/opt/fts}"
PY_ENV="${FTS_PY_ENV:-schedule}"
MIN_DISK_GB="${MIN_DISK_GB:-50}"
MIN_RAM_GB="${MIN_RAM_GB:-16}"
MIN_CPU="${MIN_CPU:-4}"
DRY_RUN=0
SKIP_SERVICES=0
STAGE="启动"

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --env) PY_ENV="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-services) SKIP_SERVICES=1; shift ;;
    --help|-h)
      sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
done

GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { printf "${GREEN}✅ %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}⚠️  %s${NC}\n" "$*"; }
bad()  { printf "${RED}❌ %s${NC}\n" "$*" >&2; }
hdr()  { STAGE="$*"; printf "\n${BOLD}── %s ─────────────────────────────${NC}\n" "$*"; }
trap 'bad "安装在「${STAGE}」阶段失败"' ERR

printf "${BOLD}FTS 飞行训练排班系统 · 离线安装${NC}\n"
printf "   包目录：%s\n   安装到：%s\n   conda 环境：%s\n" "$PKG_ROOT" "$PREFIX" "$PY_ENV"
[ "$DRY_RUN" -eq 1 ] && warn "dry-run：只做体检与校验，不改动系统"

# ═══════════════════════════════════════════════════════════════════
hdr "① 环境体检"
# ═══════════════════════════════════════════════════════════════════
CPUS=$(nproc)
RAM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
# 装到哪就查哪个分区的余量。`df` 对不存在的目录会失败，所以先取最近的已存在祖先。
CHECK_DIR="$PREFIX"
while [ ! -d "$CHECK_DIR" ] && [ "$CHECK_DIR" != "/" ]; do CHECK_DIR="$(dirname "$CHECK_DIR")"; done
DISK_GB=$(df -BG --output=avail "$CHECK_DIR" | tail -1 | tr -dc '0-9')

printf "   CPU %s 核 · 内存 %s GB · %s 可用 %s GB\n" "$CPUS" "$RAM_GB" "$CHECK_DIR" "$DISK_GB"
FAILED=0
[ "$CPUS" -ge "$MIN_CPU" ] && ok "CPU ≥ ${MIN_CPU} 核" || { bad "CPU 少于 ${MIN_CPU} 核"; FAILED=1; }
[ "$RAM_GB" -ge "$MIN_RAM_GB" ] && ok "内存 ≥ ${MIN_RAM_GB} GB" || { bad "内存少于 ${MIN_RAM_GB} GB"; FAILED=1; }
# ★ v6 §11.4 明确要求的那一条
if [ "$DISK_GB" -ge "$MIN_DISK_GB" ]; then
  ok "磁盘余量 ${DISK_GB} GB ≥ ${MIN_DISK_GB} GB"
else
  bad "磁盘余量 ${DISK_GB} GB < ${MIN_DISK_GB} GB（v6 §11.4 硬要求）"
  FAILED=1
fi

# 显存：**没有 GPU 不算失败**。排班链路一次都不经 LLM（v6 §0），
# 没卡时 LLM_PROVIDER 退到 mock 照样能排班 —— 那正是 FTS-4001 降级路径的意义。
if command -v nvidia-smi >/dev/null 2>&1; then
  VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | sort -rn | head -1)
  if [ "${VRAM:-0}" -ge 20000 ]; then
    ok "显存 ${VRAM} MiB（14B-Q4 推理需 ~17 GB）"
  else
    warn "最大显存 ${VRAM:-0} MiB < 20 GB —— 本地 14B 推理跑不动，排班本身不受影响"
  fi
else
  warn "没有 nvidia-smi —— 无 GPU 模式：LLM_PROVIDER 请设为 mock，排班能力完整保留"
fi

command -v conda >/dev/null 2>&1 && ok "conda：$(conda --version)" \
  || { bad "找不到 conda（需先装 Miniconda）"; FAILED=1; }
[ "$FAILED" -eq 0 ] || { bad "体检未通过，安装中止"; exit 1; }

# ═══════════════════════════════════════════════════════════════════
hdr "② 校验 CHECKSUMS.sha256"
# ═══════════════════════════════════════════════════════════════════
# **在动系统之前校验**：装到一半才发现包是坏的，清理成本比重下一次还高。
if [ ! -f "$PKG_ROOT/CHECKSUMS.sha256" ]; then
  bad "缺 CHECKSUMS.sha256 —— 这个包不完整，拒绝安装"
  exit 1
fi
( cd "$PKG_ROOT" && sha256sum -c CHECKSUMS.sha256 --quiet ) \
  && ok "$(wc -l < "$PKG_ROOT/CHECKSUMS.sha256") 个文件校验通过" \
  || { bad "校验失败 —— 包在传输中损坏或被改动过"; exit 1; }

if [ "$DRY_RUN" -eq 1 ]; then
  hdr "dry-run 结束"
  ok "体检与校验都通过。去掉 --dry-run 即可真装。"
  exit 0
fi

# ═══════════════════════════════════════════════════════════════════
hdr "③ 铺开源码"
# ═══════════════════════════════════════════════════════════════════
APP_DIR="$PREFIX/FTS_PLANAU"
mkdir -p "$APP_DIR"
cp -a "$PKG_ROOT/src/." "$APP_DIR/"
[ -f "$APP_DIR/.env" ] || cp "$PKG_ROOT/.env.example" "$APP_DIR/.env"
mkdir -p "$APP_DIR/.data" "$APP_DIR/data/plans" "$APP_DIR/traces"
ok "源码铺到 $APP_DIR（commit $(cat "$PKG_ROOT/src/COMMIT" 2>/dev/null | cut -c1-8)）"
warn "记得填 $APP_DIR/.env 的 API_TOKENS —— 留空 = 全部拒绝，不是全部放行"

# ═══════════════════════════════════════════════════════════════════
hdr "④ 建 conda 环境（--offline，只用本地包）"
# ═══════════════════════════════════════════════════════════════════
if conda env list | awk '{print $1}' | grep -qx "$PY_ENV"; then
  ok "环境 $PY_ENV 已存在，跳过创建"
else
  # `--offline` 是这一步的关键：**不许它偷偷联网**。离线机上联网会挂很久
  # 然后报一个与真实原因无关的错。
  CONDA_PKGS_DIRS="$PKG_ROOT/conda/pkgs" \
    conda env create -n "$PY_ENV" -f "$PKG_ROOT/conda/environment.yml" --offline
  ok "conda 环境 $PY_ENV 已建"
fi

if [ -d "$PKG_ROOT/wheels" ] && [ -n "$(ls -A "$PKG_ROOT/wheels" 2>/dev/null)" ]; then
  # ★ v6 §11.5「依赖离线」那一行的执行形态。`--no-index` 保证**一次都不出网**。
  conda run -n "$PY_ENV" pip install --no-index --find-links="$PKG_ROOT/wheels" \
      -r "$PKG_ROOT/conda/requirements.txt"
  ok "依赖从本地 wheels 装完（--no-index，全程未出网）"
else
  warn "包里没有 wheels/，跳过 pip 安装（环境里应已自带）"
fi

# ═══════════════════════════════════════════════════════════════════
hdr "⑤ 导入模型"
# ═══════════════════════════════════════════════════════════════════
if [ -d "$PKG_ROOT/models" ] && [ -n "$(ls -A "$PKG_ROOT/models" 2>/dev/null)" ]; then
  [ -d "$PKG_ROOT/models/ollama" ] && cp -a "$PKG_ROOT/models/ollama/." "$APP_DIR/.data/ollama/" 2>/dev/null || true
  mkdir -p "$APP_DIR/.data/models"
  for name in bge-m3 bge-reranker-v2-m3; do
    [ -d "$PKG_ROOT/models/$name" ] && cp -a "$PKG_ROOT/models/$name" "$APP_DIR/.data/models/"
  done
  [ -d "$PKG_ROOT/models/paddleocr" ] && cp -a "$PKG_ROOT/models/paddleocr/." "$APP_DIR/.data/paddleocr/" 2>/dev/null || true
  ok "模型已导入 $APP_DIR/.data"
  # 导入完立刻验 digest（v6 §11.5「模型完整性」）：**在这里发现被换掉，
  # 比在第一次排班时发现便宜得多**。
  if ( cd "$APP_DIR" && conda run -n "$PY_ENV" python -m backend.core.integrity ); then
    ok "模型 digest 校验通过"
  else
    bad "模型 digest 不匹配 —— 包里的模型与 .env 里钉的 digest 对不上"
    exit 1
  fi
else
  warn "包里没有 models/，跳过（LLM_PROVIDER 请设为 mock）"
fi

# ═══════════════════════════════════════════════════════════════════
hdr "⑥ 裸装服务（PG 16 / Redis 7 / Ollama）"
# ═══════════════════════════════════════════════════════════════════
if [ "$SKIP_SERVICES" -eq 1 ]; then
  warn "跳过（--skip-services）"
else
  export FTS_ROOT="$APP_DIR"
  for script in init_pg.sh start_pg.sh start_redis.sh; do
    if [ -f "$PKG_ROOT/native/$script" ]; then
      ( cd "$APP_DIR" && bash "$PKG_ROOT/native/$script" ) && ok "$script 完成"
    else
      warn "缺 native/$script"
    fi
  done
  if [ -f "$PKG_ROOT/native/start_ollama.sh" ] && command -v nvidia-smi >/dev/null 2>&1; then
    ( cd "$APP_DIR" && bash "$PKG_ROOT/native/start_ollama.sh" ) && ok "Ollama 已起"
  else
    warn "跳过 Ollama（没有 GPU 或缺脚本）—— 排班能力不依赖它"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
hdr "⑦ 初始化数据库"
# ═══════════════════════════════════════════════════════════════════
if [ "$SKIP_SERVICES" -eq 1 ]; then
  warn "跳过（--skip-services）"
else
  ( cd "$APP_DIR" && conda run -n "$PY_ENV" alembic upgrade head )
  ok "schema 已升到 head（$(cd "$APP_DIR" && conda run -n "$PY_ENV" alembic current 2>/dev/null | tail -1)）"
fi

# ═══════════════════════════════════════════════════════════════════
hdr "⑧ 黄金用例（★ 绿灯才算安装成功）"
# ═══════════════════════════════════════════════════════════════════
# v6 §11.4 的最后一步。**这一步不过就不算装上了** —— 前面七步全绿只说明
# 「东西都放对了地方」，只有这一步回答「这台机器现在能不能排出合规的班」。
#
# 跑的是 40 个合成黄金场景（约 10 秒，纯 CPU，不碰 PG / 不碰 LLM），
# 它同时给出 `golden_fingerprint` —— 与交付方在出厂机上记录的那个比一比，
# 就知道这台机器跑出来的结果与出厂时是否逐字节相同。
FP_OUT="$APP_DIR/.data/install_golden.json"
if ( cd "$APP_DIR" && conda run -n "$PY_ENV" python deploy/scripts/golden_fingerprint.py --json "$FP_OUT" ); then
  FP=$(sed -n 's/.*"golden_fingerprint": "\([0-9a-f]*\)".*/\1/p' "$FP_OUT" | head -1)
  ok "黄金用例 40 个全绿"
  printf "   golden_fingerprint = ${BOLD}%s${NC}\n" "$FP"
  printf "   （与出厂记录比对：不同则说明这台机器的环境有差异，先查 TZ / LANG / SOLVER_SEED）\n"
else
  bad "黄金用例未通过 —— **安装不算成功**，不要投入使用"
  exit 1
fi

hdr "安装完成"
ok "起服务：cd $APP_DIR && bash deploy/native/start_all_app.sh"
ok "体检：  bash deploy/native/healthcheck.sh"
printf "   ${YELLOW}下一步必做${NC}：填 .env 的 API_TOKENS（散列形态见管理员手册 §2）\n"
