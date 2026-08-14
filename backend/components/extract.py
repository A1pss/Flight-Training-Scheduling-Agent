"""LLM 节点 ③：`extract_llm` 自由文本抽取（v6 §7.2.3）。

> 单次受约束解码。情况文件等自由文本抽取；**结构化表格不经 LLM**。

## 它与 M1 的 `parse_situation_document` 是什么关系

M1 交付的 `backend/ingestion/parsers/freetext.py` 已经把「受约束解码 → Pydantic
→ 抽取失败即抛 `IngestionError`」这条链路做完了，**本模块不重做它**。M4-B 补的
是外面那一层：让这次调用**走 Harness**，于是它才有

- 预算计量（FTS-4003 熔断），
- 录制与重放（§12.5.2 的重放一致率靠它），
- `prompt_version` 随 trace 落盘（§7.7.1 第 8 行），
- 以及**知识层**：`doc-parsing/*` 的抽取要点按确定性路由加载（§7.8.3）。

裸 Provider 调用一样能抽出东西，但那次调用在 trace 里是隐形的，重放时也复现不了。

## Skill 在这里能做什么、不能做什么

能做：告诉模型「适配课目那一列会折行，有 `missionC1` 这种变体，注意看」。
不能做：**给出修复正则**。修复层在 `backend/ingestion/repair.py`，带后置断言，
抽不干净宁可抛 `IngestionError` 阻断（铁律 7）。§12.5.3 的 S6 专门验这一条：
改 `doc-parsing/aircraft/SKILL.md` 里关于 `missionC1` 的说明，**摄取结果不变**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

from backend.core.errors import IngestionError
from backend.harness import AgentSpec, ContextBlock, Harness
from backend.ingestion.parsers.freetext import FREETEXT_SCHEMA, SituationEvent
from backend.ingestion.prompts import wrap_untrusted
from backend.skills_loader import (
    SkillLibrary,
    ingest_conditions,
    render_skills,
    route_for_component,
)

#: 五类文档（`classify_doc` 的取值域）。`freetext` 对应情况文件。
DOC_KINDS: Final[tuple[str, ...]] = (
    "personnel",
    "aircraft",
    "mission",
    "rules",
    "exception",
)

EXTRACT_AGENT: Final[AgentSpec] = AgentSpec(
    name="extract",
    tools=(),
    requires_tool_call=False,
    output_schema=FREETEXT_SCHEMA,
)


@dataclass(frozen=True)
class ExtractionResult:
    """一次抽取的产物。"""

    events: tuple[SituationEvent, ...]
    skills_used: tuple[str, ...]
    llm_calls: int
    degraded: bool = False


def skill_context(library: SkillLibrary | None, doc_kind: str) -> tuple[str, tuple[str, ...]]:
    """按文档类型确定性地选 skill（v6 §7.8.3，**不让 LLM 选**）。"""
    names = route_for_component("extract", ingest_conditions(doc_kind))
    if library is None or library.empty or not names:
        return "", names
    return render_skills(library, names), names


def extract_events(
    text: str,
    *,
    doc_kind: str = "exception",
    harness: Harness,
    library: SkillLibrary | None = None,
    source: str = "",
) -> ExtractionResult:
    """从自由文本里抽出情况事件。

    **抽取失败绝不静默降级**（铁律 7）：JSON 不合法、schema 对不上、某条事件
    不合法——三种情况一律抛 `IngestionError` 阻断，不返回「抽到几条算几条」。
    """
    context, names = skill_context(library, doc_kind)
    blocks: list[ContextBlock] = []
    if context:
        blocks.append(ContextBlock(kind="evidence", content=context, label="skills"))
    # 文档正文进隔离标签（v6 §5.4 提示词注入防护），标签本身在 M1 里做了中和
    blocks.append(
        ContextBlock(kind="history", content=wrap_untrusted(text, source=source), role="user")
    )

    out = harness.call(EXTRACT_AGENT, blocks)
    if out.degraded:
        raise IngestionError(
            f"自由文本抽取降级（{out.error_code}）：{out.error_message or '契约重试耗尽'}",
            details={"doc_kind": doc_kind, "source": source, "error_code": out.error_code},
            suggestions=["检查 Provider 是否真的走了受约束解码", "或改用人工表单录入情况文件"],
        )

    events = _parse(out.text, doc_kind=doc_kind, source=source)
    return ExtractionResult(
        events=events, skills_used=names, llm_calls=out.llm_calls, degraded=False
    )


def _parse(raw: str, *, doc_kind: str, source: str) -> tuple[SituationEvent, ...]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IngestionError(
            f"自由文本抽取的 LLM 输出不是合法 JSON（{source or doc_kind}）",
            details={"raw": raw[:500], "error": str(exc)},
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise IngestionError(
            f"自由文本抽取结果不符合 schema（{source or doc_kind}）",
            details={"payload": str(payload)[:500]},
        )
    events: list[SituationEvent] = []
    for index, item in enumerate(payload["events"]):
        try:
            events.append(SituationEvent.model_validate(item))
        except Exception as exc:  # pydantic ValidationError 及其子类
            raise IngestionError(
                f"第 {index + 1} 条事件不合法：{exc}",
                details={"index": index, "item": item, "source": source},
            ) from exc
    return tuple(events)


__all__ = [
    "DOC_KINDS",
    "EXTRACT_AGENT",
    "ExtractionResult",
    "extract_events",
    "skill_context",
]
