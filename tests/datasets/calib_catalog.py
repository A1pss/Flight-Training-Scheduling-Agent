"""`judge_calib_50` 的抽样与断言分解（v6 §12.4.1）。

## 本集与其余八集的根本区别

其余八集是「Claude Code 初稿 → Alps 复核」；**这一集必须由业务方全程人工标注**。
§12.4.1 写明了理由：它是给 judge 当基准真值的，**用 LLM 生成初稿会把要验证的
偏差直接引进基准里**。所以 Claude Code 在这里只做两件事：

1. **抽样**（确定性，seed 固定）；
2. **断言分解**（把回答切成一条条可判定的断言）。

标签栏（`verdict` / `context_used`）交付时**必须全空** —— 由 `JudgeCalibItem`
在加载期强制，不靠自觉。

## 分层不能用 judge 自己的标签

否则就是循环。用三条**确定性代理信号**：

| 信号 | 判据 | 与「无支撑」的关系 |
|---|---|---|
| `recall_miss` | gold 与召回**归一化后**无交集 | 召回都没中，回答多半没出处 |
| `entity_not_retrieved` | 回答里提到的实体编号不在召回集里 | 典型的凭参数记忆作答 |
| `low_supported_ratio` | M5 逐句核验通过率 < 0.8 | 只作信号**不作标签**（口径不同） |
| `degraded` | 生成降级或退回事实直出 | 那一轮的回答形态本就异常 |

★ **`pg:` 与 `ent:` 必须先归一**（`canonical_doc_id`）：路 A 发的是
`pg:persons:P04`，语料发的是 `ent:person:P04`，直接按字符串比会把最强的那一路
命中全判成未召回 —— 高风险层会被灌满假阳性。

## 负例不足时的处置

高风险层不足 25 条时，用**受控故障注入**补足：把回答里的关键事实改错
（到期日改成 02-07、JL-8 说成 JL-9、加一句语料里没有的断言）。这类条目标
`is_synthetic_negative=True`，**报告里真实样本与含注入样本的一致率分开报**。
业务方 2026-08-19 确认了这一条。
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any, Final

from backend.datasets.schemas import canonical_doc_id

SAMPLE_SIZE: Final[int] = 50
HIGH_RISK_TARGET: Final[int] = 25
SEED: Final[int] = 42
#: 常规层按三类记忆的条数比例分配（120:120:80 → 10:9:6）
REGULAR_QUOTA: Final[dict[str, int]] = {"semantic": 10, "episodic": 9, "procedural": 6}

_ENTITY_RE: Final[re.Pattern[str]] = re.compile(r"P\d+|AC\d+|mission[A-Z]-\d+")
_SENTENCE_SPLIT: Final[re.Pattern[str]] = re.compile(r"[。！？；\n]+")

#: 受控扰动：(名字, 查找, 替换)。**只改事实，不改语气** —— 要造的是
#: 「读起来一样可信但事实错了」的负例，那才是 Faithfulness 要抓的东西。
PERTURBATIONS: Final[tuple[tuple[str, str, str], ...]] = (
    ("到期日改错（01-07 → 02-07）", "2026-01-07", "2026-02-07"),
    ("机型说反（JL-8 → JL-9）", "JL-8", "JL-9"),
    ("机型说反（JL-9 → JL-8）", "JL-9", "JL-8"),
    ("结论反转（不能 → 可以）", "不能", "可以"),
    ("容量说错（容量 1 → 容量 3）", "容量为 1", "容量为 3"),
)

#: 追加一句语料里没有的断言 —— 「多说一句」是最常见的无支撑形态
FABRICATED_SENTENCE: Final[str] = "此外，该记录已于上周经训练主任复核并确认无误。"


def load_answers(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def risk_signals(record: dict[str, Any]) -> list[str]:
    """三条确定性代理信号。**没有一条是 judge。**"""
    signals: list[str] = []
    gold = {canonical_doc_id(d) for d in record["expected_doc_ids"]}
    got = {canonical_doc_id(d) for d in record["retrieved_doc_ids"]}
    if gold and not (gold & got):
        signals.append("recall_miss")

    mentioned = set(_ENTITY_RE.findall(record["answer"]))
    retrieved_entities = {
        part for doc in got for part in doc.split(":")[-1:] if _ENTITY_RE.fullmatch(part)
    }
    if mentioned - retrieved_entities:
        signals.append("entity_not_retrieved")

    ratio = record.get("supported_ratio")
    if isinstance(ratio, int | float) and ratio < 0.8:
        signals.append("low_supported_ratio")
    if record.get("degraded") or record.get("fallback"):
        signals.append("degraded")
    return signals


def decompose(record: dict[str, Any]) -> list[dict[str, Any]]:
    """断言分解。

    优先用 M5 逐句核验器已经切好的断言（那是确定性代码的产物）；核验器没切
    （比如回答退回了事实直出）时按句号切分兜底。**不做语义合并、不做改写** ——
    分解要可复现，任何「归纳一下」的动作都会引入判断。
    """
    claims = record.get("claims") or []
    if claims:
        return [
            {
                "claim_id": f"c{index}",
                "text": str(item["claim"]),
                "verdict": None,
                "context_used": None,
                "verifier_supported": bool(item.get("verifier_supported")),
            }
            for index, item in enumerate(claims, start=1)
        ]
    pieces = [p.strip() for p in _SENTENCE_SPLIT.split(record["answer"]) if p.strip()]
    return [
        {
            "claim_id": f"c{index}",
            "text": piece,
            "verdict": None,
            "context_used": None,
            "verifier_supported": None,
        }
        for index, piece in enumerate(pieces, start=1)
    ] or [
        {
            "claim_id": "c1",
            "text": record["answer"].strip() or "（空回答）",
            "verdict": None,
            "context_used": None,
            "verifier_supported": None,
        }
    ]


def _perturb(record: dict[str, Any]) -> tuple[str, str]:
    """造一条合成负例。返回 `(扰动名, 改过的回答)`。"""
    answer = record["answer"]
    for name, needle, replacement in PERTURBATIONS:
        if needle in answer:
            return name, answer.replace(needle, replacement, 1)
    return "追加一句语料中没有的断言", answer.rstrip() + FABRICATED_SENTENCE


def build(answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """抽 50 条：高风险 25 + 常规 25（不足时用受控注入补足高风险层）。"""
    rng = random.Random(SEED)
    scored = [(record, risk_signals(record)) for record in answers]
    high_pool = sorted((r for r, s in scored if s), key=lambda r: str(r["item_id"]))
    low_pool = sorted((r for r, s in scored if not s), key=lambda r: str(r["item_id"]))
    signals_by_id = {r["item_id"]: s for r, s in scored}

    picked_high = (
        high_pool if len(high_pool) <= HIGH_RISK_TARGET else rng.sample(high_pool, HIGH_RISK_TARGET)
    )
    rows: list[dict[str, Any]] = []

    def emit(
        record: dict[str, Any],
        stratum: str,
        *,
        synthetic: bool = False,
    ) -> None:
        answer = record["answer"]
        perturbation: str | None = None
        claims = decompose(record)
        if synthetic:
            perturbation, answer = _perturb(record)
            claims = decompose({**record, "answer": answer, "claims": []})
        rows.append(
            {
                "item_id": f"JCAL-{len(rows) + 1:03d}",
                "probe_id": record["item_id"],
                "stratum": stratum,
                "memory_type": record["memory_type"],
                "query": record["query"],
                "answer": answer,
                "retrieved_doc_ids": list(record["retrieved_doc_ids"]),
                "expected_doc_ids": list(record["expected_doc_ids"]),
                "risk_signals": list(signals_by_id.get(record["item_id"], [])),
                "is_synthetic_negative": synthetic,
                "perturbation": perturbation,
                "claims": claims,
                "rationale": (
                    (
                        f"高风险层：命中信号 {signals_by_id.get(record['item_id'], []) or '（合成负例）'}。"
                        if stratum == "high_risk"
                        else f"常规层：{record['memory_type']} 类，三条代理信号一个都没命中。"
                    )
                    + (
                        "★ 本条是**受控故障注入**造出来的负例，报一致率时要与真实样本分开。"
                        if synthetic
                        else ""
                    )
                    + "★ 标签栏（verdict / context_used）交付时为空，由业务方人工标注（§12.4.1）。"
                ),
            }
        )

    for record in picked_high:
        emit(record, "high_risk")

    # 高风险不足 → 受控注入补足（业务方 2026-08-19 确认）
    shortfall = HIGH_RISK_TARGET - len(rows)
    donors = [r for r in low_pool if r["answer"].strip()]
    for record in rng.sample(donors, min(shortfall, len(donors))):
        emit(record, "high_risk", synthetic=True)

    # 常规层：按 10/9/6 分层随机
    used = {r["probe_id"] for r in rows}
    for memory_type, quota in REGULAR_QUOTA.items():
        pool = [r for r in low_pool if r["memory_type"] == memory_type and r["item_id"] not in used]
        for record in rng.sample(pool, min(quota, len(pool))):
            emit(record, "regular")
    return rows
