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
fts_info() { printf "   %s\n" "$*"; }
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
mkdir -p "$APP_DIR/.data" "$APP_DIR/.data/conda-pkgs" "$APP_DIR/data/plans" "$APP_DIR/traces"
ok "源码铺到 $APP_DIR（commit $(cat "$PKG_ROOT/src/COMMIT" 2>/dev/null | cut -c1-8)）"
warn "记得填 $APP_DIR/.env 的 API_TOKENS —— 留空 = 全部拒绝，不是全部放行"

# ═══════════════════════════════════════════════════════════════════
hdr "④ 建 conda 环境（--offline，只用本地包）"
# ═══════════════════════════════════════════════════════════════════
# 解释器有三条来路，**按可靠性从高到低试**。三条都以「一个装好全部依赖的
# Python 环境」收尾，差别只在解释器从哪来。
#
# ⚠️ **依赖一律来自 `wheels/`**（`pip install --no-index`），三条路共用。
# conda 本地包缓存只负责提供**解释器本身**，它在现场经常是不全的
# （宿主机清理过缓存、或者当初是联网装的），所以不能把整条安装押在它上面。
PY_BIN=""

if conda env list | awk '{print $1}' | grep -qx "$PY_ENV"; then
  # 路 ①：环境已存在（升级安装的常见情形）
  PY_BIN="$(conda run -n "$PY_ENV" python -c 'import sys;print(sys.executable)' | tr -d '\r' | tail -1)"
  ok "环境 $PY_ENV 已存在，直接用（$PY_BIN）"
elif CONDA_PKGS_DIRS="$APP_DIR/.data/conda-pkgs:$PKG_ROOT/conda/pkgs" \
     conda env create -n "$PY_ENV" -f "$PKG_ROOT/conda/environment.yml" --offline >/dev/null 2>&1; then
  # 路 ②：从包内的 conda 缓存建环境。`--offline` 是关键：**不许它偷偷联网** ——
  # 离线机上联网会挂很久，然后报一个与真实原因完全无关的错。
  #
  # ⚠️ **`CONDA_PKGS_DIRS` 的第一项必须是可写的临时目录**，包内那份放第二位：
  # conda 会把包**解压到列表里第一个可写目录**。直接指向 `$PKG_ROOT/conda/pkgs`
  # 的后果是**交付包被就地改写** —— M8 实测在包里多出 5611 个解压产物与一个
  # `cache/`，`CHECKSUMS.sha256` 当场失效。交付包必须是只读的既成事实。
  PY_BIN="$(conda run -n "$PY_ENV" python -c 'import sys;print(sys.executable)' | tr -d '\r' | tail -1)"
  ok "conda 环境 $PY_ENV 已建（用包内的本地包缓存）"
else
  # 路 ③：回落到 venv。`python -m venv` 全程不出网（pip 由 ensurepip 从内置
  # wheel 装），所以只要有一个**版本匹配的**解释器就够。
  #
  # ⚠️ **版本必须匹配到小版本**：`wheels/` 里的编译包带 ABI tag（如 cp311），
  # 装到 3.14 上会报「No matching distribution found for aiohttp」——那个报错
  # 完全看不出真实原因（M8 实测踩过这一次，回落当时抓的是 conda base 的 3.14）。
  # 所以这里**先按版本挑，挑不到就明确失败**，绝不拿一个不匹配的凑合往下装。
  warn "conda 本地包缓存不足以离线建环境，回落到 venv（依赖仍全部来自 wheels/）"
  REQ_PY="$(cat "$PKG_ROOT/conda/PYTHON_VERSION" 2>/dev/null || echo "")"
  if [ -z "$REQ_PY" ]; then
    bad "包里缺 conda/PYTHON_VERSION —— 无法判断 wheels 需要哪个 Python，拒绝盲装"
    exit 1
  fi
  fts_info "wheels 需要 Python $REQ_PY，开始找匹配的解释器"

  pick_python() {
    local candidate
    for candidate in "$@"; do
      [ -n "$candidate" ] && [ -x "$candidate" ] || continue
      if "$candidate" -c "import sys;raise SystemExit(0 if f'{sys.version_info.major}.{sys.version_info.minor}'=='$REQ_PY' else 1)" 2>/dev/null; then
        printf '%s' "$candidate"
        return 0
      fi
    done
    return 1
  }

  CONDA_ROOT="$(conda info --base 2>/dev/null | tr -d '\r' | tail -1)"
  BASE_PY="$(pick_python \
      "$(command -v "python$REQ_PY" || true)" \
      "${CONDA_ROOT:+$CONDA_ROOT/bin/python$REQ_PY}" \
      "${CONDA_ROOT:+$CONDA_ROOT/bin/python}" \
      $( [ -n "$CONDA_ROOT" ] && ls -d "$CONDA_ROOT"/envs/*/bin/"python$REQ_PY" 2>/dev/null ) \
      "$(command -v python3 || true)" \
      || true)"

  if [ -z "$BASE_PY" ]; then
    bad "本机找不到 Python $REQ_PY —— 而 wheels/ 里的编译包是为它构建的"
    printf '   可选出路（三选一）：\n'
    printf '     ① 在目标机上用 conda 建一个：conda create -n %s python=%s\n' "$PY_ENV" "$REQ_PY"
    printf '     ② 装一个系统 python%s（离线机需先备好安装介质）\n' "$REQ_PY"
    printf '     ③ 在一台装有 Python %s 的机器上重新构建交付包\n' "$REQ_PY"
    printf '   ⚠️ 不要用别的小版本硬装 —— 报错会是「找不到某个依赖」，与真实原因无关\n'
    exit 1
  fi

  VENV_DIR="$APP_DIR/.venv"
  "$BASE_PY" -m venv "$VENV_DIR"
  PY_BIN="$VENV_DIR/bin/python"
  ok "venv 已建：$VENV_DIR（解释器 $("$PY_BIN" --version)，来自 $BASE_PY）"
fi

if [ -d "$PKG_ROOT/wheels" ] && [ -n "$(ls -A "$PKG_ROOT/wheels" 2>/dev/null)" ]; then
  # ★ v6 §11.5「依赖离线」那一行的执行形态。
  # `--no-index` + `PIP_NO_INDEX=1` 双保险，保证**一次都不出网**。
  PIP_NO_INDEX=1 "$PY_BIN" -m pip install --no-index \
      --find-links="$PKG_ROOT/wheels" -r "$PKG_ROOT/conda/requirements.txt"
  ok "$("$PY_BIN" -m pip list 2>/dev/null | wc -l) 个依赖从本地 wheels 装完（--no-index，全程未出网）"
else
  bad "包里没有 wheels/ —— 离线安装缺了依赖来源，拒绝继续"
  exit 1
fi

# 后续阶段（模型 digest / 迁移 / 黄金用例）一律用这个解释器，
# **不再用 `conda run -n`** —— 路 ③ 下根本没有那个 conda 环境。
echo "$PY_BIN" > "$APP_DIR/.data/PYTHON_BIN"

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
  if ( cd "$APP_DIR" && "$PY_BIN" -m backend.core.integrity ); then
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
  ( cd "$APP_DIR" && "$PY_BIN" -m alembic upgrade head )
  ok "schema 已升到 head（$(cd "$APP_DIR" && "$PY_BIN" -m alembic current 2>/dev/null | tail -1)）"
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
if ( cd "$APP_DIR" && TZ="${TZ:-Asia/Shanghai}" LANG="${LANG:-C.UTF-8}" PYTHONHASHSEED=0 \
       "$PY_BIN" deploy/scripts/golden_fingerprint.py --json "$FP_OUT" ); then
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
