#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# deploy/scripts/compare_deploy_paths.sh
#
# **M8 出口标准**（v6 §11.4 + §13）：
#
# > native 与 compose 两条路径产出的 `content_sha256` 逐字节相同
#
# 做法是让两条路径各跑一遍 `golden_fingerprint.py`（40 个黄金用例的
# content_sha256 聚合成一个数），然后比。不同时逐个用例列出差异。
#
# ## 为什么比的是黄金用例而不是基准周
#
# 见 `golden_fingerprint.py` 的模块注释：黄金用例全部落在**可复现的终态**
# （OPTIMAL / INFEASIBLE），而基准周单次求解 20 秒且在机器忙时可能落到
# `FEASIBLE` —— 那个状态按 v6 §3.11.1 **不保证逐字节可复现**，拿它比两条路径
# 会得到一个「有时红有时绿」的门禁。会飘的门禁比没有门禁更糟。
#
# ## 两条路径之外，还比一遍配置
#
# 指纹相同只说明「这次跑出来一样」。`--config-only` 另外逐项比对两边**影响
# 确定性的环境项**（seed / workers / 规则与语义版本 / 时区 / locale）——
# 那是「为什么会一样」的解释。两者都要有：只比指纹，某天两边同时改错了同一项
# 还是会一致；只比配置，配置一样也可能因为别的原因跑出不同结果。
#
# ## 用法
#
# ```bash
# bash deploy/scripts/compare_deploy_paths.sh              # 两条路径都跑
# bash deploy/scripts/compare_deploy_paths.sh --native-only
# bash deploy/scripts/compare_deploy_paths.sh --config-only
# ```
#
# 退出码：0 = 两条路径指纹一致；1 = 不一致或某条路径跑不起来。
# ─────────────────────────────────────────────────────────────────────
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PY_ENV="${FTS_PY_ENV:-schedule}"
COMPOSE_DIR="$ROOT/deploy/compose"
OUT_DIR="${FTS_COMPARE_OUT:-$ROOT/.release/compare}"
mkdir -p "$OUT_DIR"

MODE="${1:-both}"

#: 影响 content_sha256 的环境项。两条路径在这些项上必须逐字相同。
DETERMINISM_KEYS=(SOLVER_SEED SOLVER_WORKERS SOLVER_TIME_LIMIT_S RULESET_PATH SEMANTICS_PATH TZ LANG)

hdr() { printf '\n\033[1m── %s ─────────────────────────────\033[0m\n' "$*"; }

run_native() {
  hdr "native 路径（裸装，本项目主路径）"
  conda run -n "$PY_ENV" python deploy/scripts/golden_fingerprint.py \
        --per-case --json "$OUT_DIR/native.json" | tee "$OUT_DIR/native.txt"
  return "${PIPESTATUS[0]}"
}

run_compose() {
  hdr "compose 路径（交付备选）"
  if ! command -v docker >/dev/null 2>&1; then
    echo "❌ 没有 docker，compose 路径跑不了" >&2
    return 1
  fi
  # prod 优先，没有就用 dev（开发机上验一致性时用的就是 dev）。
  local env_name="prod"
  if [ ! -f "$COMPOSE_DIR/env/prod.env" ]; then
    if [ -f "$COMPOSE_DIR/env/dev.env" ]; then
      env_name="dev"
      echo "   （用 env/dev.env —— 确定性五项与 prod 逐字相同）"
    else
      echo "❌ 缺 env 文件：先 cp deploy/compose/env/dev.env.example deploy/compose/env/dev.env" >&2
      return 1
    fi
  fi
  # 镜像里不装依赖，conda 环境与仓库都是 bind-mount 进去的（见 Dockerfile 注释）。
  # 因此这里必须把宿主的三个绝对路径传进去，且容器内挂载点与宿主完全相同。
  FTS_ENV="$env_name" \
  FTS_REPO_DIR="$ROOT" \
  FTS_CONDA_ENV_DIR="$(conda run -n "$PY_ENV" python -c 'import sys,pathlib;print(pathlib.Path(sys.executable).parent.parent)' | tail -1)" \
  docker compose -f "$COMPOSE_DIR/docker-compose.yml" --profile verify \
      run --rm golden | tee "$OUT_DIR/compose.txt"
  return "${PIPESTATUS[0]}"
}

compare_config() {
  hdr "确定性配置逐项比对"
  local native_env="$ROOT/.env"
  [ -f "$native_env" ] || native_env="$ROOT/.env.example"
  local compose_env="$COMPOSE_DIR/env/prod.env"
  [ -f "$compose_env" ] || compose_env="$COMPOSE_DIR/env/prod.env.example"
  echo "native  : $native_env"
  echo "compose : $compose_env"
  local bad=0
  for key in "${DETERMINISM_KEYS[@]}"; do
    local a b
    a=$(grep -E "^${key}=" "$native_env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"')
    b=$(grep -E "^${key}=" "$compose_env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"')
    # 路径类的只比 basename：两条路径的仓库根目录本来就可能不同，
    # 要比的是「用的是不是同一份规则文件」，不是「装在哪个盘」。
    case "$key" in
      RULESET_PATH|SEMANTICS_PATH) a="${a##*/}"; b="${b##*/}" ;;
    esac
    if [ "$a" = "$b" ]; then
      printf '   ✅ %-22s %s\n' "$key" "${a:-（两边都未设，取代码默认）}"
    else
      printf '   ❌ %-22s native=%s  compose=%s\n' "$key" "${a:-∅}" "${b:-∅}"
      bad=1
    fi
  done
  return "$bad"
}

extract_fp() { grep -oE 'golden_fingerprint=[0-9a-f]{64}' "$1" | tail -1 | cut -d= -f2; }

FAIL=0
case "$MODE" in
  --config-only) compare_config || FAIL=1 ;;
  --native-only) run_native || FAIL=1 ;;
  both|"")
    compare_config || FAIL=1
    run_native || FAIL=1
    if run_compose; then
      NAT=$(extract_fp "$OUT_DIR/native.txt")
      CMP=$(extract_fp "$OUT_DIR/compose.txt")
      hdr "指纹比对"
      echo "   native  = ${NAT:-∅}"
      echo "   compose = ${CMP:-∅}"
      if [ -n "$NAT" ] && [ "$NAT" = "$CMP" ]; then
        echo "✅ 两条路径的黄金用例指纹逐字节相同"
      else
        echo "❌ 两条路径指纹不同 —— 离线交付不可信，先查上面的配置比对"
        diff <(grep -E '^g[0-9]' "$OUT_DIR/native.txt") \
             <(grep -E '^g[0-9]' "$OUT_DIR/compose.txt") || true
        FAIL=1
      fi
    else
      echo "❌ compose 路径未能执行" >&2
      FAIL=1
    fi
    ;;
  *) echo "用法：$0 [--native-only|--config-only]" >&2; exit 2 ;;
esac

exit "$FAIL"
