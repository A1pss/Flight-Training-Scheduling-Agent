"""API 层的审计写入（v6 §11.5「审计」）。

> 所有写操作与批准操作入 `audit_log`：操作人、IP、前后值 diff、trace_id。

## 「所有写操作」= 全部 POST 端点

v6 §9.1 的 11 个端点里有 6 个 POST，它们各自都改了服务端状态：`/ingest` 落盘、
`/confirm` 建快照、`/chat` 与 `/schedule` 占锁并入队、`/approve` 归档、
`/reject` 结束一次运行。**GET 一律不写审计** —— 读操作写审计只会把表撑大到
没人愿意翻，真正要查的那几行反而淹掉。

## 为什么每个端点自己开一个会话

审计要记的是「**这次请求发生了**」，而不是「这次请求的业务事务成功了」。两者
在这里恰好可以分开：POST 的业务副作用（入队、占锁）不在 PG 事务里，所以审计
用自己的短会话立刻 commit 是对的。

唯一的例外是 `/confirm`：摄取落库那一路在 `pipeline.commit()` 里另写一行
`ingest.commit`，**与建快照同一个事务成败与共**。那一行记的是快照 A → B 的
数据变更，本模块这一行记的是「谁从哪台机器调了这个端点」——两件事，两行，
都要有。查审计时前者回答「数据怎么变的」，后者回答「谁动的手」。

## 前后值 diff 记什么

对决策类端点（approve / reject），`before` 是**提交决策前那次运行的状态**
（是否停在门禁、方案指纹），`after` 是决策本身（决定、意见、授权档位）。
这样 diff 里能直接看出「本来停在 AWAITING_HUMAN 的 14 架次方案，被 P01 批了」。

对提交类端点（chat / schedule / ingest），`before` 为 `None`——**它们本来就
不存在前值**，硬造一个空 dict 只会让 diff 里出现一堆假的 `added`。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request

from backend.api.deps import CurrentClientIP, CurrentPrincipal, CurrentTraceId
from backend.core.audit import record_audit
from backend.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class AuditRecorder:
    """绑定了「谁 / 从哪 / 哪次 trace」的审计写入器。"""

    session_factory: Any
    actor: str
    actor_ip: str
    trace_id: str

    def record(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
    ) -> None:
        """写一行并 commit。

        **写不进去要说话。** 审计失败时记 ERROR 日志但不让请求失败：审计是旁路，
        为它把一次已经生效的业务操作回滚掉，只会制造「操作做了一半」这种更糟的
        状态。但**绝不静默**——日志里那条 ERROR 就是「这段时间的审计不可信」的
        唯一证据。
        """
        session = self.session_factory()
        try:
            record_audit(
                session,
                actor=self.actor,
                actor_ip=self.actor_ip,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                before=before,
                after=after,
                trace_id=self.trace_id,
            )
            session.commit()
        except Exception as exc:
            logger.error(
                "审计写入失败",
                action=action,
                resource_id=resource_id,
                trace_id=self.trace_id,
                error=str(exc),
            )
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                rollback()
        finally:
            session.close()


def get_audit(
    request: Request,
    principal: CurrentPrincipal,
    actor_ip: CurrentClientIP,
    trace_id: CurrentTraceId,
) -> AuditRecorder:
    """审计写入器依赖。**放在 `principal` 之后**，所以未认证的请求写不出审计行
    ——那类请求根本没有「操作人」，写进去只能是一行匿名噪声。"""
    from backend.core.db import get_session_factory

    factory = request.app.state.session_factory or get_session_factory()
    return AuditRecorder(
        session_factory=factory,
        actor=principal.user_id,
        actor_ip=actor_ip,
        trace_id=trace_id,
    )


CurrentAudit = Annotated[AuditRecorder, Depends(get_audit)]

__all__ = ["AuditRecorder", "CurrentAudit", "get_audit"]
