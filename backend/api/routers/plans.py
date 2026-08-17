"""`GET /api/v1/plans?week=2026-W02` —— 历史计划查询（v6 §9.1）。

## 两种周写法都认

v6 §9.1 的示例写的是 `2026-W02`（ISO 8601 的带连字符形态），而库里存的是
`2026W02`（`SchedulePlan.iso_week` 的形态）。两种都接受、内部归一到后者——
**不认带连字符的那种就等于让文档里的示例调不通**。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from backend.api.deps import CurrentPrincipal, CurrentSession
from backend.api.security import require_role
from backend.core.errors import RequiredInputMissingError
from backend.models.planning import Plan, Sortie
from backend.schemas.api import PlanListView, PlanSummaryView

router = APIRouter(tags=["计划"])

_WEEK_PATTERN = re.compile(r"^(\d{4})-?W(\d{2})$", re.IGNORECASE)


def normalize_week(week: str) -> str:
    """`2026-W02` / `2026W02` → `2026W02`。认不出就抛 FTS-1004。"""
    match = _WEEK_PATTERN.match(week.strip())
    if match is None:
        raise RequiredInputMissingError(
            f"week 参数 {week!r} 不是合法的 ISO 周（如 2026-W02 或 2026W02）",
            details={"week": week, "resolution": "answer"},
            suggestions=["按 2026-W02 的写法重试"],
        )
    return f"{match.group(1)}W{match.group(2)}"


@router.get("/plans", response_model=PlanListView, summary="历史计划查询")
def get_plans(
    principal: CurrentPrincipal,
    session: CurrentSession,
    week: str | None = Query(default=None, description="ISO 周，如 2026-W02。不给则返回最近 50 条"),
    limit: int = Query(default=50, ge=1, le=200),
) -> PlanListView:
    require_role(principal, "viewer", action="查询历史计划")
    normalized = normalize_week(week) if week else None

    counts: dict[str, int] = {
        str(plan_id): int(total)
        for plan_id, total in session.execute(
            select(Sortie.plan_id, func.count(Sortie.sortie_id)).group_by(Sortie.plan_id)
        ).all()
    }

    stmt = select(Plan).order_by(Plan.iso_week.desc(), Plan.plan_version.desc()).limit(limit)
    if normalized:
        stmt = stmt.where(Plan.iso_week == normalized)

    plans = [
        PlanSummaryView(
            plan_id=row.plan_id,
            iso_week=row.iso_week,
            plan_version=row.plan_version,
            week_start=row.week_start,
            week_end=row.week_end,
            status=row.status,
            relax_tier=row.relax_tier,
            sorties=int(counts.get(row.plan_id, 0)),
            snapshot_id=row.snapshot_id,
            ruleset_version=row.ruleset_version,
            semantics_version=row.semantics_version,
            content_sha256=row.content_sha256,
            created_at=row.created_at,
            approved_by=row.approved_by,
        )
        for row in session.execute(stmt).scalars()
    ]
    return PlanListView(week=normalized, plans=plans)


__all__ = ["normalize_week", "router"]
