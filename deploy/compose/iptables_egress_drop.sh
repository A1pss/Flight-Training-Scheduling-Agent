#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# deploy/compose/iptables_egress_drop.sh —— 容器级 egress 封锁（v6 §11.5）
#
# > 交付方若使用 compose 路径，额外用 `iptables` DROP 所有 egress
#
# ⚠️ **本脚本要 root，开发机上不执行**（CLAUDE.md §2：裸装、用户态、不要求
# root）。它随离线交付包一起交给现场运维，在**采用 compose 路径**时执行。
# 裸装主路径不需要它 —— 那条路上的 egress 收口靠代码层 allowlist
# （`backend/core/http.py`）+ CI 静态扫描（E2/E3）+ 运行时抓包（E4）。
#
# ## 这一层拦的是代码层拦不住的东西
#
# 代码层 allowlist 只管**走 `core/http.py` 的请求**。一个用 C 扩展出网的第三方库
# （libpq、静态链接了 curl 的东西）在 Python 层是隐形的 —— E4 的 `/proc` 探针能
# **发现**它，但发现不等于阻止。iptables 这一层是最后一道，它在内核里，谁都绕不过。
#
# ## 为什么是「默认 DROP + 显式放行」而不是「封几个已知的坏地址」
#
# 黑名单永远不全。离线部署的正确形态是「除了本机与内网什么都不通」，那正好是
# `EGRESS_ALLOWLIST` 的内容 —— 两层用同一份口径，改一处两处一起改。
#
# ## 用法
#
# ```bash
# sudo bash iptables_egress_drop.sh apply     # 应用规则
# sudo bash iptables_egress_drop.sh status    # 看当前规则
# sudo bash iptables_egress_drop.sh revert    # 撤销（排障用）
# ```
#
# 规则挂在自建链 `FTS-EGRESS` 上而不是直接改 `DOCKER-USER` 的默认策略：
# 自建链可以整条删掉，**撤销是一条命令而不是「回想当初加了哪几条」**。
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

CHAIN="FTS-EGRESS"
# 与 `Settings.EGRESS_ALLOWLIST` 的缺省逐项对应（v6 §11.5）。
ALLOW_NETS=(
  "127.0.0.0/8"
  "10.0.0.0/8"
  "172.16.0.0/12"
  "192.168.0.0/16"
)

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "❌ 需要 root：sudo bash $0 $*" >&2
    exit 1
  fi
}

apply_rules() {
  require_root "$@"
  # DOCKER-USER 是 Docker 官方留给管理员的钩子链，**在 Docker 自己的规则之前**求值。
  # 往 FORWARD 里加规则会被 Docker 重启时覆盖，往 DOCKER-USER 里加不会。
  iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"

  # ① 已建立的连接照常（否则回包会被自己拦掉）
  iptables -A "$CHAIN" -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
  # ② allowlist 内的目标放行
  for net in "${ALLOW_NETS[@]}"; do
    iptables -A "$CHAIN" -d "$net" -j RETURN
  done
  # ③ 其余一律 DROP，并记一条日志（限速，免得刷满 dmesg）
  iptables -A "$CHAIN" -m limit --limit 6/min -j LOG --log-prefix "FTS-EGRESS-DROP: "
  iptables -A "$CHAIN" -j DROP

  # 挂到 DOCKER-USER 上（幂等：先删再加）
  iptables -D DOCKER-USER -j "$CHAIN" 2>/dev/null || true
  iptables -I DOCKER-USER 1 -j "$CHAIN"

  echo "✅ 已应用：容器出站仅允许 ${ALLOW_NETS[*]}"
  echo "   撤销：sudo bash $0 revert"
}

revert_rules() {
  require_root "$@"
  iptables -D DOCKER-USER -j "$CHAIN" 2>/dev/null || true
  iptables -F "$CHAIN" 2>/dev/null || true
  iptables -X "$CHAIN" 2>/dev/null || true
  echo "✅ 已撤销 $CHAIN"
}

show_status() {
  require_root "$@"
  iptables -S DOCKER-USER 2>/dev/null || echo "(没有 DOCKER-USER 链：Docker 没起？)"
  echo "── $CHAIN ──"
  iptables -S "$CHAIN" 2>/dev/null || echo "(未应用)"
}

case "${1:-}" in
  apply)  apply_rules ;;
  revert) revert_rules ;;
  status) show_status ;;
  *)
    echo "用法：sudo bash $0 {apply|revert|status}" >&2
    exit 2
    ;;
esac
