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
  CONDA_PKGS="$(conda info --json 2>/dev/null | sed -n 's/.*"pkgs_dirs": \[\s*"\([^"]*\)".*/\1/p' | head -1)"
  if [ -n "${CONDA_PKGS:-}" ] && [ -d "$CONDA_PKGS" ]; then
    mkdir -p "$PKG/conda/pkgs"
    # 只带 .conda/.tar.bz2 包本身，不带解压后的目录（那是重复的一份）
    find "$CONDA_PKGS" -maxdepth 1 \( -name '*.conda' -o -name '*.tar.bz2' \) \
      -exec cp -n {} "$PKG/conda/pkgs/" \; 2>/dev/null || true
    note "本地包缓存 $(find "$PKG/conda/pkgs" -type f | wc -l) 个（$(du -sh "$PKG/conda/pkgs" | cut -f1)）"
  else
    note "⚠️ 找不到 conda pkgs 目录，跳过本地包缓存"
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
      cp -a "$ROOT/$src/." "$PKG/models/$name/"
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
