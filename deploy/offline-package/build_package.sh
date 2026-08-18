#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# deploy/offline-package/build_package.sh —— 组装离线交付包（v6 §11.4）
#
# 产物结构（与 v6 §11.4 那张图逐行对应）：
#
#   fts-release-v1.0.0/
#   ├── native/      ★ 裸装脚本集（主路径）：pg/ redis/ ollama/ install.sh
#   ├── compose/     docker-compose.yml + 分环境 env 模板（备选路径）
#   ├── images/      docker save 的镜像 tar（仅 compose 路径需要）
#   ├── models/      Ollama 模型 blob + bge 权重 + PaddleOCR 模型
#   ├── wheels/      pip download 的全部依赖 wheel（含 ortools）
#   ├── conda/       environment.yml + 本地包缓存
#   ├── sql/         建表 + Alembic 迁移
#   ├── rules/       ruleset_v1.3.yaml + semantics.yaml
#   ├── skills/      知识层 markdown
#   ├── templates/   Excel 模板 + 版式基准抽取清单
#   ├── src/         应用源码（离线机上没有 Git）
#   ├── scripts/install.sh
#   └── CHECKSUMS.sha256
#
# ## 分层构建：大件可以跳过
#
# 模型权重约 14 GB、wheels 约 6 GB，重新收集一次要几十分钟。所以每个大件都有
# 独立开关，改一行脚本不必重打整包：
#
# ```bash
# bash build_package.sh                      # 全量
# bash build_package.sh --skip-models        # 不收模型（调脚本时用）
# bash build_package.sh --skip-wheels --skip-models --skip-images   # 只打「小件」
# ```
#
# **`CHECKSUMS.sha256` 始终按实际打进去的内容生成**：跳过大件时清单里就没有它们，
# 而不是留一批算不出来的条目。install.sh 校验时缺项即失败 —— 一个「部分校验通过」
# 的交付包比没有校验更危险。
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VERSION="${FTS_RELEASE_VERSION:-1.0.0}"
OUT_ROOT="${FTS_RELEASE_DIR:-$ROOT/.release}"
PKG="$OUT_ROOT/fts-release-v$VERSION"
PY_ENV="${FTS_PY_ENV:-schedule}"
# 项目自己的环境变量（尤其 `CONDA_PKGS_DIRS` 指向 /shares2 上的包缓存）。
# 不 source 它的话，下面收集 conda 本地包时会漏掉那一份。
# shellcheck disable=SC1091
[ -f "$ROOT/deploy/native/env.sh" ] && . "$ROOT/deploy/native/env.sh" >/dev/null 2>&1 || true

ENV_BIN="$(conda run -n "$PY_ENV" python -c 'import sys,pathlib;print(pathlib.Path(sys.executable).parent)' | tr -d "\r" | tail -1)"
[ -x "$ENV_BIN/python" ] || { echo "❌ 解析不出 $PY_ENV 的 bin 目录" >&2; exit 1; }

SKIP_MODELS=0; SKIP_WHEELS=0; SKIP_IMAGES=0; SKIP_CONDA_PKGS=0
for arg in "$@"; do
  case "$arg" in
    --skip-models) SKIP_MODELS=1 ;;
    --skip-wheels) SKIP_WHEELS=1 ;;
    --skip-images) SKIP_IMAGES=1 ;;
    --skip-conda-pkgs) SKIP_CONDA_PKGS=1 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

hdr() { printf '\n\033[1m── %s ─────────────────────────────\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }

hdr "输出目录"
note "$PKG"
mkdir -p "$PKG"/{native,compose,images,models,wheels,conda,sql,rules,skills,templates,src,scripts,docs}

# ── ① 脚本与配置（小件，永远重打）──────────────────────────────────
hdr "① 脚本 · 规则 · 知识层 · 模板"
cp -a deploy/native/. "$PKG/native/"
cp -a deploy/compose/. "$PKG/compose/"
# 现场的 env 由运维按模板填；**打包时不带任何 .env**（v6 §11.5 机密管理）
rm -f "$PKG"/compose/env/*.env
cp -a rules/. "$PKG/rules/"
cp -a skills/. "$PKG/skills/"
cp -a templates/. "$PKG/templates/"
cp -a alembic "$PKG/sql/alembic"
cp -a alembic.ini "$PKG/sql/"
cp -a deploy/scripts "$PKG/scripts/checks"
cp -a deploy/offline-package/scripts/install.sh "$PKG/scripts/install.sh"
cp -a .env.example "$PKG/"
note "native/ compose/ rules/ skills/ templates/ sql/ scripts/ 就位"

# ── ② 源码（离线机上没有 Git）────────────────────────────────────────
hdr "② 应用源码"
# 用 `git archive` 而不是 `cp -a`：**只带入库的文件**，自动排除 .data/、
# data/plans/、.env、模型权重这些绝不该进交付包的东西（v6 §11.5）。
git archive --format=tar HEAD | tar -x -C "$PKG/src"
note "src/ = git archive HEAD（$(find "$PKG/src" -type f | wc -l) 个文件）"
git rev-parse HEAD > "$PKG/src/COMMIT"

# ── ③ conda 环境导出 ────────────────────────────────────────────────
hdr "③ conda 环境"
conda env export -n "$PY_ENV" --no-builds > "$PKG/conda/environment.yml" 2>/dev/null \
  || conda env export -n "$PY_ENV" > "$PKG/conda/environment.yml"
cp requirements.txt requirements.in "$PKG/conda/" 2>/dev/null || true
note "environment.yml（$(grep -c '^\s*-' "$PKG/conda/environment.yml") 条依赖）"
if [ "$SKIP_CONDA_PKGS" -eq 0 ]; then
  # ⚠️ **用 python 解析 `conda info --json`，不要用 sed**：`pkgs_dirs` 是个数组，
  # 而 conda 的 JSON 缩进形态会随版本变 —— 实测那条 sed 一个都没匹配到，
  # 于是「跳过本地包缓存」被静默打印出来，包里少了 1.5 GB 而构建照样成功。
  # **收集类的步骤失败必须像失败**，所以这里把「一个都没收到」也当异常报出来。
  #
  # 多个候选目录全都扫：`CONDA_PKGS_DIRS`（env.sh 设的）与 conda 自己报的那几个
  # 可能不是同一个，两边都可能有包。
  mkdir -p "$PKG/conda/pkgs"
  # ⚠️ 用 `$ENV_BIN/python` 而不是 `conda run`：**`conda run` 不转发 stdin**，
  # 于是这里的 heredoc 送不进去，脚本拿到一个空的候选目录列表
  # （实测踩过，与 `start_all_app.sh` 用直接可执行文件是同一条理由）。
  CANDIDATE_DIRS="$("$ENV_BIN/python" - <<'PY' 2>/dev/null
import json
import os
import subprocess

dirs = []
for item in (os.environ.get("CONDA_PKGS_DIRS") or "").split(os.pathsep):
    if item.strip():
        dirs.append(item.strip())
try:
    info = json.loads(subprocess.run(["conda", "info", "--json"], capture_output=True, text=True, check=True).stdout)
    dirs.extend(info.get("pkgs_dirs") or [])
except Exception:
    pass
seen = set()
for item in dirs:
    if item and item not in seen and os.path.isdir(item):
        seen.add(item)
        print(item)
PY
)"
  COPIED_PKGS=0
  for dir in $CANDIDATE_DIRS; do
    while IFS= read -r pkgfile; do
      [ -n "$pkgfile" ] || continue
      cp -n "$pkgfile" "$PKG/conda/pkgs/" 2>/dev/null && COPIED_PKGS=$((COPIED_PKGS + 1))
    done <<EOF
$(find "$dir" -maxdepth 1 \( -name '*.conda' -o -name '*.tar.bz2' \) 2>/dev/null)
EOF
  done
  TOTAL_PKGS=$(find "$PKG/conda/pkgs" -type f | wc -l)
  if [ "$TOTAL_PKGS" -eq 0 ]; then
    echo "❌ conda 本地包缓存一个都没收到（扫过：${CANDIDATE_DIRS:-无})" >&2
    echo "   离线机上 conda env create --offline 会失败。要么修好这一步，" >&2
    echo "   要么显式 --skip-conda-pkgs 表示「这个包不含 conda 缓存」。" >&2
    exit 1
  fi
  note "本地包缓存 $TOTAL_PKGS 个（$(du -sh "$PKG/conda/pkgs" | cut -f1)），来自：$(echo "$CANDIDATE_DIRS" | tr '\n' ' ')"

  # ★ 记录 wheels 对应的 Python 版本。**这是离线安装能不能成的关键前提**：
  # `wheels/` 里的编译包带 ABI tag（cp311），装到别的小版本上会以
  # 「No matching distribution found」失败 —— 而那个报错完全看不出真实原因
  # （M8 实测踩过：venv 回落用了 conda base 的 3.14，报的是 aiohttp 找不到）。
  "$ENV_BIN/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' \
    > "$PKG/conda/PYTHON_VERSION"
  REQ_PY="$(cat "$PKG/conda/PYTHON_VERSION")"
  note "wheels 对应 Python $REQ_PY（写入 conda/PYTHON_VERSION）"

  # 目标机上如果还没有 conda 环境，install.sh 需要一个匹配的解释器。它有三个
  # 来源：本包的 conda 缓存里带 python 包、目标机已有 conda 环境、系统 python。
  # 第一个是唯一**我们能保证**的那个，所以缺了要说出来。
  if ! find "$PKG/conda/pkgs" -maxdepth 1 -name "python-${REQ_PY}*" | grep -q .; then
    note "⚠️ conda 缓存里**没有** python-$REQ_PY 包"
    note "   → 目标机必须自带 Python $REQ_PY（Miniconda base、已有 conda 环境或系统 python）"
    note "   → 想让包自带：在一台 conda 缓存里有 python-$REQ_PY 的机器上重新构建"
  fi
else
  note "跳过 conda 本地包缓存（--skip-conda-pkgs）"
fi

# ── ④ wheels ────────────────────────────────────────────────────────
hdr "④ pip wheels"
if [ "$SKIP_WHEELS" -eq 0 ]; then
  # 已经有内容就不重下（几 GB，重下一次几十分钟）
  if [ -z "$(ls -A "$PKG/wheels" 2>/dev/null)" ]; then
    PIP_CACHE_DIR="${PIP_CACHE_DIR:-/shares2/mingde/.pip-cache}" \
      conda run -n "$PY_ENV" pip download -r requirements.txt -d "$PKG/wheels" --no-input
  fi
  note "$(find "$PKG/wheels" -type f | wc -l) 个包（$(du -sh "$PKG/wheels" | cut -f1)）"
else
  note "跳过（--skip-wheels）"
fi

# ── ⑤ 模型 ──────────────────────────────────────────────────────────
hdr "⑤ 模型权重"
if [ "$SKIP_MODELS" -eq 0 ]; then
  for spec in ".data/ollama:ollama" ".data/models/bge-m3:bge-m3" \
              ".data/models/bge-reranker-v2-m3:bge-reranker-v2-m3" ".data/paddleocr:paddleocr"; do
    src="${spec%%:*}"; name="${spec##*:}"
    if [ -d "$ROOT/$src" ]; then
      mkdir -p "$PKG/models/$name"
      # `cp -a -u`：已经拷过的不再拷。13 GB 复制一次好几分钟，而重跑构建
      # （改了脚本、重算 checksum）是常事。
      cp -au "$ROOT/$src/." "$PKG/models/$name/"
      note "$name（$(du -sh "$PKG/models/$name" | cut -f1)）"
    else
      note "⚠️ $src 不存在，跳过 $name"
    fi
  done
else
  note "跳过（--skip-models）"
fi

# ── ⑥ 镜像（仅 compose 路径需要）────────────────────────────────────
hdr "⑥ 容器镜像"
if [ "$SKIP_IMAGES" -eq 0 ] && command -v docker >/dev/null 2>&1; then
  IMAGE="${FTS_BASE_IMAGE:-fts-base:local}"
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker save "$IMAGE" -o "$PKG/images/fts-base.tar"
    note "$IMAGE → images/fts-base.tar（$(du -h "$PKG/images/fts-base.tar" | cut -f1)）"
  else
    note "⚠️ 本机没有 $IMAGE，先跑 deploy/compose/build_base_image.sh"
  fi
else
  note "跳过（--skip-images 或没有 docker）"
fi

# ── ⑦ 手册 ──────────────────────────────────────────────────────────
hdr "⑦ 文档"
for doc in 部署手册.md 用户手册.md 管理员手册.md; do
  [ -f "docs/$doc" ] && cp "docs/$doc" "$PKG/docs/" && note "$doc"
done
[ -f docs/openapi.json ] && cp docs/openapi.json "$PKG/docs/" && note "openapi.json"

# ── ⑧ CHECKSUMS ─────────────────────────────────────────────────────
hdr "⑧ CHECKSUMS.sha256"
cd "$PKG"
# `CHECKSUMS.sha256` 自己不进清单（自指的校验和算不出来）。
find . -type f ! -name CHECKSUMS.sha256 -print0 \
  | sort -z \
  | xargs -0 sha256sum > CHECKSUMS.sha256
note "$(wc -l < CHECKSUMS.sha256) 个文件"
cd "$ROOT"

hdr "完成"
note "包体积：$(du -sh "$PKG" | cut -f1)"
note "校验：cd $PKG && sha256sum -c CHECKSUMS.sha256 --quiet"
note "安装：bash $PKG/scripts/install.sh --help"
