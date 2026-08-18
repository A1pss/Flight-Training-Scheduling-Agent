#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# deploy/compose/build_base_image.sh —— 从**宿主自身**构建 compose 路径的基础镜像
#
# ## 为什么不是 `FROM ubuntu:20.04`
#
# `Dockerfile` 里写的确实是 `FROM ubuntu:20.04`，那是**有 registry 或有
# `images/*.tar` 的现场**的正常做法。但本项目的两个前提让「拉基础镜像」这条路
# 既走不通、也不该走：
#
# 1. **全离线内网部署**（v6 §0/§11.4）。离线机上没有 registry，交付包里的
#    `images/` 就是为此存在的 —— 而那些 tar 本来也得先在某处被构建出来。
# 2. **compose 路径的镜像里不装依赖**（见 `Dockerfile` 注释）：conda 环境与仓库
#    都是 bind-mount 进去的，镜像只需要提供**与宿主兼容的 glibc 与几个系统库**。
#    从宿主自己抽这几个库，兼容性是**由构造保证**的，而不是靠「基础镜像的发行版
#    版本号看起来对得上」。
#
# 于是这个脚本做的事是：算出 conda 环境真正依赖的宿主系统库闭包 → 拼一个极小的
# rootfs → `docker import` 成镜像。**产物只有几十 MB**，落在 `/var/lib/docker`
# 上的增量可以忽略（本机 `/` 分区只剩 27 GB，这一点是被逼出来的，也是对的）。
#
# ## CUDA / NVIDIA 那一族**故意不带**
#
# 宿主 `/lib/x86_64-linux-gnu` 里 2.2 GB 有一大半是 CUDA 与显卡驱动。容器里跑的是
# **纯 CPU 的确定性校验任务**（黄金用例指纹、CP-SAT），用不上它们。真要在容器里
# 跑推理，正确做法是 `--gpus` 让 nvidia container runtime 注入宿主驱动，
# **而不是把驱动烤进镜像** —— 版本一错比没有更糟。
#
# ## 用法
#
# ```bash
# bash deploy/compose/build_base_image.sh                 # 建 fts-base:local
# bash deploy/compose/build_base_image.sh --save out.tar  # 顺便导出给离线包
# ```
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY_ENV="${FTS_PY_ENV:-schedule}"
IMAGE="${FTS_BASE_IMAGE:-fts-base:local}"
SAVE_TO=""
[ "${1:-}" = "--save" ] && SAVE_TO="${2:?--save 要给输出路径}"

CONDA_PREFIX_DIR="$(conda run -n "$PY_ENV" python -c 'import sys,pathlib;print(pathlib.Path(sys.executable).parent.parent)' | tr -d '\r' | tail -1)"
[ -d "$CONDA_PREFIX_DIR" ] || { echo "❌ 找不到 conda 环境 $PY_ENV" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
echo "── 构建 rootfs（$STAGE）──"

# ── ① 算宿主系统库闭包 ───────────────────────────────────────────────
# 起点：conda 的 python 解释器 + 环境自带的顶层 .so + ortools 的扩展。
# `ldd` 已经是**递归**的（它给出的是完整的运行期依赖图），所以取一遍即可。
NEEDED="$STAGE/needed.txt"
{
  ldd "$CONDA_PREFIX_DIR/bin/python3.11" 2>/dev/null || true
  find "$CONDA_PREFIX_DIR/lib" -maxdepth 1 -name '*.so*' -print0 2>/dev/null \
    | xargs -0 -r -n1 ldd 2>/dev/null || true
  find "$CONDA_PREFIX_DIR/lib/python3.11/site-packages/ortools" -name '*.so' -print0 2>/dev/null \
    | xargs -0 -r -n1 ldd 2>/dev/null || true
} | grep -oE '=> /(usr/)?lib[^ ]*' | sed 's|=> ||' | sort -u > "$NEEDED"

# 动态链接器本身不出现在 ldd 的 `=>` 那一列里（它是被内核直接加载的）。
echo "/lib64/ld-linux-x86-64.so.2" >> "$NEEDED"
# 这几个是**运行期 dlopen** 的，ldd 看不见：
#   libgomp   —— ortools 的并行搜索（SOLVER_WORKERS>1 时才 dlopen）
#   libnss_*  —— getpwuid()，容器里以非 root 跑时要查用户名
#   libresolv/libnsl —— socket 连 PG/Redis 时的名字解析路径
for extra in libgomp.so.1 libnss_files.so.2 libnss_dns.so.2 libresolv.so.2 libnsl.so.1 libz.so.1; do
  # ⚠️ awk 里**不能用 `exit`**：提前退出会给 `ldconfig` 一个 SIGPIPE，
  # 而 `set -o pipefail` 会把它变成整条脚本以 141 退出（实测踩过）。
  found=$(ldconfig -p 2>/dev/null | awk -v n="$extra" '$1==n && !seen {v=$NF; seen=1} END {print v}')
  [ -n "$found" ] && echo "$found" >> "$NEEDED"
done
sort -u -o "$NEEDED" "$NEEDED"

# ── ② 拷进 rootfs（连符号链接一起，保持原路径）────────────────────────
mkdir -p "$STAGE/rootfs"/{lib,lib64,usr/lib,etc,tmp,proc,sys,dev}
COPIED=0
while read -r lib; do
  [ -e "$lib" ] || continue
  target_dir="$STAGE/rootfs$(dirname "$lib")"
  mkdir -p "$target_dir"
  # `-L` 解引用符号链接后再按原名放回去：容器里没有宿主的目录结构，
  # 保留一条指向不存在路径的软链只会在运行时报「file not found」而不说是哪一个。
  cp -Lf "$lib" "$target_dir/" 2>/dev/null && COPIED=$((COPIED + 1))
done < "$NEEDED"
echo "   系统库 $COPIED 个"

# ── ③ 最小 /etc ─────────────────────────────────────────────────────
# 以宿主同一个 UID 跑（bind-mount 进来的目录属主是宿主的，UID 对不上就只能读）。
HOST_UID="$(id -u)"; HOST_GID="$(id -g)"
printf 'root:x:0:0:root:/root:/bin/sh\nfts:x:%s:%s:fts:/tmp:/bin/sh\n' "$HOST_UID" "$HOST_GID" \
  > "$STAGE/rootfs/etc/passwd"
printf 'root:x:0:\nfts:x:%s:\n' "$HOST_GID" > "$STAGE/rootfs/etc/group"
printf 'hosts: files dns\npasswd: files\ngroup: files\n' > "$STAGE/rootfs/etc/nsswitch.conf"
printf '127.0.0.1 localhost\n' > "$STAGE/rootfs/etc/hosts"
# 时区：两条部署路径的日期字符串进 content_sha256，这一项分叉指纹必然不同。
cp -L /etc/localtime "$STAGE/rootfs/etc/localtime" 2>/dev/null || true
# 证书目录留空但存在：离线运行不需要证书，缺目录反而会让某些库在启动时报错。
mkdir -p "$STAGE/rootfs/etc/ssl/certs"

# ── ④ import 成镜像 ─────────────────────────────────────────────────
echo "── docker import → $IMAGE ──"
tar -C "$STAGE/rootfs" -c . | docker import \
    --change "ENV TZ=Asia/Shanghai LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1" \
    --change "USER $HOST_UID:$HOST_GID" \
    - "$IMAGE" >/dev/null

SIZE=$(docker image inspect "$IMAGE" --format '{{.Size}}')
echo "✅ $IMAGE 已构建（$((SIZE / 1024 / 1024)) MB）"

if [ -n "$SAVE_TO" ]; then
  mkdir -p "$(dirname "$SAVE_TO")"
  docker save "$IMAGE" -o "$SAVE_TO"
  echo "✅ 已导出：$SAVE_TO（$(du -h "$SAVE_TO" | cut -f1)）"
fi
