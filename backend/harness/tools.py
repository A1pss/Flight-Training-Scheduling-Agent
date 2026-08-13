"""工具目录：v6 §7.7.2 权限矩阵里出现的**每一个**工具的入参契约。

三条设计口径：

1. **入参只有一份真相**——Pydantic 模型。给模型看的 JSON Schema 由它导出，
   运行时校验也由它执行（`ToolSpec.params_model`）。
2. **实体字段带 `x-entity` 标注**。它有两个用途：导出的 JSON Schema 里明确告诉
   模型「这里要的是编号不是人名」；运行时把该字段的任何失败归类为
   `entity_hallucination`（v6 §12.5.1 硬地板 x 的唯一观测口）。
   编号形态按 §5.1.1 / 附录 B：**只固定前缀、不限位数**，`airspace_id` 连前缀
   都不固定（编号完全由上传数据决定）。
3. **`writes=True` 只有 `memory.write` 一个**。§7.7.2 最后一行「任何数据写入
   （除 memory）✖」在这里是**结构性**保证：工具表里根本没有第二个写工具，
   而 `acl.py` 会在注册时逐个复核。`sql_query` 的只读性由参数校验兜住
   （非 SELECT/WITH 语句直接判非法），不靠调用方自觉。

> **这里没有 `solve` / `validate` / `compile_spec` / `resume_guard` /
> `human_gate` / `commit_plan`，而且永远不会有。** 六个确定性节点不注册为
> 工具，物理上就调不到（v6 §7.7.2 最后两行，CLAUDE.md 铁律 4）。
> `acl.FORBIDDEN_NODES` 会在注册路径上再挡一道。
"""

from __future__ import annotations

import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.harness.types import ToolSpec
from backend.schemas.common import EntityKind
from backend.schemas.intent import SolveIntent

# ─────────────────────────────────────────────────────────────────────
# 实体字段
# ─────────────────────────────────────────────────────────────────────

#: 各类实体编号的形态（§5.1.1 / 附录 B：只固定前缀、不限位数）。
#: `airspace` 刻意没有 pattern —— 空域编号由上传数据决定，一致性靠引用完整性
#: 校验，不靠正则；把它钉成 `^SA[AB]$` 这类基准取值就是 M2-A 修过的那个 bug。
ENTITY_PATTERNS: Final[dict[EntityKind, str | None]] = {
    "person": r"^P\d+$",
    "aircraft": r"^AC\d+$",
    "mission": r"^mission[A-Z]-\d+$",
    "runway": r"^RWY-\d+$",
    "week": r"^\d{4}W\d{2}$",
    "airspace": None,
}

#: JSON Schema 里标注实体类别的扩展键。导出后模型能看见，运行时也据此分类。
ENTITY_SCHEMA_KEY: Final[str] = "x-entity"


def entity_field(kind: EntityKind, description: str, **kwargs: Any) -> Any:
    """声明一个实体编号字段。"""
    pattern = ENTITY_PATTERNS[kind]
    extra: dict[str, Any] = {ENTITY_SCHEMA_KEY: kind}
    if pattern is not None:
        return Field(pattern=pattern, description=description, json_schema_extra=extra, **kwargs)
    return Field(min_length=1, description=description, json_schema_extra=extra, **kwargs)


def entity_kind_of(schema: dict[str, Any]) -> EntityKind | None:
    """从字段的 JSON Schema 片段里读回 `x-entity` 标注。"""
    kind = schema.get(ENTITY_SCHEMA_KEY)
    if isinstance(kind, str) and kind in ENTITY_PATTERNS:
        return kind
    return None


class _Params(BaseModel):
    """全部入参模型的基类：禁止多余字段。

    `extra="forbid"` 不是洁癖——模型多塞一个 `person_name` 字段而我们默默忽略，
    最后排出来的是「按 person_id 排的班」还是「按名字排的班」就说不清了。
    """

    model_config = ConfigDict(extra="forbid")


# ─────────────────────────────────────────────────────────────────────
# 第 1~2 行：实体消解与人机交互（意图路由 / Planner）
# ─────────────────────────────────────────────────────────────────────


class ResolvePersonParams(_Params):
    surface: str = Field(min_length=1, description="原文里的人员表述，如「何超」")


class ResolveAircraftParams(_Params):
    surface: str = Field(min_length=1, description="原文里的飞机表述，如「73 号机」")


class ResolveWeekParams(_Params):
    surface: str = Field(min_length=1, description="原文里的周表述，如「下周」「2026 年第 2 周」")
    reference_date: str = Field(
        default="",
        pattern=r"^(\d{4}-\d{2}-\d{2})?$",
        description="相对表述的参照日期 YYYY-MM-DD；留空表示用当前快照的周",
    )


class AskUserParams(_Params):
    question: str = Field(min_length=1, description="要问用户的问题原文")
    resolution: Literal["answer", "upload"] = Field(
        description="answer=给一个值即可；upload=必须补传整份文件（v6 §5.1.1 / FTS-1004）"
    )
    options: list[str] = Field(default_factory=list, description="可选项，留空表示自由作答")


class EscalateParams(_Params):
    reason: str = Field(min_length=1)
    severity: Literal["INFO", "WARN", "ERROR", "CRITICAL"] = "WARN"


# ─────────────────────────────────────────────────────────────────────
# 第 3~4 行：Planner 专属
# ─────────────────────────────────────────────────────────────────────


class EstimateScopeParams(_Params):
    iso_week: str = entity_field("week", "目标周，ISO 形式如 2026W02")
    scope_persons: list[str] | Literal["ALL"] = Field(description="人员范围，或 ALL")
    scope_missions: list[str] | Literal["ALL"] = Field(description="课目范围，或 ALL")


class AssessDisruptionParams(_Params):
    iso_week: str = entity_field("week", "目标周")
    baseline_plan_id: str = Field(default="", description="对比基线方案 ID；首轮排班留空")
    changed_persons: list[str] = Field(default_factory=list)
    changed_aircraft: list[str] = Field(default_factory=list)


class ProposeSolveIntentParams(_Params):
    """Planner 的唯一产物（v6 §7.3.2）。

    直接复用 `schemas.intent.SolveIntent` —— 它已经把「只能调四类旋钮」写死在
    契约里，重新定义一份等价模型只会给漂移留缝。
    """

    iso_week: str = entity_field("week", "目标周")
    intent: SolveIntent
    rationale: str = Field(min_length=1, description="选这套参数的理由，进 Sheet 4")


class TranslateRevisionParams(_Params):
    utterance: str = Field(min_length=1, description="用户原话，原样保留供撤销与审计")
    round_no: int = Field(ge=1, description="第几轮修订")
    iso_week: str = entity_field("week", "目标周")


class CheckAuthorityParams(_Params):
    actor_role: Literal["查看者", "排班员", "训练主任", "管理员"] = Field(
        description="RBAC 四角色（v6 §11.5）"
    )
    requested_tier: int = Field(ge=0, le=3, description="申请的松弛档位（v6 §3.10）")


# ─────────────────────────────────────────────────────────────────────
# 第 5~6 行：摄取抽取专属
# ─────────────────────────────────────────────────────────────────────

DocKind = Literal["personnel", "aircraft", "missions", "rules", "freetext"]


class ClassifyDocParams(_Params):
    filename: str = Field(min_length=1)
    text_head: str = Field(min_length=1, description="文档前若干字符，供分类器判型")


class ParseDocParams(_Params):
    """`parse_personnel` / `parse_aircraft` / `parse_missions` / `parse_rules` 共用。"""

    document_id: str = Field(min_length=1, description="已入库的原始文档 ID")
    page_range: str = Field(
        default="",
        pattern=r"^(\d+(-\d+)?)?$",
        description="页码范围如 `2-5`；留空表示全文",
    )


class DiffSnapshotParams(_Params):
    base_snapshot_id: str = Field(min_length=1)
    new_snapshot_id: str = Field(min_length=1)


class ProposeChangeParams(_Params):
    """**提案**，不是写入。落库由人工确认后的确定性路径完成（v6 §5.1）。"""

    entity_kind: EntityKind
    entity_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    old_value: str = ""
    new_value: str = ""
    reason: str = Field(min_length=1)


class ProposeRuleDslParams(_Params):
    clause_no: int = Field(ge=1, le=14, description="rules.pdf 的条文编号（14 条）")
    clause_text: str = Field(min_length=1, description="条文原文")


# ─────────────────────────────────────────────────────────────────────
# 第 7 行：检索（摄取 / Knowledge / Diagnosis / 解释生成 共用）
# ─────────────────────────────────────────────────────────────────────

_WRITE_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|merge|vacuum)\b",
    re.IGNORECASE,
)
_READ_SQL_HEAD = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class SqlQueryParams(_Params):
    """只读 SQL。

    「除 memory 外任何数据写入禁止」（§7.7.2 最后一行）在这里是**参数级**强制：
    非 `SELECT`/`WITH` 开头、含写关键字、或带分号想串第二条语句的，一律在契约
    校验阶段就被判非法，压根到不了执行器。
    """

    sql: str = Field(min_length=1, description="单条只读查询，仅允许 SELECT / WITH")
    params: dict[str, Any] = Field(default_factory=dict, description="绑定参数")
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator("sql")
    @classmethod
    def _read_only(cls, value: str) -> str:
        if not _READ_SQL_HEAD.match(value):
            raise ValueError("只允许 SELECT / WITH 开头的只读查询")
        if ";" in value.rstrip().rstrip(";"):
            raise ValueError("不允许多条语句（检出分号）")
        if _WRITE_SQL.search(value):
            raise ValueError("检出写操作关键字；除 memory.write 外禁止任何数据写入")
        return value


class PrereqCteParams(_Params):
    person_id: str = entity_field("person", "学员编号")
    mission_id: str = entity_field("mission", "课目编号")


class VectorSearchParams(_Params):
    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=50)
    collection: str = Field(default="", description="留空表示全部集合")


class Bm25SearchParams(_Params):
    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=50)


class RrfFuseParams(_Params):
    rankings: list[list[str]] = Field(min_length=1, description="多路召回的 doc_id 排序表")
    k: int = Field(default=60, ge=1, description="RRF 平滑常数（v6 §6.5.4 默认 60）")
    top_k: int = Field(default=10, ge=1, le=50)


class RerankParams(_Params):
    query: str = Field(min_length=1)
    candidates: list[str] = Field(min_length=1, description="待重排的 doc_id")
    top_k: int = Field(default=5, ge=1, le=50)


# ─────────────────────────────────────────────────────────────────────
# 第 8~9 行：记忆
# ─────────────────────────────────────────────────────────────────────

MemoryKind = Literal["semantic", "episodic", "procedural"]


class MemorySearchParams(_Params):
    query: str = Field(min_length=1)
    kinds: list[MemoryKind] = Field(default_factory=list, description="留空表示三类都查")
    top_k: int = Field(default=5, ge=1, le=50)


class MemoryWriteParams(_Params):
    """**全工具表里唯一的写工具**，且只有摄取抽取组件有权调用。

    `memory.advance_progress` 不在这里 —— 训练进度的推进发生在人工确认之后，
    是确定性节点 `commit_plan_node` 的职责（v6 §7.7.2 注）。
    """

    kind: MemoryKind
    key: str = Field(min_length=1)
    content: str = Field(min_length=1)
    valid_from: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    source: str = Field(default="", description="来源文档/快照，供时效性消解")


# ─────────────────────────────────────────────────────────────────────
# 第 10 行：Diagnosis 专属
# ─────────────────────────────────────────────────────────────────────


class MinConflictSetParams(_Params):
    iso_week: str = entity_field("week", "目标周")
    scope_persons: list[str] = Field(default_factory=list)


class BlameChainParams(_Params):
    person_id: str = entity_field("person", "涉事人员")
    mission_id: str = entity_field("mission", "涉事课目")


class ProbeSolveParams(_Params):
    """只读探针（v6 §7.7.2 的唯一例外，受 §3.9.2 独立预算池约束）。

    它不产出交付方案：结果必须经 `validate_node` 才能进入输出。
    """

    iso_week: str = entity_field("week", "目标周")
    relaxations: list[str] = Field(
        default_factory=list, description="试探性放开的松弛项 ID（v6 §3.9.1）"
    )
    time_limit_s: float = Field(default=30.0, gt=0, le=30.0, description="单次探针上限（§3.9.2）")


class RankRelaxationsParams(_Params):
    proposals: list[str] = Field(min_length=1, description="待排序的松弛提案 ID")
    prefer: Literal["least_debt", "least_disruption", "fastest"] = "least_debt"


# ─────────────────────────────────────────────────────────────────────
# 第 11 行：解释生成专属
# ─────────────────────────────────────────────────────────────────────


class RenderWorkbookParams(_Params):
    plan_id: str = Field(min_length=1)


class ComposeReportParams(_Params):
    plan_id: str = Field(min_length=1)
    sections: list[str] = Field(default_factory=list, description="留空表示 Sheet 4 七个区块全出")


class VerifyClaimParams(_Params):
    """解释文本的自核验（v6 §12.4.1 的 Faithfulness 落点）。"""

    claim: str = Field(min_length=1, description="待核验的一句话结论")
    evidence_refs: list[str] = Field(min_length=1, description="支撑证据的引用 ID")


# ─────────────────────────────────────────────────────────────────────
# 目录
# ─────────────────────────────────────────────────────────────────────


def _spec(
    name: str,
    description: str,
    model: type[BaseModel],
    *,
    deterministic: bool = True,
    writes: bool = False,
    budget_pool: Literal["default", "probe"] = "default",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        params_model=model,
        deterministic=deterministic,
        writes=writes,
        budget_pool=budget_pool,
    )


#: v6 §7.7.2 权限矩阵里出现的全部工具，一个不多一个不少。
TOOL_CATALOG: Final[dict[str, ToolSpec]] = {
    spec.name: spec
    for spec in (
        # ── 实体消解与交互 ──
        _spec("resolve_person", "把人员表述解析为 person_id", ResolvePersonParams),
        _spec("resolve_aircraft", "把飞机表述解析为 aircraft_id", ResolveAircraftParams),
        _spec("resolve_week", "把周表述解析为 ISO 周", ResolveWeekParams),
        _spec("ask_user", "向用户提一个必须回答的问题", AskUserParams, deterministic=False),
        _spec("escalate", "升级到人工处理", EscalateParams, deterministic=False),
        # ── Planner ──
        _spec("estimate_scope", "估算求解范围的规模", EstimateScopeParams),
        _spec("assess_disruption", "评估相对基线方案的影响面", AssessDisruptionParams),
        _spec("propose_solve_intent", "产出 SolveIntent", ProposeSolveIntentParams),
        _spec("translate_revision", "把修订原话翻译成增量约束", TranslateRevisionParams),
        _spec("check_authority", "核对角色是否有权授权该松弛档", CheckAuthorityParams),
        # ── 摄取抽取 ──
        _spec("classify_doc", "判定文档属于五类中的哪一类", ClassifyDocParams),
        _spec("parse_personnel", "抽取人员表", ParseDocParams),
        _spec("parse_aircraft", "抽取飞机表", ParseDocParams),
        _spec("parse_missions", "抽取课目表", ParseDocParams),
        _spec("parse_rules", "抽取规则条文", ParseDocParams),
        _spec("diff_snapshot", "比对两个快照的差异", DiffSnapshotParams),
        _spec("propose_change", "提出一条数据修改提案（不落库）", ProposeChangeParams),
        _spec("propose_rule_dsl", "把规则条文翻译成 DSL 草案", ProposeRuleDslParams),
        # ── 检索 ──
        _spec("sql_query", "只读 SQL 查询（禁止任何写操作）", SqlQueryParams),
        _spec("prereq_cte", "先修达标判定（递归 CTE）", PrereqCteParams),
        _spec("vector_search", "向量召回", VectorSearchParams),
        _spec("bm25_search", "BM25 关键词召回", Bm25SearchParams),
        _spec("rrf_fuse", "多路召回的 RRF 融合", RrfFuseParams),
        _spec("rerank", "交叉编码器重排", RerankParams),
        # ── 记忆 ──
        _spec("memory.search", "检索长期记忆", MemorySearchParams),
        _spec(
            "memory.write",
            "写入长期记忆（全表唯一的写工具）",
            MemoryWriteParams,
            deterministic=False,
            writes=True,
        ),
        # ── Diagnosis ──
        _spec("min_conflict_set", "求最小冲突集", MinConflictSetParams),
        _spec("blame_chain", "给出不可行的归因链", BlameChainParams),
        _spec(
            "probe_solve",
            "只读求解探针（受 §3.9.2 独立预算池约束）",
            ProbeSolveParams,
            deterministic=False,
            budget_pool="probe",
        ),
        _spec("rank_relaxations", "给松弛提案排序", RankRelaxationsParams),
        # ── 解释生成 ──
        _spec("render_workbook", "渲染四表工作簿", RenderWorkbookParams),
        _spec("compose_report", "组织 Sheet 4 的解释文本", ComposeReportParams),
        _spec("verify_claim", "核验一句结论是否有证据支撑", VerifyClaimParams),
    )
}


__all__ = [
    "ENTITY_PATTERNS",
    "ENTITY_SCHEMA_KEY",
    "TOOL_CATALOG",
    "entity_field",
    "entity_kind_of",
]
