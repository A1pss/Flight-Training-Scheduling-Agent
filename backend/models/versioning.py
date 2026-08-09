"""版本表：数据快照 / 规则集版本 / 语义版本。

三者合起来就是铁律 9 里那把「可复现性」的钥匙：
`snapshot_id + ruleset_version + semantics_version + seed` 唯一确定一次排班。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base

#: 快照状态。`PENDING` = 已抽取待人工确认；`ACTIVE` = 已确认并生效；
#: `SUPERSEDED` = 被更新的快照取代；`REJECTED` = 人工门禁驳回。
SNAPSHOT_STATUSES = ("PENDING", "ACTIVE", "SUPERSEDED", "REJECTED")


class DataSnapshot(Base):
    """一次摄取产出的事实快照（v6 §5.1 落库段落）。"""

    __tablename__ = "data_snapshots"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'ACTIVE', 'SUPERSEDED', 'REJECTED')",
            name="snapshot_status_enum",
        ),
    )

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    #: 参与摄取的源文件清单：路径 / sha256 / 页数 / 分类结果
    source_manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: 规范化事实内容的 sha256 —— 同样输入必然同样快照内容（铁律 9）
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 规范化后的全量事实（`ingestion.diff.normalize_facts` 的输出）。
    #:
    #: **Diff 的基线取这里，不取事实表回读。** 原因：`rules.pdf` 抽出的条文原文
    #: 按 v6 §6.1 只进 Chroma、不进 PG（PG 侧的 `rulesets` 存的是 YAML 规则集的
    #: 版本登记，不是 PDF 条文），所以从事实表回读永远拼不出 `rule` 那一类，
    #: 每次摄取都会凭空多出 14 条「新增规则」。把规范化快照原样存下来，Diff
    #: 才是精确的，规则原文的变更也才会真的走到人工确认门禁。
    normalized_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Ruleset(Base):
    """`rules/ruleset_v1.3.yaml` 的落库登记。"""

    __tablename__ = "rulesets"

    ruleset_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    rule_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SemanticsVersion(Base):
    """`rules/semantics.yaml` 的落库登记（S-01~S-13 的裁定值全量存档）。"""

    __tablename__ = "semantics_versions"

    semantics_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    decided_on: Mapped[date] = mapped_column(Date, nullable=False)
    decided_by: Mapped[str] = mapped_column(String(64), nullable=False)
    #: S-01~S-13 的完整开关取值，切换任何一条都会改变排班结果
    switches: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["SNAPSHOT_STATUSES", "DataSnapshot", "Ruleset", "SemanticsVersion"]
