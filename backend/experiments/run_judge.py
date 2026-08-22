"""§12.4.1 的 judge 批跑：先验证 judge，达标才采信。

```bash
# ① 一致性验证（judge_calib_50，204 个判定）
python -m backend.experiments.run_judge --stage calib

# ② 达标之后才跑全量 320（未达标时本步骤会拒绝执行）
python -m backend.experiments.run_judge --stage full
```

`--stage full` **在一致性未达标时直接拒绝跑**，这是刻意的：跑出来的数按
§12.4.1 本来就不许报，跑它只是浪费一小时 GPU 并制造一个「反正数在这儿了，
要不要报一下」的诱惑。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from backend.core.config import Settings
from backend.core.db import session_scope
from backend.experiments.judge import (
    JUDGE_MODEL,
    build_faithfulness_request,
    build_usage_request,
    faithfulness,
    parse_used,
    parse_verdict,
)
from backend.experiments.stats import (
    agreement_rate,
    class_recall,
    cohen_kappa,
    kappa_bootstrap_ci,
)
from backend.llm.ollama import OllamaProvider

CALIB = Path("datasets/judge_calib_50/v1/items.jsonl")
ANSWERS = Path("datasets/judge_calib_50/v1/answers_v1.jsonl")

#: §12.4.1 的采信门槛，**不许放宽**。
AGREEMENT_FLOOR = 0.85
KAPPA_FLOOR = 0.70


def _provider() -> OllamaProvider:
    """指向 32B 的 Provider。温度与 seed 由请求侧固定（judge.py）。"""
    return OllamaProvider(Settings(_env_file=None, LLM_PROVIDER="ollama", LLM_MODEL=JUDGE_MODEL))


def run_calibration(out: Path, provider: Any | None = None) -> dict[str, Any]:
    """在 50 条人工标注上跑 judge，算一致率 / Kappa / 少数类召回。"""
    items = [
        json.loads(line) for line in CALIB.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    # `provider` 可注入是为了让这段逻辑能被单测覆盖 —— 一致率与 Kappa 的算法
    # 不该只能在一块 24G 显卡前面才验证得了。
    provider = provider or _provider()
    rows: list[dict[str, Any]] = []
    started = time.monotonic()

    human_v: list[str] = []
    judge_v: list[str] = []
    human_u: list[str] = []
    judge_u: list[str] = []

    total_claims = sum(1 for i in items for c in i["claims"] if c.get("is_assertive"))
    done = 0
    for item in items:
        contexts = item.get("retrieved_contexts") or []
        for claim in item["claims"]:
            # 非陈述片段（「请问是哪一个？」这类）**不进一致率的分母** ——
            # 把它们混进去会让 judge 与人在一堆无意义的格子上「达成一致」，
            # 正是 §12.4.1 说的虚高。
            if not claim.get("is_assertive"):
                continue
            done += 1
            request = build_faithfulness_request(str(claim["text"]), contexts)
            verdict = parse_verdict(provider.chat(request).text)
            gold = str(claim.get("verdict") or "")
            rows.append(
                {
                    "kind": "claim",
                    "item_id": item["item_id"],
                    "claim_id": claim["claim_id"],
                    "human": gold,
                    "judge": verdict,
                    "text": claim["text"],
                }
            )
            if verdict and gold:
                human_v.append(gold)
                judge_v.append(verdict)
            if done % 20 == 0:
                print(
                    f"  断言 {done}/{total_claims} … {(time.monotonic() - started) / 60:.1f}min",
                    flush=True,
                )

        for usage in item.get("context_usage") or []:
            request = build_usage_request(str(item["answer"]), str(usage.get("snippet", "")))
            used = parse_used(provider.chat(request).text)
            gold_used = usage.get("used")
            rows.append(
                {
                    "kind": "usage",
                    "item_id": item["item_id"],
                    "doc_id": usage.get("doc_id"),
                    "human": gold_used,
                    "judge": used,
                }
            )
            if used is not None and gold_used is not None:
                human_u.append("Y" if gold_used else "N")
                judge_u.append("Y" if used else "N")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )

    # judge 一条合法判定都没给出来（受约束解码失效、schema 不被支持…）。
    # 这时**不能**去算一致率 —— 分母为 0。如实记成「未通过验证」并说明原因，
    # 而不是让 `wilson_interval` 抛到调用栈顶上去。
    if not human_v:
        return {
            "judge_model": JUDGE_MODEL,
            "n_claims_scored": 0,
            "n_claims_unparsed": sum(1 for r in rows if r["kind"] == "claim" and not r["judge"]),
            "agreement": None,
            "kappa": None,
            "kappa_ci": None,
            "minority_recall": {},
            "usage": {"n": 0, "agreement": None, "kappa": None},
            "thresholds": {"agreement": AGREEMENT_FLOOR, "kappa": KAPPA_FLOOR},
            "passed": False,
            "failure_reason": "judge 没有产出任何可解析的判定，一致率无从计算",
            "wall_min": (time.monotonic() - started) / 60,
        }

    agree = agreement_rate(human_v, judge_v)
    kappa = cohen_kappa(human_v, judge_v)
    kappa_ci = kappa_bootstrap_ci(human_v, judge_v)
    passed = agree.point >= AGREEMENT_FLOOR and kappa >= KAPPA_FLOOR

    summary: dict[str, Any] = {
        "judge_model": JUDGE_MODEL,
        "n_claims_scored": len(human_v),
        "n_claims_unparsed": sum(1 for r in rows if r["kind"] == "claim" and not r["judge"]),
        "agreement": agree.__dict__,
        "kappa": kappa,
        "kappa_ci": kappa_ci.__dict__,
        # M9-A §3.9.4 点名要一起报的第三个数：少数类各自的召回率。
        # 只有它说得清 judge 是「整体不准」还是「只是抓不住少数类」。
        "minority_recall": {
            label: class_recall(human_v, judge_v, label).__dict__
            for label in ("SUPPORTED", "PARTIAL", "NOT_SUPPORTED")
        },
        "usage": {
            "n": len(human_u),
            "agreement": agreement_rate(human_u, judge_u).__dict__ if human_u else None,
            "kappa": cohen_kappa(human_u, judge_u) if human_u else None,
        },
        "thresholds": {"agreement": AGREEMENT_FLOOR, "kappa": KAPPA_FLOOR},
        "passed": passed,
        "wall_min": (time.monotonic() - started) / 60,
    }
    return summary


def run_full(out: Path, provider: Any | None = None) -> dict[str, Any]:
    """全量 320 条的生成层判定。**上下文原文从库里还原**，不重跑 LLM。"""
    from tests.datasets.calib_contexts import build_text_index, snippet

    answers = [
        json.loads(line)
        for line in ANSWERS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with session_scope() as session:
        index = build_text_index(session)

    provider = provider or _provider()
    rows: list[dict[str, Any]] = []
    verdicts: list[str] = []
    started = time.monotonic()

    for n, ans in enumerate(answers, start=1):
        contexts = [
            {"snippet": snippet(index[d])} for d in ans.get("retrieved_doc_ids", []) if d in index
        ]
        for i, claim in enumerate(ans.get("claims") or [], start=1):
            text = str(claim.get("claim", "")).strip()
            if not text:
                continue
            verdict = parse_verdict(provider.chat(build_faithfulness_request(text, contexts)).text)
            verdicts.append(verdict)
            rows.append({"item_id": ans["item_id"], "claim_no": i, "judge": verdict, "text": text})
        if n % 25 == 0:
            print(
                f"  探针 {n}/{len(answers)} … {(time.monotonic() - started) / 60:.1f}min",
                flush=True,
            )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    hits, total = faithfulness(verdicts)
    return {
        "judge_model": JUDGE_MODEL,
        "n_claims": len(verdicts),
        "n_unparsed": sum(1 for v in verdicts if not v),
        "faithfulness": {"hits": hits, "n": total, "rate": hits / total if total else 0.0},
        "wall_min": (time.monotonic() - started) / 60,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="§12.4.1 离线 judge")
    parser.add_argument("--stage", choices=("calib", "full"), default="calib")
    parser.add_argument("--out-dir", default="reports/m9b")
    parser.add_argument(
        "--calib-summary",
        default="reports/m9b/exp3_judge_calib_summary.json",
        help="full 阶段据此确认 judge 已通过验证",
    )
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)

    if args.stage == "calib":
        summary = run_calibration(out_dir / "exp3_judge_calib.jsonl")
        Path(args.calib_summary).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(
            "\n✅ judge 通过验证，可以跑全量"
            if summary["passed"]
            else "\n❌ judge 未通过验证 —— 按 §12.4.1，本轮 Faithfulness 与上下文利用率"
            "**不报数**，列为改进项。不许换模型、不许放宽门槛。"
        )
        return 0

    calib_path = Path(args.calib_summary)
    if not calib_path.exists():
        print("先跑 --stage calib", file=sys.stderr)
        return 2
    calib = json.loads(calib_path.read_text(encoding="utf-8"))
    if not calib.get("passed"):
        print(
            "judge 未通过一致性验证，拒绝跑全量 —— 跑出来的数按 §12.4.1 本来就不许报。",
            file=sys.stderr,
        )
        return 3
    summary = run_full(out_dir / "exp3_judge_full.jsonl")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    sys.exit(main())
