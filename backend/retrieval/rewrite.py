"""① 查询改写（v6 §6.5.2 / §6.5.3）。

> 改写是**唯一**需要 LLM 的环节，其余三阶段全为确定性代码。

四件事：

| 步 | 做什么 | 落点 |
|---|---|---|
| 指代消解 | 多轮里的「他」「那个课目」→ 具体实体 | :func:`resolve_anaphora` |
| 时间归一 | 本周 / 上上周 → ISO 周 + 日期区间 | :func:`normalize_time` |
| 术语对齐 | 口语 → 系统术语（起落航线 → A 类 → missionA-1/A-2） | `retrieval.terms` |
| 查询分解 | 复合问题拆成子查询 | :func:`decompose` |

## 实体消解不靠 LLM 猜（§6.5.3 硬要求）

> LLM 只负责识别「这里提到了一个人名」，实际映射走 `resolve_person` 工具做
> **精确字典匹配 + 编辑距离候选**。若匹配到多个候选或编辑距离过近
> （如同时命中何超/高超），**不自行选择，写入 `ambiguities` 触发反问**。

所以 LLM 在本模块的作用面只有一处：**从自由文本里圈出「这一段是个实体表述」**。
圈出来之后交给 `routing.entities` 的字典匹配，编号由字典决定。
LLM 挂了（FTS-4001）时走确定性的表述扫描 —— 覆盖面窄一些，但不会猜错编号。

## 保留原查询（§6.5.3 末段）

> 三路召回中，向量路同时检索改写后与原始查询，取并集。改写有时会丢失原句的
> 细微语义。

`RewrittenQuery.original_query` 永远是用户那句原话，`vector_queries()` 把
原句与语义化表述一起交给向量路。**这不是冗余** —— 改写会把「为什么推迟」
压缩成关键词，而「为什么」正是那句话的重点。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final

from backend.core.errors import FTSError
from backend.core.logging import get_logger
from backend.harness import AgentSpec, ContextBlock, Harness
from backend.retrieval.terms import Terminology, TermMatch, get_terminology
from backend.routing.entities import (
    EntityDirectory,
    Resolution,
    monday_of,
    resolve_aircraft,
    resolve_mission,
    resolve_person,
    resolve_week,
    week_start_of,
)
from backend.schemas.common import DateRange, EntityRef
from backend.schemas.retrieval import RewrittenQuery

logger = get_logger(__name__)

#: 改写调用的组件与提示词。改写属 Knowledge 一侧（它是问答链路的第一步）。
REWRITE_AGENT: Final[AgentSpec] = AgentSpec(
    name="knowledge",
    tools=(),
    prompt_key="rewrite",
    requires_tool_call=False,
    output_schema={
        "type": "object",
        "properties": {
            "person_surfaces": {"type": "array", "items": {"type": "string"}},
            "aircraft_surfaces": {"type": "array", "items": {"type": "string"}},
            "mission_surfaces": {"type": "array", "items": {"type": "string"}},
            "time_surfaces": {"type": "array", "items": {"type": "string"}},
            "sub_queries": {"type": "array", "items": {"type": "string"}},
            "semantic_query": {"type": "string"},
        },
        "required": ["sub_queries", "semantic_query"],
        "additionalProperties": False,
    },
)

#: 复合问题的切分符。**只切明确的并列**，不做句法分析
_SPLITTERS: Final[re.Pattern[str]] = re.compile(r"[；;？?]|，还有|，另外|，同时|，以及")

#: 指代词。命中即尝试从上一轮的实体里接上
_ANAPHORA: Final[tuple[str, ...]] = (
    "他",
    "她",
    "他们",
    "这个人",
    "那个人",
    "那个课目",
    "这个课目",
    "那门课",
    "这门课",
    "那架",
    "这架",
    "那架飞机",
    "这架飞机",
    "它",
)

#: 时间表述。ISO 周与相对周都交给 `routing.entities.resolve_week`
_TIME_SURFACES: Final[re.Pattern[str]] = re.compile(
    r"(上上周|下下周|上一周|下一周|前一周|本周|这周|当周|次周|上周|下周"
    r"|\d{4}-?W\d{1,2}|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*[日号]?)"
)


@dataclass
class ConversationTurn:
    """一轮对话里已消解出的实体（供下一轮做指代消解）。"""

    utterance: str
    entities: tuple[EntityRef, ...] = ()


@dataclass
class RewriteOutcome:
    """改写的完整产物。"""

    query: RewrittenQuery
    term_matches: tuple[TermMatch, ...] = ()
    #: 术语对齐出来的课目类别（供路 A 展开）
    mission_classes: tuple[str, ...] = ()
    runway_ids: tuple[str, ...] = ()
    airspace_ids: tuple[str, ...] = ()
    llm_calls: int = 0
    #: LLM 不可用时为 True —— 走了确定性扫描（覆盖面窄，但不会猜错编号）
    degraded: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def vector_queries(self) -> tuple[str, ...]:
        """交给向量路的查询集：**语义化表述 + 原句**（§6.5.3 末段）。"""
        out = [self.query.semantic_query or self.query.original_query, self.query.original_query]
        out.extend(self.query.sub_queries)
        seen: set[str] = set()
        unique: list[str] = []
        for q in out:
            text = q.strip()
            if text and text not in seen:
                seen.add(text)
                unique.append(text)
        return tuple(unique)

    def bm25_query(self) -> str:
        """交给 BM25 的查询：原句 + 对齐后的系统术语 + 消解出的编号。

        编号必须进去 —— 它们是这一路最擅长的低频 token（§6.5.1）。
        """
        parts = [self.query.original_query]
        parts.extend(self.query.keyword_terms)
        return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────
# 各步
# ─────────────────────────────────────────────────────────────────────
def resolve_anaphora(text: str, history: Sequence[ConversationTurn]) -> tuple[str, ...]:
    """指代消解：把「他」「那个课目」接到上一轮提到的实体上。

    **只往回看一轮**，且只接**上一轮唯一提到的**那一类实体。上一轮同时提到
    两个人时，「他」指谁是不确定的 —— 那种情况返回空，交由歧义反问处理。
    这与「何超 / 高超」并列时不自行选择是同一条规矩。
    """
    if not history or not any(word in text for word in _ANAPHORA):
        return ()
    last = history[-1]
    kinds_wanted: set[str] = set()
    if any(w in text for w in ("他", "她", "他们", "这个人", "那个人")):
        kinds_wanted.add("person")
    if any(w in text for w in ("课目", "门课")):
        kinds_wanted.add("mission")
    if any(w in text for w in ("那架", "这架", "飞机")):
        kinds_wanted.add("aircraft")
    if not kinds_wanted and "它" in text:
        kinds_wanted = {"mission", "aircraft"}

    out: list[str] = []
    for kind in sorted(kinds_wanted):
        same = [e for e in last.entities if e.kind == kind]
        if len(same) == 1:
            out.append(same[0].entity_id)
    return tuple(out)


def normalize_time(text: str, *, today: date) -> tuple[DateRange | None, str]:
    """时间归一：本周 / 上上周 / 1月5日 → ISO 周 + 日期区间。

    返回 `(区间, ISO 周)`。认不出返回 `(None, "")` —— **不默认成本周**。
    「刘斌的资质什么时候到期」里没有时间表述，硬安一个本周会让时间过滤
    把该召回的东西滤掉。
    """
    match = _TIME_SURFACES.search(text)
    if match is None:
        return None, ""
    resolution = resolve_week(match.group(), today=today)
    if resolution.entity_id is None:
        return None, ""
    start = week_start_of(resolution.entity_id)
    return DateRange(start=start, end=start + timedelta(days=6)), resolution.entity_id


def decompose(text: str) -> tuple[str, ...]:
    """查询分解：复合问题拆成子查询。

    只切明确的并列标记。**切不动就返回原句**（一条子查询），不硬拆 ——
    把「何超能不能排 missionB-1」从「能不能」处切开只会毁掉这个问题。
    """
    parts = [p.strip() for p in _SPLITTERS.split(text) if p and p.strip()]
    meaningful = [p for p in parts if len(p) >= 4]
    return tuple(meaningful) if len(meaningful) > 1 else (text.strip(),)


def scan_surfaces(text: str, directory: EntityDirectory) -> dict[str, list[str]]:
    """确定性的实体表述扫描（LLM 不可用时的那条路）。

    扫的是**名录里的名字与编号**，不做 NER —— 它认不出「郝超」是个人名，
    **也不该认**（M4-B §3.11 同一条理由：那是 NER 不是正则）。
    近音错字的消解由二级 LLM 路径圈表述、再交字典裁决。
    """
    found: dict[str, list[str]] = {"person": [], "aircraft": [], "mission": []}
    for kind, labels in (
        ("person", directory.persons),
        ("aircraft", directory.aircraft),
        ("mission", directory.missions),
    ):
        for entity_id, label in sorted(labels.items()):
            if entity_id in text and entity_id not in found[kind]:
                found[kind].append(entity_id)
            elif label and label in text and label not in found[kind]:
                found[kind].append(label)
    for match in re.finditer(r"\bAC\d+\b|\bP\d+\b|\bmission[A-Z]-\d+\b", text):
        token = match.group()
        kind = "aircraft" if token.startswith("AC") else "person" if token[0] == "P" else "mission"
        if token not in found[kind]:
            found[kind].append(token)
    return found


# ─────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────
def rewrite_query(
    text: str,
    *,
    directory: EntityDirectory,
    today: date,
    harness: Harness | None = None,
    history: Sequence[ConversationTurn] = (),
    terminology: Terminology | None = None,
    known_mission_classes: Sequence[str] = (),
    known_runways: Sequence[str] = (),
    known_airspaces: Sequence[str] = (),
) -> RewriteOutcome:
    """跑一次完整改写。

    LLM 只用来**圈表述**；编号一律由 `routing.entities` 的字典匹配决定。
    并列命中（何超 / 高超）写进 `ambiguities`，**不自行选择**。
    """
    original = text.strip()
    terms = terminology or get_terminology()

    surfaces: dict[str, list[str]] = {"person": [], "aircraft": [], "mission": []}
    sub_queries: list[str] = []
    semantic_query = ""
    llm_calls = 0
    degraded = harness is None
    notes: list[str] = []

    if harness is not None:
        try:
            out = harness.call(REWRITE_AGENT, _blocks(original, history))
            llm_calls = out.llm_calls
            if out.degraded:
                degraded = True
                notes.append(f"改写降级（{out.error_code}），已回落到确定性扫描")
            else:
                payload = _parse(out.text)
                for kind, key in (
                    ("person", "person_surfaces"),
                    ("aircraft", "aircraft_surfaces"),
                    ("mission", "mission_surfaces"),
                ):
                    surfaces[kind].extend(str(s) for s in payload.get(key, []) if str(s).strip())
                sub_queries.extend(str(s) for s in payload.get("sub_queries", []) if str(s).strip())
                semantic_query = str(payload.get("semantic_query", "")).strip()
        except FTSError as exc:
            degraded = True
            notes.append(f"改写中断（{exc.message}），已回落到确定性扫描")

    # 确定性扫描永远跑：LLM 圈的表述可能漏，字典能扫出来的一个都不该少
    for kind, items in scan_surfaces(original, directory).items():
        for item in items:
            if item not in surfaces[kind]:
                surfaces[kind].append(item)

    # ── 实体消解：字典匹配 + 编辑距离，并列即歧义 ────────────────────
    entities: list[EntityRef] = []
    ambiguities: list[str] = []
    resolvers = {
        "person": resolve_person,
        "aircraft": resolve_aircraft,
        "mission": resolve_mission,
    }
    for kind in ("person", "aircraft", "mission"):
        for surface in surfaces[kind]:
            resolution = resolvers[kind](surface, directory)
            _absorb(resolution, entities, ambiguities)

    # ── 指代消解：接上一轮 ──────────────────────────────────────────
    for entity_id in resolve_anaphora(original, history):
        if any(e.entity_id == entity_id for e in entities):
            continue
        previous = next(
            (e for turn in reversed(history) for e in turn.entities if e.entity_id == entity_id),
            None,
        )
        if previous is not None:
            entities.append(
                EntityRef(
                    kind=previous.kind,
                    entity_id=previous.entity_id,
                    surface=previous.surface,
                    confidence=previous.confidence,
                )
            )
            notes.append(f"指代消解：接上一轮的 {previous.entity_id}（{previous.surface}）")

    # ── 时间归一 ────────────────────────────────────────────────────
    timerange, iso_week = normalize_time(original, today=today)

    # ── 术语对齐 ────────────────────────────────────────────────────
    matches, term_ambiguities = terms.align(
        original,
        known_mission_classes=known_mission_classes,
        known_runways=known_runways,
        known_airspaces=known_airspaces,
    )
    ambiguities.extend(term_ambiguities)

    keyword_terms = _keywords(entities, matches, iso_week)
    if not sub_queries:
        sub_queries = list(decompose(original))
    if not semantic_query:
        semantic_query = _semantic_query(original, matches)

    query = RewrittenQuery(
        original_query=original,
        resolved_entities=entities,
        normalized_timerange=timerange,
        sub_queries=sub_queries,
        keyword_terms=keyword_terms,
        semantic_query=semantic_query,
        ambiguities=ambiguities,
    )
    return RewriteOutcome(
        query=query,
        term_matches=matches,
        mission_classes=tuple(m.target for m in matches if m.kind == "mission_class"),
        runway_ids=tuple(m.target for m in matches if m.kind == "runway"),
        airspace_ids=tuple(m.target for m in matches if m.kind == "airspace"),
        llm_calls=llm_calls,
        degraded=degraded,
        notes=tuple(notes),
    )


def _absorb(resolution: Resolution, entities: list[EntityRef], ambiguities: list[str]) -> None:
    """把一次消解结果并进产物。**歧义与查不到都要反问**，不静默丢。"""
    if resolution.entity_id is not None:
        if any(e.entity_id == resolution.entity_id for e in entities):
            return
        entities.append(
            EntityRef(
                kind=resolution.kind,
                entity_id=resolution.entity_id,
                surface=resolution.surface,
                confidence=resolution.confidence,
            )
        )
        return
    if resolution.reason == "ambiguous":
        options = "、".join(f"{c.label}({c.entity_id})" for c in resolution.candidates)
        note = f"「{resolution.surface}」有多个可能：{options}。请问是哪一个？"
    else:
        note = f"「{resolution.surface}」在当前快照里查不到对应的{resolution.kind}"
    if note not in ambiguities:
        ambiguities.append(note)


def _keywords(
    entities: Sequence[EntityRef], matches: Sequence[TermMatch], iso_week: str
) -> list[str]:
    """BM25 的关键词：编号 + 系统术语 + ISO 周。**都是低频 token**。"""
    out: list[str] = [e.entity_id for e in entities]
    out.extend(m.as_term() for m in matches)
    if iso_week:
        out.append(iso_week)
    seen: set[str] = set()
    unique: list[str] = []
    for token in out:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def _semantic_query(original: str, matches: Sequence[TermMatch]) -> str:
    """向量路的语义化表述：原句 + 术语补注。

    **不删原句里的任何字**：改写在这一路是「加信息」，不是「换说法」。
    删字是改写丢语义的主要来源，而 §6.5.3 特意为此要求保留原查询。
    """
    if not matches:
        return original
    extra = "、".join(f"{m.surface}即{m.as_term()}" for m in matches)
    return f"{original}（{extra}）"


def _blocks(text: str, history: Sequence[ConversationTurn]) -> list[ContextBlock]:
    lines = [
        "请从下面这句提问里圈出实体表述、拆出子查询、给一句语义化表述。",
        "**只圈表述，不要给编号** —— 编号由系统的字典匹配决定。",
        f"提问：{text}",
    ]
    if history:
        recent = "；".join(t.utterance for t in history[-2:])
        lines.append(f"最近两轮：{recent}")
    return [ContextBlock(kind="history", content="\n".join(lines), role="user")]


def _parse(text: str) -> Mapping[str, Any]:
    """解析受约束解码的产物。**解析不了就当空**，由确定性扫描兜底。"""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def week_range(iso_week: str) -> DateRange:
    """`2026W02` → 该周的日期区间（周一~周日）。"""
    start = week_start_of(iso_week)
    return DateRange(start=start, end=start + timedelta(days=6))


def this_week(today: date) -> DateRange:
    """「本周」的区间。`today` 由调用方给 —— 图里不调 `date.today()`。"""
    start = monday_of(today)
    return DateRange(start=start, end=start + timedelta(days=6))


__all__ = [
    "REWRITE_AGENT",
    "ConversationTurn",
    "RewriteOutcome",
    "decompose",
    "normalize_time",
    "resolve_anaphora",
    "rewrite_query",
    "scan_surfaces",
    "this_week",
    "week_range",
]
