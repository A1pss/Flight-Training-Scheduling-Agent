# 瘦运行时镜像（v6 §11.4 的 compose 备选路径）。
#
# ⚠️ **离线机上用不了这份 Dockerfile** —— `FROM ubuntu:20.04` 要能连 registry。
# 那种现场用 `build_base_image.sh`：它从宿主自身抽 glibc 闭包 import 成镜像，
# 兼容性由构造保证，产物实测 6 MB。这份 Dockerfile 留给「有 registry 或已经
# `docker load` 过 images/*.tar」的现场，两者产出的容器行为一致（镜像里都不装依赖）。
#
# **镜像里不装 Python 依赖。** conda 环境与仓库由 compose 以 bind-mount 的方式
# 挂进来，且挂在**与宿主完全相同的绝对路径**上 —— conda 环境里的 shebang、
# `.pth`、编译产物全都写着绝对路径，换个挂载点就跑不起来。
#
# 基础镜像钉 `ubuntu:20.04`：宿主实测 Ubuntu 20.04.6 / glibc 2.31，而 bind-mount
# 进来的是**宿主编译的**二进制（torch 的 .so、ortools 的 .so）。基础镜像的 glibc
# 低于宿主会直接 `GLIBC_2.3x not found`。换宿主发行版时这一行要跟着换。
FROM ubuntu:20.04

# 只装运行 conda 环境所需的系统库。**不装 python** —— 用的是挂进来那一个。
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates libgomp1 libglib2.0-0 libsm6 libxext6 libxrender1 tzdata \
    && rm -rf /var/lib/apt/lists/*

# 时区与 locale 必须与裸装一致：它们会影响日期格式化，而 `content_sha256` 的
# 输入里有日期字符串。两条路径在这一项上分叉，指纹就必然不同 —— 而那与
# 「部署方式」无关，是纯粹的环境噪声。
ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0

# 以非 root 运行（v6 §11.1「用户态运行，不要求 root」在容器里的对应做法）。
# UID 由 build arg 给，默认与常见的第一个普通用户一致；install.sh 会传宿主的真实 UID
# —— bind-mount 进来的目录属主是宿主的，UID 对不上就只能读不能写。
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g "${APP_GID}" fts 2>/dev/null || true \
    && useradd -u "${APP_UID}" -g "${APP_GID}" -M -s /usr/sbin/nologin fts 2>/dev/null || true
USER ${APP_UID}:${APP_GID}

# 没有 ENTRYPOINT：每个服务在 compose 里显式给完整命令，命令行长一点，
# 换来的是「这个容器到底在跑什么」一眼可见。
