"""M8: audit_log 补 actor_ip 与 diff 两列（v6 §11.5「审计」四要素）

v6 §11.5 要求审计记录「操作人、IP、前后值 diff、trace_id」四样。M1 建表时
落了操作人、前后值与 trace_id，**IP 与 diff 两样没有列可放**。M8 补上：

- `actor_ip`：`request.client.host`。非 HTTP 入口（CLI 摄取）写空串。
  历史行按空串回填 —— 那些操作确实不是从网络来的，空串是**如实**而不是缺省。
- `diff`：写入当时算出的顶层键差异。历史行留 NULL 而**不**回填 —— 回填就得
  现在再算一遍，而「现在算的」与「当时算的」是两件事，审计表里不该出现
  一个看不出是事后补的推导值。NULL 明确表示「这行早于 diff 列存在」。

Revision ID: c1f4a9b73e02
Revises: 8f31c2ad57b1
Create Date: 2026-08-18 15:10:00.000000+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c1f4a9b73e02"
down_revision: str | None = "8f31c2ad57b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column("actor_ip", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column("audit_log", sa.Column("diff", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "diff")
    op.drop_column("audit_log", "actor_ip")
