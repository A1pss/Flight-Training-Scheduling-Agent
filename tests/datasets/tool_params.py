"""工具调用参数的**合法**默认值工厂。

## 为什么要有这个文件

`trajectory_100` 与 `tool_calls_200` 都要给出「每步期望的工具与参数」。手写参数
会错 —— W11 造数据时就写错了三处：`check_authority` 的字段名（实际是
`actor_role` / `requested_tier`）、`rank_relaxations.prefer` 的枚举值（实际是
`least_debt` / `least_disruption` / `fastest`）、以及 `propose_solve_intent.intent`
（实际是一个完整的 `SolveIntent`，不是一个字典片段）。

**这三处都是被 `ToolStep` 的 schema 校验抓出来的**，不是靠人看出来的 ——
所以参数一律从这里取，再按需覆盖字段。

> 一个标注错了参数的数据集，会把「参数准确率」测成模型的问题，
> 而实际上错的是标注。
"""

from __future__ import annotations

from typing import Any, Final

from backend.harness.tools import TOOL_CATALOG

BASE_WEEK: Final[str] = "2026W02"
BASE_SNAPSHOT: Final[str] = "snap_9724982865ee"

#: 一个合法的 `SolveIntent`（`propose_solve_intent.intent` 要的是完整契约）
SOLVE_INTENT: Final[dict[str, Any]] = {
    "scope_persons": "ALL",
    "scope_missions": "ALL",
    "freeze_policy": "BALANCED",
    "freeze_reason": "无既有计划，取中性冻结档",
    "objective_weights": {"progress": 1.0, "disruption": 0.3, "balance": 0.2},
    "estimated_blast_radius": 14,
    "pre_authorized_tiers": [],
    "incremental_constraints": [],
}

_DEFAULTS: Final[dict[str, dict[str, Any]]] = {
    "resolve_person": {"surface": "何超"},
    "resolve_aircraft": {"surface": "AC73"},
    "resolve_week": {"surface": "下周", "reference_date": "2026-01-05"},
    "ask_user": {
        "question": "您指的是哪一周？",
        "resolution": "answer",
        "options": ["2026-W02", "2026-W03"],
    },
    "escalate": {"reason": "无可行的松弛提案，需人工调配资源", "severity": "WARN"},
    "estimate_scope": {
        "iso_week": BASE_WEEK,
        "scope_persons": "ALL",
        "scope_missions": "ALL",
    },
    "assess_disruption": {
        "iso_week": BASE_WEEK,
        "baseline_plan_id": "plan_2026W02_v1",
        "changed_persons": ["P02"],
        "changed_aircraft": ["AC84"],
    },
    "propose_solve_intent": {
        "iso_week": BASE_WEEK,
        "intent": SOLVE_INTENT,
        "rationale": "全员排班，无既有计划",
    },
    "translate_revision": {
        "utterance": "把张勇的 missionC-2 挪到周四以后",
        "round_no": 1,
        "iso_week": BASE_WEEK,
    },
    "check_authority": {"actor_role": "排班员", "requested_tier": 1},
    "classify_doc": {"filename": "personnel.pdf", "text_head": "人员信息表 编号 姓名 身份…"},
    "parse_personnel": {"document_id": "doc_personnel", "page_range": "1-3"},
    "parse_aircraft": {"document_id": "doc_aircraft", "page_range": "1-2"},
    "parse_missions": {"document_id": "doc_missions", "page_range": "1-2"},
    "parse_rules": {"document_id": "doc_rules", "page_range": "1-4"},
    "diff_snapshot": {"base_snapshot_id": BASE_SNAPSHOT, "new_snapshot_id": "snap_pending"},
    "propose_change": {
        "entity_kind": "person",
        "entity_id": "P04",
        "field": "recurrent_due",
        "old_value": "2026-02-07",
        "new_value": "2026-01-07",
        "reason": "总表口径优先于明细表（SPEC_DECISIONS §C.1）",
    },
    "propose_rule_dsl": {
        "clause_no": 7,
        "clause_text": "同一架飞机的相邻架次之间须满足周转时间；维护时段内不得排班。",
    },
    "sql_query": {
        "sql": "SELECT * FROM persons WHERE person_id = :pid",
        "params": {"pid": "P08"},
        "limit": 10,
    },
    "prereq_cte": {"person_id": "P08", "mission_id": "missionB-1"},
    "vector_search": {"query": "何超 训练进度", "top_k": 5, "collection": "entity_summaries"},
    "bm25_search": {"query": "missionF-1 推迟 原因", "top_k": 5},
    "rrf_fuse": {"rankings": [["ent:person:P08"], ["ent:mission:missionB-1"]], "k": 60, "top_k": 5},
    "rerank": {"query": "何超 训练进度", "candidates": ["ent:person:P08"], "top_k": 5},
    "memory.search": {"query": "松弛档偏好", "kinds": ["procedural"], "top_k": 5},
    "memory.write": {
        "kind": "episodic",
        "key": "session/2026W02",
        "content": "用户驳回第 1 版，理由：带飞教员集中在孙军一人身上",
        "valid_from": "2026-01-05",
        "source": "对话推断",
    },
    "min_conflict_set": {"iso_week": BASE_WEEK, "scope_persons": ["ALL"]},
    "blame_chain": {"person_id": "P08", "mission_id": "missionB-1"},
    "probe_solve": {"iso_week": BASE_WEEK, "relaxations": ["TIER_1"], "time_limit_s": 30.0},
    "rank_relaxations": {"proposals": ["R1_freq_relax"], "prefer": "least_debt"},
    "render_workbook": {"plan_id": "plan_2026W02_v1"},
    "compose_report": {"plan_id": "plan_2026W02_v1", "sections": ["blocked", "debts"]},
    "verify_claim": {
        "claim": "何超本周未排 missionB-1，因为先修未达标",
        "evidence_refs": ["ent:person:P08", "rule:1.3.0:13"],
    },
}


def params_for(tool: str, **overrides: Any) -> dict[str, Any]:
    """取该工具的一组合法参数，可按字段覆盖。

    **返回前会过一遍真实的 `params_model`** —— 覆盖字段写错名字或类型，
    在这里就炸，而不是等到数据集写盘之后。
    """
    if tool not in _DEFAULTS:
        raise KeyError(f"{tool!r} 没有登记默认参数；工具目录共 {len(TOOL_CATALOG)} 个")
    payload = {**_DEFAULTS[tool], **overrides}
    TOOL_CATALOG[tool].params_model.model_validate(payload)
    return payload


__all__ = ["BASE_SNAPSHOT", "BASE_WEEK", "SOLVE_INTENT", "params_for"]
