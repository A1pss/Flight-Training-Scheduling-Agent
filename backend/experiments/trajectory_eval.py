"""实验五 · 智能体轨迹评估（v6 §12.6）。

§12.6.2 写明「判定全部可自动化：工具与参数用结构化精确比对，路径用最长公共
子序列相似度 —— 八项指标没有一项依赖人工复核」。本模块就是那套判定。

## 参数比对按字段类别分开（M7 §4.2 的教训）

M7 实测过一次：`propose_solve_intent` 的参数精确匹配率只有 86.5%，逐条查完
发现**那 13.5% 全是自由文本**（`rationale`、`query` 这类）。一个具体例子 ——
期望 `query="张勇 的训练情况"`，模型给 `query="张勇 的 训练 情况"`，
**给 BM25 做了分词，工程上更对**，却因字符串不等被判成错。

所以这里：**实体编号 / 枚举 / 数值 / 布尔精确匹配，自由文本不比对**。
否则测的是字符串相等，不是正确性。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: 自由文本字段 —— 不参与精确比对（理由见模块 docstring）。
FREE_TEXT_FIELDS: frozenset[str] = frozenset(
    {
        "rationale",
        "query",
        "question",
        "reason",
        "comment",
        "freeze_reason",
        "origin_utterance",
        "summary",
        "text",
        "note",
        "notes",
        "explanation",
    }
)


def lcs_length(a: Sequence[str], b: Sequence[str]) -> int:
    """最长公共子序列长度（§12.6.2 指定的路径判定方式）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(cur[j], prev[j + 1]))
        prev = cur
    return prev[-1]


def path_similarity(observed: Sequence[str], expected: Sequence[str]) -> float:
    """LCS 相似度：`2·LCS / (|a|+|b|)`（Dice 形态，两边都惩罚）。

    用 Dice 而不是 `LCS/|expected|`：后者对**多走了三步**毫无惩罚，
    而「冗余调用」正是本组要抓的失效之一。
    """
    if not observed and not expected:
        return 1.0
    total = len(observed) + len(expected)
    if total == 0:
        return 1.0
    return 2.0 * lcs_length(observed, expected) / total


def path_is_correct(
    observed: Sequence[str],
    expected: Sequence[str],
    acceptable: Sequence[Sequence[str]] = (),
    forbidden: Sequence[Sequence[str]] = (),
) -> tuple[bool, str]:
    """路径正确性。

    判定顺序刻意把 `forbidden` 放在最前：**禁止路径即便与期望路径相似度很高
    也必须判错**（数据集的 D/E/F 三条否决规则 —— 跳过确定性节点、弱工具替代
    强工具、不调工具直接回答）。相似度高恰恰是这类错误危险的地方。
    """
    obs = list(observed)
    for bad in forbidden:
        if obs == list(bad):
            return False, "命中 forbidden_path"
    if obs == list(expected):
        return True, "与 expected_path 逐元素相同"
    for ok in acceptable:
        if obs == list(ok):
            return True, "命中 acceptable_path"
    return False, "既非期望路径也不在可接受集合内"


def _comparable(params: Mapping[str, Any]) -> dict[str, Any]:
    """剔除自由文本字段后的可比参数。"""
    return {k: v for k, v in params.items() if k not in FREE_TEXT_FIELDS}


def params_match(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    """参数是否算对：**只比对期望里给出的非自由文本字段**。

    不要求模型一个多余字段都不填 —— 多填一个可选字段不是错误；
    少填或填错一个**被标注的**字段才是。
    """
    exp = _comparable(expected)
    for key, want in exp.items():
        if key not in observed:
            return False
        got = observed[key]
        if isinstance(want, list) and isinstance(got, list):
            if sorted(map(str, want)) != sorted(map(str, got)):
                return False
        elif str(want) != str(got):
            return False
    return True


def _call_key(name: str, args: Mapping[str, Any]) -> tuple[str, str]:
    """一次调用的去重键：工具名 + 可比参数（自由文本不进键）。"""
    return (name, str(sorted((k, str(v)) for k, v in _comparable(args).items())))


@dataclass
class StepScore:
    """一条轨迹的工具层判定。"""

    expected_steps: int = 0
    #: 该调工具的步骤上，选对工具的次数
    tool_hits: int = 0
    #: 工具选对的前提下，参数完全正确的次数
    param_hits: int = 0
    param_denominator: int = 0
    #: 该调却没调（**静默失效**，§12.6 最重要的一条）
    missing: int = 0
    #: 对结果无贡献的调用（重复查同一实体）
    redundant: int = 0
    observed_calls: int = 0


def score_steps(
    expected: Sequence[Mapping[str, Any]],
    observed: Sequence[tuple[str, Mapping[str, Any]]],
) -> StepScore:
    """逐步比对。

    `observed` 是 `(工具名, 参数)` 的有序序列。**可选步骤缺席不计缺失**
    （数据集规则 B：信息已足够时省略可选步骤是可接受的）。
    """
    score = StepScore(expected_steps=len(expected), observed_calls=len(observed))
    remaining = list(observed)
    seen: list[tuple[str, str]] = []

    for step in expected:
        tool = str(step["tool"])
        alts = {str(a) for a in (step.get("alternatives") or [])} | {tool}
        idx = next((i for i, (name, _) in enumerate(remaining) if name in alts), None)
        if idx is None:
            if not step.get("optional"):
                score.missing += 1
            continue
        name, args = remaining.pop(idx)
        seen.append(_call_key(name, args))
        score.tool_hits += 1
        score.param_denominator += 1
        if params_match(args, step.get("params") or {}):
            score.param_hits += 1

    # 剩下没被任何期望步骤认领的调用里，**重复查同一 (工具, 实体)** 的算冗余。
    # `seen` 已经装了被认领的那些 —— 「又查了一遍刚查过的东西」正是最典型的
    # 冗余形态，只在剩余项之间比会把它漏掉。
    for name, args in remaining:
        key = _call_key(name, args)
        if key in seen:
            score.redundant += 1
        seen.append(key)
    return score


@dataclass
class TrajectoryOutcome:
    """一条轨迹的完整判定结果。"""

    item_id: str
    flow: str
    observed_path: list[str] = field(default_factory=list)
    expected_path: list[str] = field(default_factory=list)
    path_ok: bool = False
    path_reason: str = ""
    path_similarity: float = 0.0
    steps: StepScore = field(default_factory=StepScore)
    #: validate→solve 回环触发且非规格 bug（§12.6「无效回环率 = 0」）
    invalid_loop: bool = False
    revision_translation_ok: bool | None = None
    revision_rollback_ok: bool | None = None
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "flow": self.flow,
            "observed_path": self.observed_path,
            "expected_path": self.expected_path,
            "path_ok": self.path_ok,
            "path_reason": self.path_reason,
            "path_similarity": self.path_similarity,
            "steps": {
                "expected_steps": self.steps.expected_steps,
                "tool_hits": self.steps.tool_hits,
                "param_hits": self.steps.param_hits,
                "param_denominator": self.steps.param_denominator,
                "missing": self.steps.missing,
                "redundant": self.steps.redundant,
                "observed_calls": self.steps.observed_calls,
            },
            "invalid_loop": self.invalid_loop,
            "revision_translation_ok": self.revision_translation_ok,
            "revision_rollback_ok": self.revision_rollback_ok,
            "error": self.error,
        }


def aggregate(outcomes: Sequence[TrajectoryOutcome]) -> dict[str, Any]:
    """§12.6.1 的八项指标。分母口径逐项写清楚，不合并。"""
    ok = [o for o in outcomes if not o.error]
    exp_steps = sum(o.steps.expected_steps for o in ok)
    tool_hits = sum(o.steps.tool_hits for o in ok)
    param_den = sum(o.steps.param_denominator for o in ok)
    param_hits = sum(o.steps.param_hits for o in ok)
    missing = sum(o.steps.missing for o in ok)
    observed = sum(o.steps.observed_calls for o in ok)
    redundant = sum(o.steps.redundant for o in ok)
    trans = [o for o in ok if o.revision_translation_ok is not None]
    roll = [o for o in ok if o.revision_rollback_ok is not None]

    return {
        "n_scored": len(ok),
        "n_errored": len(outcomes) - len(ok),
        "tool_selection": {"hits": tool_hits, "n": exp_steps},
        "param_accuracy": {"hits": param_hits, "n": param_den},
        "redundant_calls": {"hits": redundant, "n": observed},
        "missing_calls": {"hits": missing, "n": exp_steps},
        "path_correct": {"hits": sum(1 for o in ok if o.path_ok), "n": len(ok)},
        "invalid_loop": {"hits": sum(1 for o in ok if o.invalid_loop), "n": len(ok)},
        "revision_translation": {
            "hits": sum(1 for o in trans if o.revision_translation_ok),
            "n": len(trans),
        },
        "revision_rollback": {
            "hits": sum(1 for o in roll if o.revision_rollback_ok),
            "n": len(roll),
        },
        "mean_path_similarity": (sum(o.path_similarity for o in ok) / len(ok)) if ok else 0.0,
    }


__all__ = [
    "FREE_TEXT_FIELDS",
    "StepScore",
    "TrajectoryOutcome",
    "aggregate",
    "lcs_length",
    "params_match",
    "path_is_correct",
    "path_similarity",
    "score_steps",
]
