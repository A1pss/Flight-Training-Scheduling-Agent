"""审计流水的写入与前后值 diff（v6 §11.5「审计」那一行）。

> 所有写操作与批准操作入 `audit_log`：操作人、IP、前后值 diff、trace_id。

## 四要素各自的落点

| 要素 | 列 | 来源 |
|---|---|---|
| 操作人 | `actor` | `Principal.user_id`（**不是 token**——token 进日志等于口令进日志） |
| IP | `actor_ip` | `request.client.host`。**不认 `X-Forwarded-For`**，见下 |
| 前后值 diff | `before` / `after` / `diff` | 调用方给前后两个快照，`diff` 由 :func:`value_diff` 当场算 |
| trace_id | `trace_id` | 中间件生成的那一个，与 `trace_events` 对得上 |

## 为什么不认 `X-Forwarded-For`

那个头是客户端可以随便写的。裸装部署（v6 §11.1）里 uvicorn 直接面向内网，
前面没有反向代理，所以 `request.client.host` 就是真实来源。采信 XFF 只会
**让审计日志变成可伪造的**——审计日志一旦可伪造，它的全部价值就没了。
将来真在前面加了 nginx，那时再按「只信任已知代理 IP 转发的那一跳」改，
而不是现在先埋一个默认信任。

## 为什么 `diff` 单独存一列

`before` / `after` 两列已经含有全部信息，`diff` 从形式上是冗余的。存它是为了
两件事：① 管理员查审计不必自己对着两坨 JSON 找不同；② diff 是**写入当时**
按当时的值算出来的，日后即便这个算法改了，历史记录里的结论也不会跟着变。
审计表只追加不更新，冗余在这里是特性而不是缺陷。

## diff 只比顶层键

嵌套结构整体当一个值比。理由是审计要回答的是「哪个字段被改了」，而不是
「这个字段内部第 7 层的哪一位变了」；后者要的是产物 diff（`data/plans/` 里
那份 manifest），不是审计流水。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from backend.models.audit import AuditLog


def value_diff(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """按顶层键算前后值差异。

    返回三段：`changed`（两侧都有且不等）、`added`（只有 after 有）、
    `removed`（只有 before 有）。三段都为空时返回 `{}`——**空 diff 是有意义的
    信号**（「批准了但什么都没改」），所以照样写进去，不省略这一行审计。
    """
    old = dict(before or {})
    new = dict(after or {})
    changed = {
        key: {"before": old[key], "after": new[key]}
        for key in sorted(old.keys() & new.keys())
        if old[key] != new[key]
    }
    added = {key: new[key] for key in sorted(new.keys() - old.keys())}
    removed = {key: old[key] for key in sorted(old.keys() - new.keys())}
    out: dict[str, Any] = {}
    if changed:
        out["changed"] = changed
    if added:
        out["added"] = added
    if removed:
        out["removed"] = removed
    return out


def record_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    trace_id: str = "",
    actor_ip: str = "",
) -> AuditLog:
    """写一行审计流水并 `flush`（拿 `audit_id`），**不 commit**。

    事务边界归调用方：摄取确认那一路要与建快照同一个事务里成败与共，
    审计自己 commit 会造出「审计说改了、数据其实没改」的记录。
    """
    row = AuditLog(
        actor=actor,
        actor_ip=actor_ip,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before=dict(before) if before is not None else None,
        after=dict(after) if after is not None else None,
        diff=value_diff(before, after),
        trace_id=trace_id,
    )
    session.add(row)
    session.flush()
    return row


__all__ = ["record_audit", "value_diff"]
