"""实验一 · 自然语言交互准确率（v6 §12.2）。

## 这个 runner 的一个关键设计：**先记观测，后判动作**

反问阈值要**由误执行率反推**（§12.2），而消融第二项又要求「去掉置信度阈值
反问机制」再看一遍指标。如果 runner 在跑的时候就把阈值判进去，这两件事各要
重跑一遍 360×3。

所以这里的做法是：**把原始观测记全**（意图、置信度、来源、歧义、以及
Planner 到底问没问），动作由 `action_at_threshold()` 事后算。于是

- 阈值扫描（反推那一步）：**零次额外 LLM 调用**；
- 消融「去掉阈值反问」：**零次额外 LLM 调用**（阈值取 0 即可）。

代价是**排班类意图一律要跑一次 Planner**，哪怕它的置信度已经低于任何合理
阈值。这笔多花的调用是划算的 —— 换来的是上面两项都不用重跑。

## 温度的一处口径，必须写在报告里

§12.2 的协议写「温度 0」，但**二级路径的 self-consistency 采样是
`SELF_CONSISTENCY_TEMPERATURE=0.7`**（§7.3.5），这是生产配置且不能改成 0：
一致率是校准器的主要特征，温度 0 下它恒为 1.0，校准器就没东西可学了。

**后果**：规则命中的那 161 条是确定性的，走 LLM 的 199 条不是。
所以本组的「×3 轮」与 M7 的 `Z-38` **不是一回事** —— M7 温度 0，三轮验的是
稳定性；这里 LLM 路径真的有采样方差，三轮给的是**真方差**，Wilson 区间
按 360 条的比例算、轮间差异另行报告。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

from backend.core.config import Settings
from backend.core.errors import FTSError
from backend.harness import Harness
from backend.planner.intent import plan_solve_intent
from backend.routing import classify as _classify_module
from backend.routing.classify import classify_intent
from backend.routing.entities import EntityDirectory
from backend.routing.rules import SCHEDULING_INTENTS
from backend.schemas.intent import SchedulingRequest

#: 五类槽位（§12.2 的「人员/飞机/课目/周次/约束修饰」）。
SLOT_KINDS: tuple[str, ...] = ("persons", "aircraft", "missions", "week", "constraint_modifiers")

#: 「执行了」的动作集合 —— 误执行率的分子只看这些。
EXECUTING_ACTIONS: frozenset[str] = frozenset(
    {"solve", "reschedule", "answer", "route_ingest", "route_export"}
)


@dataclass
class NLObservation:
    """一条 nl_360 样本跑一轮的**原始观测**（不含任何阈值判断）。"""

    item_id: str
    layer: str
    round_index: int
    expected_intent: str
    expected_action: str
    expected_slots: dict[str, Any]
    # ── 一级/二级分类的观测 ───────────────────────────────────────
    observed_intent: str
    confidence: float
    source: str
    agreement: float
    has_ambiguity: bool
    llm_calls: int
    #: 校准特征原样留档 —— §12.2 要「在这 360 条上**拟合**校准器」，
    #  而拟合要的是特征，不是 `confidence`（那是校准器的输出）。
    calibration_features: dict[str, Any] = field(default_factory=dict)
    # ── Planner 的观测（仅排班类意图非空）────────────────────────
    planner_ran: bool = False
    planner_asked: bool = False
    planner_questions: list[str] = field(default_factory=list)
    observed_slots: dict[str, Any] = field(default_factory=dict)
    wall_s: float = 0.0
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _observed_slots(
    request: SchedulingRequest | Any,
    modifiers: Sequence[Any],
) -> dict[str, Any]:
    """把系统真实抽到的槽位摊成与标注同形的字典。

    `constraint_modifiers` 只记 `kind` —— 标注侧的 `surface` 是原话切片，
    两边逐字比对等于在测字符串相等而不是抽取正确性（M7 §4.2 踩过一次）。
    """
    if request is None:
        return {k: [] for k in SLOT_KINDS} | {"week": None}
    return {
        "persons": list(getattr(request, "persons", []) or []),
        "aircraft": list(getattr(request, "aircraft", []) or []),
        "missions": list(getattr(request, "missions", []) or []),
        "week": getattr(request, "iso_week", None),
        "constraint_modifiers": [str(getattr(m, "kind", "")) for m in modifiers],
    }


def run_item(
    item: Mapping[str, Any],
    *,
    directory: EntityDirectory,
    today: date,
    harness: Harness | None,
    settings: Settings,
    round_index: int,
    use_rules: bool = True,
) -> NLObservation:
    """跑一条样本，**只观测不判定**。

    `use_rules=False` 即消融第一项「去掉规则分类器，全走 LLM」——
    它不是把规则删掉，而是绕开一级直接进二级（见 `_classify_without_rules`）。
    """
    started = time.monotonic()
    utterance = str(item["utterance"])
    obs_kwargs: dict[str, Any] = {
        "item_id": str(item["item_id"]),
        "layer": str(item["layer"]),
        "round_index": round_index,
        "expected_intent": str(item["expected_intent"]),
        "expected_action": str(item["expected_action"]),
        "expected_slots": dict(item["expected_slots"]),
    }

    try:
        result = (
            classify_intent(
                utterance,
                directory=directory,
                today=today,
                harness=harness,
                settings=settings,
            )
            if use_rules
            else _classify_without_rules(
                utterance, directory=directory, today=today, harness=harness, settings=settings
            )
        )
    except FTSError as exc:
        return NLObservation(
            observed_intent="unknown",
            confidence=0.0,
            source="degraded",
            agreement=0.0,
            has_ambiguity=False,
            llm_calls=0,
            wall_s=time.monotonic() - started,
            error=f"{exc.__class__.__name__}: {exc}",
            **obs_kwargs,
        )

    obs = NLObservation(
        observed_intent=result.intent,
        confidence=result.confidence,
        source=result.source,
        agreement=result.agreement,
        has_ambiguity=bool(result.ambiguities),
        llm_calls=result.llm_calls,
        calibration_features=dict(result.calibration_features),
        **obs_kwargs,
    )

    # ★ 排班类意图**一律**跑 Planner，哪怕置信度低于任何阈值 ——
    #   这样阈值扫描与「去掉阈值」消融事后算就够了，不用重跑。
    if result.intent in SCHEDULING_INTENTS and harness is not None:
        try:
            decision = plan_solve_intent(
                result.request,
                user_role="director",
                harness=harness,
                settings=settings,
            )
            obs.planner_ran = True
            obs.planner_asked = bool(decision.needs_clarification)
            obs.planner_questions = list(decision.intent.open_questions)
            obs.observed_slots = _observed_slots(
                result.request, decision.intent.incremental_constraints
            )
            obs.llm_calls += decision.llm_calls
        except FTSError as exc:
            obs.error = f"planner: {exc.__class__.__name__}: {exc}"
            obs.observed_slots = _observed_slots(result.request, ())
    else:
        obs.observed_slots = _observed_slots(result.request, ())

    obs.wall_s = time.monotonic() - started
    return obs


def _classify_without_rules(
    text: str,
    *,
    directory: EntityDirectory,
    today: date,
    harness: Harness | None,
    settings: Settings,
) -> Any:
    """消融一：绕开一级规则，直接走 LLM 二级。

    实现方式是在调用期把一级的 `match_rule` 临时替换成「永远不命中」，
    而不是给 `classify_intent` 加一个开关 —— 生产代码不该为了消融长出一个
    开关（那个开关会一直留在那儿，而且迟早有人在生产里把它打开）。
    """
    from unittest.mock import patch

    with patch.object(_classify_module, "match_rule", return_value=None):
        return classify_intent(
            text, directory=directory, today=today, harness=harness, settings=settings
        )


# ─────────────────────────────────────────────────────────────────────
# 事后判定：动作、指标
# ─────────────────────────────────────────────────────────────────────
def action_at_threshold(obs: Mapping[str, Any], threshold: float) -> str:
    """由原始观测推出系统的最终动作。

    判定顺序本身就是规格（§7.5 + §12.2）：

    1. `unknown` → **refuse**。连是什么类型的请求都没定，系统不动手。
    2. 有歧义（「郝超」到底是谁）→ **ask_clarify**。
    3. 二级路径且置信度低于阈值 → **ask_clarify**。⚠️ 规则命中的
       `confidence=1.0` 不受阈值管辖（`IntentResult.below_threshold` 的口径）。
    4. 排班类：Planner 追问了 → **ask_clarify**，否则 solve / reschedule。
    5. 其余意图各自的承接动作。
    """
    intent = str(obs["observed_intent"])
    if intent == "unknown":
        return "refuse"
    if bool(obs["has_ambiguity"]):
        return "ask_clarify"
    if str(obs["source"]) != "rule" and float(obs["confidence"]) < threshold:
        return "ask_clarify"
    if intent in ("schedule", "reschedule"):
        if bool(obs.get("planner_asked")):
            return "ask_clarify"
        return "solve" if intent == "schedule" else "reschedule"
    if intent == "query":
        return "answer"
    if intent == "ingest":
        return "route_ingest"
    if intent == "export":
        return "route_export"
    return "ask_clarify"


@dataclass(frozen=True)
class SlotCounts:
    """槽位抽取的微平均计数。"""

    tp: int = 0
    fp: int = 0
    fn: int = 0

    def merged(self, other: SlotCounts) -> SlotCounts:
        return SlotCounts(self.tp + other.tp, self.fp + other.fp, self.fn + other.fn)

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def _as_multiset(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            if isinstance(v, dict):
                out.append(str(v.get("kind", "")))
            else:
                out.append(str(v))
        return sorted(out)
    return [str(value)]


#: 排班类意图下「没点名」的语义就是全体 —— `deterministic_intent` 正是这么做的
#: （`scope_persons = "ALL" if not request.persons`）。
_SCHEDULING = frozenset({"schedule", "reschedule"})


def _normalize_scope(
    pred: list[str], gold: list[str], kind: str, obs: Mapping[str, Any]
) -> list[str]:
    """把「空列表」还原成它真正的语义 `ALL`。

    标注侧用 `["ALL"]` 表示「给所有人排」，而 `SchedulingRequest.persons` 在
    没点名时是**空列表** —— 两者说的是同一件事，`SolveIntent.scope_persons`
    落到的也确实是 `"ALL"`。不还原就会把 152 条标准/指定排班的 persons 槽
    **全判成漏抽**，槽位 F1 被系统性压低（M9-A §4.1 抓到过同构的一处：
    「查询不需要周次」的想当然，漏标 9 条）。

    只对排班类意图生效：查询类的空人员列表就是「没提到人」，不是「所有人」。
    """
    if kind != "persons" or pred:
        return pred
    if str(obs.get("observed_intent", "")) in _SCHEDULING and gold == ["ALL"]:
        return ["ALL"]
    return pred


def slot_counts(obs: Mapping[str, Any], kind: str) -> SlotCounts:
    """单条样本、单类槽位的 TP/FP/FN。

    多值槽位按**多重集**比对：抽到两次同一个人只算一次命中，多抽出来的那次
    是一个 FP。
    """
    gold = _as_multiset(dict(obs["expected_slots"]).get(kind))
    pred = _as_multiset(dict(obs.get("observed_slots") or {}).get(kind))
    pred = _normalize_scope(pred, gold, kind, obs)
    remaining = list(pred)
    tp = 0
    for g in gold:
        if g in remaining:
            remaining.remove(g)
            tp += 1
    return SlotCounts(tp=tp, fp=len(remaining), fn=len(gold) - tp)


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """逐行读回落盘的观测。"""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield cast(dict[str, Any], json.loads(line))


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    """一条一行地追加 —— 中断了也不丢已跑的部分。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


Metric = Literal["completion", "intent", "misexec_over_all", "misexec_over_unclear"]


__all__ = [
    "EXECUTING_ACTIONS",
    "SLOT_KINDS",
    "Metric",
    "NLObservation",
    "SlotCounts",
    "action_at_threshold",
    "append_jsonl",
    "iter_jsonl",
    "run_item",
    "slot_counts",
]
