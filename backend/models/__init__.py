"""SQLAlchemy ORM 声明基类。

M0 只交付 `Base` —— Alembic 的 `target_metadata` 需要它才能跑通。
**全部表定义由 M1 窗口交付**（人员/飞机/课目/空域/跑道/规则版本/快照/
排班计划/架次/训练进度/审计日志/Checkpoint/TraceEvent，见 v6 §6.1）。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """全部 ORM 模型的声明基类。"""


__all__ = ["Base"]
