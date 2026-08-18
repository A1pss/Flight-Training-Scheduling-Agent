"""RBAC 权限矩阵（v6 §11.5「认证鉴权」）—— **端点 → 最低角色**的唯一声明处。

## 为什么要有这张表

M6 把 `require_role(principal, "director", ...)` 直接写在每个处理器里。那是对的
（鉴权要贴着业务动作），但它有一个盲点：**没有任何地方能回答「一共有哪些端点、
分别要什么角色」**。新加一个端点忘了写 `require_role`，代码照样能跑、测试照样
能绿，而那个端点就是全员可达的。

这张表把矩阵显式化，`tests/guardrail/test_rbac_matrix.py` 拿它做两件事：

1. **表 vs 实现**：四个角色 × 全部端点逐格发真请求，断言 403 当且仅当
   `ROLE_RANK[角色] < ROLE_RANK[表里的最低角色]`；
2. **表 vs 路由**：OpenAPI 里的每一条 operation 都必须在表里有一行，
   多一条少一条都红。**新端点漏写鉴权会在这里被拦下。**

所以这张表不是文档，是可执行的规格。

## 四个角色的阶梯

`viewer < scheduler < director < admin`（`planner/authority.py::ROLE_RANK`）。
`admin` 在 v6 §11.5 的职责是「数据与规则变更」，它天然覆盖下面三档，所以矩阵里
不会出现「admin 不可达而 director 可达」的格子。

## 松弛档位是第二道，不在这张表里

`RELAX_TIER_AUTHORITY`（Tier 3 需 `director`）管的是**同一个端点内部**的参数，
角色够格进 `/approve` 不等于够格批 Tier 3。那道校验在
`Principal.can_authorize_tier`，由 `test_rbac_matrix.py` 的档位小节单独测。
"""

from __future__ import annotations

from typing import Final, NamedTuple

from backend.schemas.intent import UserRole


class EndpointPolicy(NamedTuple):
    """一个端点的鉴权声明。`minimum=None` = 匿名可达。"""

    method: str
    path: str
    minimum: UserRole | None
    action: str


#: v6 §9.1 的 11 个端点 + `/health` 存活探针。
#:
#: `/health` 是**唯一匿名可达**的路径：它要能在 token 配错、甚至 `API_TOKENS`
#: 整个为空时回答「进程还活着吗」。把它挡在鉴权后面，运维排障时就只能靠
#: 「连不上」和「401」去猜进程死没死。它的响应体里没有任何业务数据
#: （只有版本串与离线标志），不构成信息泄露面。
ENDPOINT_POLICIES: Final[tuple[EndpointPolicy, ...]] = (
    EndpointPolicy("GET", "/health", None, "存活探针"),
    # ── 摄取 ─────────────────────────────────────────────────────────
    EndpointPolicy("POST", "/api/v1/ingest", "scheduler", "上传数据文件"),
    EndpointPolicy("GET", "/api/v1/ingest/{job_id}/changeset", "viewer", "查看摄取变更集"),
    EndpointPolicy("POST", "/api/v1/ingest/{job_id}/confirm", "scheduler", "确认数据入库"),
    # ── 交互与排班 ───────────────────────────────────────────────────
    EndpointPolicy("POST", "/api/v1/chat", "scheduler", "发起会话"),
    EndpointPolicy("POST", "/api/v1/schedule", "scheduler", "提交排班"),
    # ── 查询 ─────────────────────────────────────────────────────────
    EndpointPolicy("GET", "/api/v1/jobs/{job_id}", "viewer", "查看任务状态"),
    EndpointPolicy("GET", "/api/v1/runs/{trace_id}", "viewer", "查看运行结果"),
    EndpointPolicy("GET", "/api/v1/plans", "viewer", "查询历史计划"),
    EndpointPolicy("GET", "/api/v1/schedule/{trace_id}/export", "viewer", "下载排班产物"),
    # ── 决策 ─────────────────────────────────────────────────────────
    EndpointPolicy("POST", "/api/v1/schedule/{trace_id}/reject", "scheduler", "驳回排班方案"),
    # ★ 归档是本系统唯一不可撤销的写：推进进度、结算欠账、写 last_done_date 锚点
    EndpointPolicy("POST", "/api/v1/schedule/{trace_id}/approve", "director", "确认并归档排班方案"),
)

#: `(METHOD, path)` → 声明，便于按路由查。
POLICY_BY_ROUTE: Final[dict[tuple[str, str], EndpointPolicy]] = {
    (item.method, item.path): item for item in ENDPOINT_POLICIES
}


def policy_for(method: str, path: str) -> EndpointPolicy | None:
    return POLICY_BY_ROUTE.get((method.upper(), path))


__all__ = ["ENDPOINT_POLICIES", "POLICY_BY_ROUTE", "EndpointPolicy", "policy_for"]
