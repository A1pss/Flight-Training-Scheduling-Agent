"""情况文件（请假 / 维修通知）→ 结构化事件，**唯一走 LLM 的抽取路径**。

四份基准 PDF 全是规整表格，走不到这里。但请假条、临时停飞通知这类文档天生
是自由文本，规则抽取会漏。v6 §5.1 对这条路径的要求是「LLM 受约束解码
（Ollama `format=JSON Schema`）→ Pydantic」，三层注入防护全部生效：

1. 文档包进 `<untrusted_document>`（:mod:`backend.ingestion.prompts`）
2. 输出被 :data:`FREETEXT_SCHEMA` 强约束
3. 结果仍要过 Pydantic 值域 + Diff 人工门禁才能落库

**抽不出来就抛 `IngestionError`，不猜**（铁律 7）。模型返回 schema 外内容、
日期不合法、引用了不存在的人员/机号 —— 全部阻断，不做「尽力而为的部分入库」。
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core.errors import IngestionError
from backend.ingestion.adapters import ExtractedDocument
from backend.ingestion.prompts import build_extraction_messages
from backend.llm.provider import LLMProvider
from backend.schemas.plan import AIRCRAFT_ID_PATTERN, PERSON_ID_PATTERN

EventKind = Literal["person_unavailable", "aircraft_maintenance"]

#: 受约束解码的 JSON Schema（Ollama `format` 参数）
FREETEXT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["person_unavailable", "aircraft_maintenance"],
                    },
                    "subject_id": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["kind", "subject_id", "start_date", "end_date", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}

_INSTRUCTION: Final[str] = (
    "从下面的情况文件里抽出全部「人员不可用」与「飞机维护」事件，只输出 JSON。\n"
    "- kind=person_unavailable 时 subject_id 填人员编号（形如 P03）\n"
    "- kind=aircraft_maintenance 时 subject_id 填机号（形如 AC73）\n"
    "- 日期一律用 YYYY-MM-DD；单日事件的 start_date 与 end_date 相同\n"
    "- 文档里没写的事件不要补，宁可少抽也不要编造"
)


class SituationEvent(BaseModel):
    """一条抽出来的情况事件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EventKind
    subject_id: str = Field(min_length=1)
    start_date: date
    end_date: date
    reason: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> SituationEvent:
        if self.end_date < self.start_date:
            raise ValueError(f"end_date({self.end_date}) 早于 start_date({self.start_date})")
        return self

    @model_validator(mode="after")
    def _subject_matches_kind(self) -> SituationEvent:
        """主体编号要与事件类型对得上（人员事件配人员编号，飞机事件配机号）。

        编号只校验**前缀约定、不限位数** —— v6 §5.1.1：编号由上传数据决定，
        换一个训练单位可能是三位编号。位数写死会让 `P100` 这种合法编号被拒
        （业务方 2026-08-11 裁定，与附录 B 同一次放宽）。
        """
        expected = PERSON_ID_PATTERN if self.kind == "person_unavailable" else AIRCRAFT_ID_PATTERN
        if not re.match(expected, self.subject_id):
            raise ValueError(f"{self.kind} 的 subject_id {self.subject_id!r} 不符合 {expected}")
        return self


def parse_situation_document(
    doc: ExtractedDocument, provider: LLMProvider
) -> tuple[SituationEvent, ...]:
    """情况文件主入口：受约束解码 → Pydantic。"""
    messages = build_extraction_messages(_INSTRUCTION, doc.text, source=doc.path.name)
    raw = provider.complete(messages, schema=FREETEXT_SCHEMA, temperature=0.0)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IngestionError(
            f"情况文件抽取的 LLM 输出不是合法 JSON：{doc.path.name}",
            details={"path": str(doc.path), "raw": raw[:500], "error": str(exc)},
            suggestions=["检查 Provider 是否真的走了受约束解码（schema 参数）"],
        ) from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise IngestionError(
            f"情况文件抽取结果不符合 schema：{doc.path.name}",
            details={"path": str(doc.path), "payload": str(payload)[:500]},
        )

    events: list[SituationEvent] = []
    for index, item in enumerate(payload["events"]):
        try:
            events.append(SituationEvent.model_validate(item))
        except Exception as exc:  # pydantic ValidationError 及其子类
            raise IngestionError(
                f"情况文件第 {index + 1} 条事件不合法：{exc}",
                details={"path": str(doc.path), "index": index, "item": item},
            ) from exc
    return tuple(events)


__all__ = ["FREETEXT_SCHEMA", "EventKind", "SituationEvent", "parse_situation_document"]
