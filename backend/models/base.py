"""SQLAlchemy 声明基类与全表共用的列约定。

**快照作用域**：全部事实表的主键都带 `snapshot_id`（v6 §5.1 落库段落 ——
「PG(事实) → Chroma(向量化) → 新 snapshot_id」）。这不是洁癖：`CLAUDE.md`
铁律 9 要求「同 snapshot_id + 同 ruleset_version + 同 semantics_version +
seed=42 → 结果逐字节可复现」，也就是说**下游每一次查询本来就必须带
snapshot_id 过滤**。把它写进主键，多个快照才能共存，Diff 层（§5.1）与
FTS-3004（HITL 恢复时快照已变更）才有真实的比对对象。

唯一的例外是 `training_progress` —— v6 §6.3 给出的主键是
`(person_id, mission_id, cycle_start)`，`snapshot_id` 只是普通列。任务书要求
「字段严格照 v6 §6.3」，故原样照做，另以外键把它挂回 `persons` / `missions`。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: 显式命名约定 —— 不写这个，Alembic autogenerate 出来的约束名在不同版本间会漂，
#: 迁移就不可复现了（铁律 9）。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全部 ORM 模型的声明基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """`created_at` —— 由数据库侧生成，避免应用时钟参与内容哈希（铁律 9）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["NAMING_CONVENTION", "Base", "TimestampMixin"]
