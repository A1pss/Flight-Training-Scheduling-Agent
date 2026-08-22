"""§12.4.1 的离线 judge：32B 判定 Faithfulness 与上下文利用率。

## 这个 judge 必须先被验证，然后才能被采信

v6 §12.4.1 的流程是死的，不许绕：

1. 逐条断言判 `SUPPORTED` / `PARTIAL` / `NOT_SUPPORTED`，**不整段打分**；
2. 用 `judge_calib_50`（业务方全程人工标注）算 **judge 与人工的一致率 + Kappa**；
3. **采信门槛：一致率 ≥85% 且 Kappa ≥0.70**。达标才采信它对全量 320 条的判定；
   未达标就写「judge 未通过验证，本轮不报数」并列改进项 ——
   **不许换 judge 模型、不许放宽门槛**（`CLAUDE.md` 反模式清单点名的那一条）。

## 报数时三个数一起给

M9-A §3.9.4 已经把这笔账算过：标签边际 88.4% 压在 `SUPPORTED`，
偶然一致率 `p_e ≈ 0.792`，于是

| §12.4.1 的门槛 | 实际等价于 |
|---|---|
| 一致率 ≥85% | Kappa ≈ 0.28 |
| **Kappa ≥0.70** ← 真正起作用的 | 一致率 **≥93.8%**（155 条里最多错 9 条） |

所以未过门槛**可能是这条算术的结果而不是 judge 太差**。报数必须同时给出
一致率、Kappa、以及**少数类各自的召回率** —— 第三个数才说得清 judge 是
「整体不准」还是「只是抓不住少数类」。

## 双重角色声明

32B 同时是 §15.2 的硬样本教师和本节的 judge。两者作用面不同（前者产出工具
调用样本、后者判定解释文本的忠实度），且**本 judge 不参与 §15.4 微调准入门禁
的任何一项**，故不构成「用教师评自己教出来的学生」的循环。验收报告须主动声明
（§12.7 必述项 4）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from backend.llm.types import LLMRequest

#: §12.4.1 指定的 judge 模型。
JUDGE_MODEL: Final[str] = "qwen2.5:32b-instruct-q4_K_M"

#: 三分类，只有 `SUPPORTED` 计入 Faithfulness 的分子。
VERDICTS: Final[tuple[str, ...]] = ("SUPPORTED", "PARTIAL", "NOT_SUPPORTED")

VERDICT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": list(VERDICTS)}},
    "required": ["verdict"],
}

USED_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"used": {"type": "boolean"}},
    "required": ["used"],
}

_FAITHFULNESS_SYSTEM = """你是一个严格的事实核查员。给你一段「召回内容」和一条「断言」，\
判断这条断言是否被召回内容支撑。

判定标准：
- SUPPORTED：断言的全部内容都能在召回内容里找到直接依据。
- PARTIAL：断言的一部分有依据，另一部分没有；或者断言比召回内容多走了一步推论。
- NOT_SUPPORTED：召回内容里没有依据，或断言与召回内容矛盾，或断言把召回内容的\
强度拔高了（例如召回说「周三排了 5 个架次，太密」，断言说成「不要在每周三安排飞行任务」）。

只依据给出的召回内容判断，不要用你自己的知识补全。只输出 JSON。"""

_USAGE_SYSTEM = """你是一个严格的核查员。给你一段「回答」和一条「召回条目」，\
判断这个回答有没有**实际使用**这条召回条目的信息。

「使用」指回答里出现了来自该条目的具体内容（数值、日期、编号、结论）。\
仅仅主题相关但没有用上其中任何具体信息，算没有使用。只输出 JSON。"""


def _contexts_block(contexts: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for i, ctx in enumerate(contexts, start=1):
        snippet = str(ctx.get("snippet", "")).strip()
        if snippet:
            lines.append(f"[{i}] {snippet}")
    return "\n".join(lines) if lines else "（没有召回到任何内容）"


def build_faithfulness_request(claim: str, contexts: Sequence[Mapping[str, Any]]) -> LLMRequest:
    """一条断言的判定请求。**温度 0、受约束解码到三分类**（§12.4.1）。"""
    user = f"召回内容：\n{_contexts_block(contexts)}\n\n断言：\n{claim.strip()}"
    return LLMRequest(
        messages=[
            {"role": "system", "content": _FAITHFULNESS_SYSTEM},
            {"role": "user", "content": user},
        ],
        format_schema=VERDICT_SCHEMA,
        temperature=0.0,
    )


def build_usage_request(answer: str, snippet: str) -> LLMRequest:
    """一条召回条目的「用上了没有」判定请求。"""
    user = f"回答：\n{answer.strip()}\n\n召回条目：\n{snippet.strip()}"
    return LLMRequest(
        messages=[
            {"role": "system", "content": _USAGE_SYSTEM},
            {"role": "user", "content": user},
        ],
        format_schema=USED_SCHEMA,
        temperature=0.0,
    )


def parse_verdict(text: str) -> str:
    """解析判定。**越界一律落到 `NOT_SUPPORTED` 之外的第四种：空串**。

    返回空串表示「judge 没给出合法判定」——调用方据此把该条**排除出分母**，
    而不是当成某一类。把解析失败记成 `NOT_SUPPORTED` 会凭空制造分歧，
    算出来的一致率就不再是 judge 与人的一致率了。
    """
    import json

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ""
    verdict = str(payload.get("verdict", ""))
    return verdict if verdict in VERDICTS else ""


def parse_used(text: str) -> bool | None:
    """解析「用上了没有」。解析不了返回 `None`（排除出分母，同上）。"""
    import json

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    value = payload.get("used")
    return bool(value) if isinstance(value, bool) else None


def faithfulness(verdicts: Sequence[str]) -> tuple[int, int]:
    """Faithfulness 的分子分母：**只有 `SUPPORTED` 计入分子**（§12.4.1）。

    解析失败的空串不进分母。
    """
    scored = [v for v in verdicts if v]
    return sum(1 for v in scored if v == "SUPPORTED"), len(scored)


def context_utilisation(used_flags: Sequence[bool | None]) -> tuple[int, int]:
    """上下文利用率 = **召回了正确内容却没用上的比例**（§12.4，目标 ≤18%）。

    ⚠️ 注意方向：分子是**没用上**的那些。名字叫「利用率」但目标是「≤18%」，
    这一点 v6 的表述本身就容易读反 —— 定义栏写的是「召回了正确内容却没用上的
    比例」，以定义为准。
    """
    scored = [u for u in used_flags if u is not None]
    return sum(1 for u in scored if not u), len(scored)


__all__ = [
    "JUDGE_MODEL",
    "USED_SCHEMA",
    "VERDICTS",
    "VERDICT_SCHEMA",
    "build_faithfulness_request",
    "build_usage_request",
    "context_utilisation",
    "faithfulness",
    "parse_used",
    "parse_verdict",
]
