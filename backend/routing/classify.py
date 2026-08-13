"""两级意图分类（v6 §7.2.1 / §7.5 的 `route`）。

```
一级：INTENT_RULES 正则匹配        → 命中即返回，**0 次 LLM 调用**（约 70% 请求）
二级：LLM 兜底，受约束解码到 6 类枚举 + 槽位
      → 置信度经 §7.3.5 校准；低于阈值转人工追问，宁可问不可猜
```

## 三条口径，写在前面免得反复解释

1. **规则命中路径一次 LLM 都不调**（v6 §7.6「规则命中即 0 次」）。所以槽位在
   这条路径上只能靠**确定性扫描**：把话里逐字出现的已知编号与名称捞出来。捞不到
   的就是没提到，不去猜。
2. **实体消解永远不经 LLM**。二级路径里 LLM 只负责给出**原文表述**
   （「何超」「下周」「49 号机」），编号一律由 `routing.entities` 的字典匹配 +
   编辑距离决定。模型编一个 `P08` 出来是 §12.5.1 的 `entity_hallucination`，
   这里从结构上不给它这个机会。
3. **LLM 挂了不等于排不了班**（FTS-4001）。二级路径失败时降级为
   「规则结果 + 表单追问」，`source="degraded"`，并在 `errors` 里如实记一条。
   求解链路完全不经 LLM，`/api/v1/schedule` 的结构化入口照常可用。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Literal

from backend.core.config import Settings, get_settings
from backend.core.errors import ErrorCode, FTSError, LLMSchemaError
from backend.harness import AgentSpec, ContextBlock, Harness
from backend.harness.types import AgentOutput
from backend.planner.calibration import (
    DEFAULT_CALIBRATOR,
    CalibrationFeatures,
    ConfidenceCalibrator,
    consistency_ratio,
)
from backend.routing.entities import (
    EntityDirectory,
    Resolution,
    collect_ambiguities,
    resolve_aircraft,
    resolve_mission,
    resolve_person,
    resolve_week,
    week_start_of,
)
from backend.routing.rules import match_rule, next_node_for
from backend.schemas.common import ErrorItem
from backend.schemas.intent import Intent, QueryRequest, SchedulingRequest

#: 六类意图的取值域。受约束解码的 enum 直接取它，模型编不出第七类。
INTENT_VALUES: Final[tuple[Intent, ...]] = (
    "schedule",
    "reschedule",
    "query",
    "ingest",
    "export",
    "unknown",
)

#: 二级路径的受约束输出 schema（v6 §7.2.1「受约束解码到 6 类枚举 + 槽位」）。
#: 槽位一律是**原文表述**，不是编号 —— 编号由 `resolve_*` 说了算。
INTENT_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENT_VALUES)},
        "persons": {"type": "array", "items": {"type": "string"}},
        "aircraft": {"type": "array", "items": {"type": "string"}},
        "missions": {"type": "array", "items": {"type": "string"}},
        "week": {"type": "string"},
    },
    "required": ["intent"],
}

#: 确定性扫描用的编号形态（与 `schemas.plan` 同源，只固定前缀）
_ID_SCANNERS: Final[tuple[tuple[str, str], ...]] = (
    ("person", r"P\d+"),
    ("aircraft", r"AC\d+"),
    ("mission", r"mission[A-Z]-\d+"),
)

#: 周次的确定性扫描：ISO 周、日期、相对周次
_WEEK_SCANNERS: Final[tuple[str, ...]] = (
    r"\d{4}-?W\d{1,2}",
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}",
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]",
    r"上上周|下下周|上一周|下一周|本周|这周|当周|下周|上周|前一周|次周",
)

Source = Literal["rule", "llm", "degraded"]


@dataclass(frozen=True)
class IntentResult:
    """一次意图分类的完整结果（v6 §7.5 `route` 消费的 `d`）。"""

    intent: Intent
    confidence: float
    source: Source
    next_node: str
    request: SchedulingRequest | QueryRequest | None = None
    resolutions: tuple[Resolution, ...] = ()
    ambiguities: tuple[dict[str, Any], ...] = ()
    llm_calls: int = 0
    #: self-consistency 一致率（仅二级路径有值）
    agreement: float = 1.0
    errors: tuple[ErrorItem, ...] = ()
    raw_text: str = ""

    @property
    def needs_clarification(self) -> bool:
        """要不要回头追问。

        两种情形合并在这里：**有歧义**（「郝超」到底是谁）与**置信度不足**
        （二级路径拿不准）。两者的处置相同——回路由组织追问，而不是挑一个继续。
        """
        return bool(self.ambiguities) or self.intent == "unknown"

    def below_threshold(self, threshold: float) -> bool:
        """v6 §7.5：`d.source == "llm" and d.confidence < CONFIDENCE_THRESHOLD`。

        **只对 LLM 兜底路径生效**。规则命中的 `confidence=1.0` 是确定性事实，
        不该被一个未拟合的阈值挡下来。
        """
        return self.source != "rule" and self.confidence < threshold


@dataclass
class _Slots:
    """扫描/抽取出的原文表述（尚未消解）。"""

    persons: list[str] = field(default_factory=list)
    aircraft: list[str] = field(default_factory=list)
    missions: list[str] = field(default_factory=list)
    week: str = ""


def scan_slots(text: str, directory: EntityDirectory) -> _Slots:
    """确定性槽位扫描：只认逐字出现的编号与名称。

    这是一级路径唯一的槽位来源。**故意只做精确匹配**——一级路径的承诺是「确定
    且可测」，往里塞模糊匹配会让同一句话在不同快照下解出不同的人。
    """
    import re

    slots = _Slots()
    for kind, pattern in _ID_SCANNERS:
        for token in re.findall(pattern, text):
            bucket = getattr(
                slots, {"person": "persons", "aircraft": "aircraft"}.get(kind, "missions")
            )
            if token not in bucket:
                bucket.append(token)

    for person_id, name in sorted(directory.persons.items()):
        if name and name in text and person_id not in slots.persons and name not in slots.persons:
            slots.persons.append(name)
    for mission_id, name in sorted(directory.missions.items()):
        if name and name in text and mission_id not in slots.missions:
            # 课目名在基准数据里成对重复（missionB-1/B-2 同名「导航飞行」），
            # 逐字命中会同时捞出两门 —— 那不是槽位，是歧义，交给消解层去判
            slots.missions.append(name)

    for pattern in _WEEK_SCANNERS:
        found = re.search(pattern, text)
        if found is not None:
            slots.week = found.group(0)
            break
    return slots


def _resolve_slots(
    slots: _Slots,
    directory: EntityDirectory,
    *,
    today: date,
) -> tuple[Resolution, ...]:
    out: list[Resolution] = []
    out.extend(resolve_person(s, directory) for s in slots.persons)
    out.extend(resolve_aircraft(s, directory) for s in slots.aircraft)
    out.extend(resolve_mission(s, directory) for s in slots.missions)
    if slots.week:
        out.append(resolve_week(slots.week, today=today))
    return tuple(out)


def _ids(resolutions: Sequence[Resolution], kind: str) -> list[str]:
    seen: list[str] = []
    for r in resolutions:
        if r.kind == kind and r.entity_id is not None and r.entity_id not in seen:
            seen.append(r.entity_id)
    return seen


def build_request(
    intent: Intent,
    raw_text: str,
    resolutions: Sequence[Resolution],
) -> SchedulingRequest | QueryRequest | None:
    """把消解结果装成 `state["request"]`。

    `unknown` 不产出 request —— 连是什么类型的请求都还没定，装一个空壳出来只会
    让下游误以为「已经解析好了」。
    """
    weeks = _ids(resolutions, "week")
    iso_week = weeks[0] if weeks else None
    if intent in ("schedule", "reschedule"):
        return SchedulingRequest(
            kind=intent,
            raw_text=raw_text,
            iso_week=iso_week,
            week_start=week_start_of(iso_week) if iso_week else None,
            persons=_ids(resolutions, "person"),
            aircraft=_ids(resolutions, "aircraft"),
            missions=_ids(resolutions, "mission"),
        )
    if intent in ("query", "ingest", "export"):
        return QueryRequest(
            kind=intent,
            raw_text=raw_text,
            question=raw_text,
            iso_week=iso_week,
            persons=_ids(resolutions, "person"),
            aircraft=_ids(resolutions, "aircraft"),
        )
    return None


# ─────────────────────────────────────────────────────────────────────
# 二级：LLM 兜底
# ─────────────────────────────────────────────────────────────────────
ROUTE_AGENT: Final[AgentSpec] = AgentSpec(
    name="route",
    tools=(),
    requires_tool_call=False,
    output_schema=INTENT_OUTPUT_SCHEMA,
)


def _parse_llm_payload(text: str) -> tuple[Intent, _Slots]:
    """解析受约束解码的输出。越界取值一律落到 `unknown`，不纠正、不猜。"""
    payload = json.loads(text)
    raw_intent = payload.get("intent", "unknown")
    intent: Intent = raw_intent if raw_intent in INTENT_VALUES else "unknown"
    slots = _Slots(
        persons=[str(s) for s in payload.get("persons", []) if str(s).strip()],
        aircraft=[str(s) for s in payload.get("aircraft", []) if str(s).strip()],
        missions=[str(s) for s in payload.get("missions", []) if str(s).strip()],
        week=str(payload.get("week", "") or ""),
    )
    return intent, slots


def llm_classify(
    text: str,
    *,
    harness: Harness,
    settings: Settings,
) -> tuple[Intent, _Slots, float, AgentOutput, int]:
    """二级分类。返回 (意图, 槽位, self-consistency 一致率, 首个输出, LLM 调用数)。

    self-consistency 按 v6 §7.3.5 采样 `SELF_CONSISTENCY_SAMPLES` 次：**首轮
    意图解析是高风险低频节点**，多花两次调用换一个能用的置信度信号，划算。
    """
    blocks = [ContextBlock(kind="history", content=text, role="user")]
    samples: list[str] = []
    outputs: list[AgentOutput] = []
    calls = 0

    for _ in range(max(settings.SELF_CONSISTENCY_SAMPLES, 1)):
        out = harness.call(ROUTE_AGENT, blocks)
        calls += out.llm_calls
        outputs.append(out)
        if out.degraded:
            break
        try:
            intent, _ = _parse_llm_payload(out.text)
        except (json.JSONDecodeError, AttributeError, TypeError):
            intent = "unknown"
        samples.append(intent)

    first = outputs[0]
    if first.degraded:
        raise LLMSchemaError(
            f"意图分类降级：{first.error_message or '契约重试耗尽'}",
            severity="WARN",
            stage="intent",
            details={"error_code": first.error_code},
        )

    intent, slots = _parse_llm_payload(first.text)
    return intent, slots, consistency_ratio(samples), first, calls


# ─────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────
def classify_intent(
    text: str,
    *,
    directory: EntityDirectory,
    today: date,
    harness: Harness | None = None,
    calibrator: ConfidenceCalibrator | None = None,
    settings: Settings | None = None,
) -> IntentResult:
    """两级意图分类（v6 §7.2.1 的 `classify_intent`）。

    `harness=None` 即**不允许走二级**：一级没命中就返回 `unknown` 交给追问。
    单元测试与 FTS-4001 降级路径都走这一支，一次 LLM 都不调。
    """
    cfg = settings or get_settings()
    cal = calibrator or DEFAULT_CALIBRATOR
    stripped = text.strip()

    # ── 一级：规则匹配（0 次 LLM 调用）────────────────────────────────
    hit = match_rule(stripped) if stripped else None
    if hit is not None:
        resolutions = _resolve_slots(scan_slots(stripped, directory), directory, today=today)
        return _finish(
            hit,
            confidence=1.0,
            source="rule",
            raw_text=stripped,
            resolutions=resolutions,
        )

    # ── 二级：LLM 兜底 ────────────────────────────────────────────────
    if harness is None:
        return _finish(
            "unknown",
            confidence=0.0,
            source="degraded",
            raw_text=stripped,
            resolutions=(),
            errors=(_llm_unavailable_error("未配置 Harness，二级意图分类不可用"),),
        )

    try:
        intent, slots, agreement, out, llm_calls = llm_classify(
            stripped, harness=harness, settings=cfg
        )
    except FTSError as exc:
        # FTS-4001 降级：意图解析退化为规则匹配 + 表单式追问，
        # **排班能力完全不受影响**（求解链路不依赖 LLM，v6 §9.3）。
        return _finish(
            "unknown",
            confidence=0.0,
            source="degraded",
            raw_text=stripped,
            resolutions=(),
            errors=(_llm_unavailable_error(exc.message),),
        )

    features = CalibrationFeatures.from_output(out.calibration_features(), agreement=agreement)
    resolutions = _resolve_slots(slots, directory, today=today)
    return _finish(
        intent,
        confidence=cal.predict(features),
        source="llm",
        raw_text=stripped,
        resolutions=resolutions,
        llm_calls=llm_calls,
        agreement=agreement,
    )


def _finish(
    intent: Intent,
    *,
    confidence: float,
    source: Source,
    raw_text: str,
    resolutions: tuple[Resolution, ...],
    llm_calls: int = 0,
    agreement: float = 1.0,
    errors: tuple[ErrorItem, ...] = (),
) -> IntentResult:
    ambiguities = tuple(collect_ambiguities(resolutions))
    return IntentResult(
        intent=intent,
        confidence=confidence,
        source=source,
        next_node=next_node_for(intent),
        request=build_request(intent, raw_text, resolutions) if raw_text else None,
        resolutions=resolutions,
        ambiguities=ambiguities,
        llm_calls=llm_calls,
        agreement=agreement,
        errors=errors,
        raw_text=raw_text,
    )


def _llm_unavailable_error(message: str) -> ErrorItem:
    """FTS-4001：LLM 不可用。**排班能力完整保留**，所以严重度是 WARN 不是 ERROR。"""
    return ErrorItem(
        code=ErrorCode.LLM_UNAVAILABLE,
        message=f"LLM 意图解析不可用，已降级为规则匹配 + 表单追问：{message}",
        severity="WARN",
        stage="intent",
        details={"degraded_to": "form_input"},
        suggestions=[
            "改用 POST /api/v1/schedule 结构化入口直接排班（求解链路不依赖 LLM）",
            "或在表单里补齐排班对象与周次后重试",
        ],
        retryable=True,
    )


__all__ = [
    "INTENT_OUTPUT_SCHEMA",
    "INTENT_VALUES",
    "ROUTE_AGENT",
    "IntentResult",
    "Source",
    "build_request",
    "classify_intent",
    "llm_classify",
    "scan_slots",
]
