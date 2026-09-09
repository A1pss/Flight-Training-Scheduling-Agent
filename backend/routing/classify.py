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
from dataclasses import dataclass, field, replace
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
from backend.routing.rules import SCHEDULING_INTENTS, match_rule, next_node_for
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
    #: ★ 本次分类的**校准特征原样留档**（`CalibrationFeatures.vector()` 的四项来源）。
    #  只读观测，不参与任何判定 —— 加它是因为 §7.3.5 的校准器要能**从日志重新拟合**，
    #  而 `confidence` 是校准器的**输出**，用输出去拟合输出是循环的。
    #  规则命中路径没有 LLM 往返，这里保持为空字典。
    calibration_features: dict[str, Any] = field(default_factory=dict)
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
        不该被一个未拟合的阈值挡下来。**这一条口径没有变。**

        ⚠️ 「缺信息该不该问」是**另一件事**，见 :attr:`missing_required_info` ——
        两者刻意分开：一个问「模型有多自信」（概率），一个问「用户说清楚了没有」
        （确定性）。混成一个方法会让 v6 §7.5 这句话失去意义。
        """
        return self.source != "rule" and self.confidence < threshold

    @property
    def missing_required_info(self) -> bool:
        """请求文本里缺少排班周次 —— 确定性地缺信息，必须反问。

        与阈值互不干扰，**对规则路径同样生效**。

        ## 为什么需要它（`Z-44`，M9-B 实测）

        期望反问的 62 条里 **17 条走规则路径**，其中 **16 条被直接执行** ——
        占全量 1080 的 **4.4%**，**单这一项就超过原 ≤4% 的目标**，
        而且阈值取任何值都碰不到它们。

        典型的是「给他排班」「排个班」「生成训练计划」：**规则命中的是
        「这是一次排班请求」，不是「排哪一周」**。把 `confidence=1.0`
        读成「整条请求都确定」，等于把意图的确定性借给了槽位。

        未点名人员按既有规格解释为 ``ALL``，不是缺失输入；真正会阻断排班的
        必需输入只有周次。这里只描述**请求文本**是否缺周次，图状态里已有的
        ``state["week_start"]`` 由 route 节点合并判断。

        ## ⚠️ 只对**排班类**意图生效（M9-B 实测收窄）

        第一版对所有意图生效，实测把查询类打残了：实验五 30 条 query 轨迹里
        **零工具轨迹从 5 条涨到 10 条**、缺失调用率 16.67% → 33.33%、
        工具选择 65.71% → 42.86%。

        原因是**查询类问题本来就常常不含任何槽位**：
        「IFR Route 的容量是多少？」没有人名、机号、周次，
        却完全不需要反问 —— 它要的是知识检索，不是排班参数。

        **排班必须知道「哪一周」，查询不必。** 同一条判据对前者是对的、
        对后者是错的，所以按意图收窄，而不是把判据调松。
        """
        if self.intent not in SCHEDULING_INTENTS:
            return False
        return bool(self.calibration_features.get("week_missing"))


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


def _looks_like_tool_call(surface: str) -> bool:
    """这个「槽位值」其实是一句工具调用表达式吗。

    槽位 surface 是**用户原话里的自然语言片段**（「何超」「下周」「2026-W03」），
    永远不会是函数调用。而实测模型会把工具调用当成槽位值交出来：

        week    = "resolve_week(2026W03)"
        week    = "resolve_week('当前')('ISO')()()"
        persons = ["resolve_person('何超')"]

    `merge_slots` 只在**扫描器抓到了东西**时才能压住它。原话里压根没有周次
    表述时（「何超现在能排 missionC-1 吗？」），扫描器没有候选可用，
    垃圾值就一路走到消解层 → `not_found` → 歧义 → 人工门禁，
    **knowledge 节点根本没机会运行**。M9-B 实测 30 条 query 轨迹里 7 条栽在这。

    ⚠️ **判据必须窄**：只认「已知工具名 + 左括号」。实体标签本身就带括号
    （`JL-9(AC84)`、`高超(P02)` 是名录里的合法写法），按「含括号」一刀切
    会把真槽位也删掉。
    """
    head = surface.strip().split("(", 1)
    return len(head) == 2 and head[0].strip() in _TOOL_NAME_PREFIXES


#: 会被模型误当成槽位值交出来的工具名（route 与 planner 两行 ACL 的并集）。
_TOOL_NAME_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "resolve_person",
        "resolve_aircraft",
        "resolve_week",
        "estimate_scope",
        "assess_disruption",
        "propose_solve_intent",
        "check_authority",
        "ask_user",
        "escalate",
    }
)


def merge_slots(scanned: _Slots, proposed: _Slots, *, raw_text: str | None = None) -> _Slots:
    """把确定性扫描结果与模型给的槽位**合并**，逐类以扫描结果优先。

    ## 这个函数为什么存在（M9-B 实测定位）

    二级路径原先**完全不跑 `scan_slots`**：一级用确定性扫描、二级直接采信模型
    自报的 surface。而实测下模型会把**工具调用表达式当成槽位值**交出来 ——

    ```
    「给所有人排 2026-W02 的班」        → week = "resolve_week(2026W02)"
    「把 2026 年 1 月 5 日那一周的班排出来」→ week = "resolve_week(2026, 1, 5)"
    「何超能不能排 missionB-1？」        → persons = ["resolve_person('何超')"]
    ```

    这些 surface 消解不了 → 记成歧义 → 整条请求被送去反问。而
    `2026-W03` **就逐字写在原话里**，`_WEEK_SCANNERS` 一抓一个准。
    M9-B 实测：360 条里 82 条因此被误判成 `ask_clarify`，端到端完成率
    63.11%（目标 ≥92%），且**全部落在 `source=llm`，规则路径一条都没有**。

    与 `Z-37`（模型自己去点 ACL 行之外的工具）是同一族：模型把「要抽取的槽位」
    当成了「要调用的工具」—— `resolve_person` / `resolve_week` 正在 route 自己
    那一行 ACL 上。

    ## 合并口径：兼并，不是覆盖

    - **扫描器抓到的那一类，用扫描器的**。它只认逐字出现的编号与名称，
      不可能把 `2026-W03` 写成一个函数调用。
    - **扫描器空着的那一类，保留模型的**。模型能处理「下周」「上上周」这类
      扫描器覆盖不到的口语表述，把它一并丢掉是**用一个缺陷换另一个缺陷**。

    所以两路互补：确定性的那部分不再被模型的自由发挥污染，模型的那部分
    继续负责扫描器抓不到的说法。
    """

    def clean(values: list[str]) -> list[str]:
        return [
            v
            for v in values
            if not _looks_like_tool_call(v) and (raw_text is None or v.strip() in raw_text)
        ]

    proposed_week = (
        ""
        if _looks_like_tool_call(proposed.week)
        or (raw_text is not None and proposed.week.strip() not in raw_text)
        else proposed.week
    )
    return _Slots(
        persons=list(scanned.persons) if scanned.persons else clean(proposed.persons),
        aircraft=list(scanned.aircraft) if scanned.aircraft else clean(proposed.aircraft),
        missions=list(scanned.missions) if scanned.missions else clean(proposed.missions),
        week=scanned.week or proposed_week,
    )


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
        # 规则路径同样记槽位消解质量：`below_threshold` 要用它做**确定性**的
        # 「一个槽位都没解出来就必须问」判据（`Z-44`）。置信度仍是 1.0 ——
        # 规则命中的意图是确定的，这一点没变。
        rule_features = _with_slot_quality(CalibrationFeatures(), resolutions)
        return _finish(
            hit,
            confidence=1.0,
            source="rule",
            raw_text=stripped,
            resolutions=resolutions,
            calibration_features={
                "no_slots_at_all": rule_features.no_slots_at_all,
                "week_missing": rule_features.week_missing,
                "has_ambiguity": rule_features.has_ambiguity,
            },
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
    # ★ 二级路径同样要过确定性扫描器，再与模型给的槽位兼并（见 `merge_slots`）。
    #   原先这里直接用 `slots`，模型把 surface 写成 `resolve_week(2026-W03)`
    #   就会一路走到歧义与反问。
    merged = merge_slots(
        scan_slots(stripped, directory),
        slots,
        # 查询会在 Knowledge 改写层再次做实体消解；这里只允许用户原文里
        # 确实出现的提示，避免模型幻觉槽位挡住知识查询。排班类则需要保留
        # LLM 从口语别称中抽出的 surface，再交给确定性目录消解。
        raw_text=stripped if intent == "query" else None,
    )
    resolutions = _resolve_slots(merged, directory, today=today)
    if intent == "query":
        # 查询的实体消解会在 Knowledge 改写层按原问题再做一次；route 这里只保留
        # 「确实来自用户原文、且已经确定解析」的提示。模型编出的 `AC01`、
        # `missionI-1`、`current` 既不该变成槽位，也不该制造歧义把查询挡在
        # Knowledge 之前。真正写在原文里的未知/歧义实体仍会由 Knowledge 按
        # §6.5.3 的同一套字典规则反问，不会被静默忽略。
        resolutions = tuple(r for r in resolutions if r.resolved and r.surface.strip() in stripped)
    # ★ 槽位消解质量进校准特征（`Z-44`）—— 「该不该反问」取决于**这句话说清楚
    #   了没有**，而不是「模型答得顺不顺」。前者在这里才有信号。
    features = _with_slot_quality(features, resolutions)
    if intent == "query":
        # `no_slots_at_all` / `week_missing` 是排班请求的缺输入信号，不是查询质量
        # 信号。查询可以合法地没有人、机、课目和周次（例如问空域容量）；继续
        # 扣分会让 Z-44 经由 below_threshold 绕回去误伤查询类。
        features = replace(features, no_slots_at_all=False, week_missing=False)
    return _finish(
        intent,
        confidence=cal.predict(features),
        source="llm",
        raw_text=stripped,
        resolutions=resolutions,
        llm_calls=llm_calls,
        agreement=agreement,
        calibration_features={
            "agreement": features.agreement,
            "first_pass": features.first_pass,
            "retries": features.retries,
            "worst_failure_mode": features.worst_failure_mode,
            # `Z-44` 的三项槽位消解质量 —— 不记就没法从日志重新拟合校准器
            "no_slots_at_all": features.no_slots_at_all,
            "week_missing": features.week_missing,
            "has_ambiguity": features.has_ambiguity,
        },
    )


def _with_slot_quality(
    features: CalibrationFeatures, resolutions: Sequence[Resolution]
) -> CalibrationFeatures:
    """把消解结果的质量补进校准特征（`Z-44`）。

    判据都取「**解出来了没有**」而不是「模型说了没有」：模型报了一个
    `resolve_week(...)` 却消解不了，等价于没说。
    """
    resolved = [r for r in resolutions if r.resolved]
    return replace(
        features,
        no_slots_at_all=not resolved,
        week_missing=not any(r.kind == "week" and r.resolved for r in resolutions),
        has_ambiguity=any(r.ambiguous for r in resolutions),
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
    calibration_features: dict[str, Any] | None = None,
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
        calibration_features=dict(calibration_features or {}),
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
