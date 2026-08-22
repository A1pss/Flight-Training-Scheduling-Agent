"""§12.3 基线对比的批跑：`python -m backend.experiments.run_baseline`。

抽样：从 200 场景里**可行的那些**按类别分层抽（固定种子）。只抽可行场景 ——
「硬约束满足率」在不可行场景上没有定义（正确输出是判不可行，不是排出方案）。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from backend.core.config import Settings
from backend.core.db import session_scope
from backend.experiments.baseline_llm import (
    CONSTRAINTS,
    MAX_ROUNDS,
    OUTPUT_SPEC,
    BaselineOutcome,
    extract_json,
    grade,
    render_world,
)
from backend.llm.provider import build_provider
from backend.llm.types import LLMRequest
from backend.validator.context import load_context

SOLVED = ("OPTIMAL", "FEASIBLE")
SAMPLE_SEED = 20260821


def _sample(results: Sequence[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """按类别分层抽 n 个可行场景，固定种子可复现。"""
    feasible = [r for r in results if r["status"] in SOLVED and not r.get("error")]
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in feasible:
        by_cat[r["category"]].append(r)
    # 固定种子的可复现抽样，不是密码学用途 —— 与 tests/scenarios/report.py
    # 的 SAMPLE_SEED 同一套做法：业务方按同一份清单复核即可复现。
    rng = random.Random(SAMPLE_SEED)  # noqa: S311
    per = max(1, n // max(len(by_cat), 1))
    out: list[dict[str, Any]] = []
    for cat in sorted(by_cat):
        rows = sorted(by_cat[cat], key=lambda r: str(r["scenario_id"]))
        out.extend(rows if len(rows) <= per else rng.sample(rows, per))
    return sorted(out, key=lambda r: str(r["scenario_id"]))[:n]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="§12.3 基线对比：LLM 直接排班")
    parser.add_argument("--results", default="reports/m9b/exp2_200scenarios.json")
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--configs", default="llm_only,llm_retry")
    parser.add_argument("--out", default="reports/m9b/exp2_baseline_llm.jsonl")
    parser.add_argument("--timeout", type=float, default=900.0, help="单次生成超时（秒）")
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.results).read_text(encoding="utf-8"))
    cases = _sample(payload["results"], args.n)
    plans = payload.get("plans") or {}
    template = next(
        ({k: v for k, v in p.items() if k != "sorties"} for p in plans.values() if p), None
    )
    if template is None:
        print("结果文件里没有任何方案，取不到计划元信息模板", file=sys.stderr)
        return 2

    # ★ 整周排班是一次**很长**的生成（14 个架次 × 12 个字段），默认 120s 会超时，
    #   而超时会被误记成「模型产不出合法 JSON」——那是两件完全不同的事。
    #   冒烟时实测：BASE-01 在 120s 下 100% 超时。
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama", LLM_TIMEOUT_S=args.timeout)
    provider = build_provider(cfg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("", encoding="utf-8")
    wanted = [c.strip() for c in args.configs.split(",") if c.strip()]
    started = time.monotonic()

    with session_scope() as session:
        ctx = load_context(
            session,
            snapshot_id=str(template["snapshot_id"]),
            week_start=__import__("datetime").date.fromisoformat(str(template["week_start"])),
        )
        world = render_world(ctx)

        for i, case in enumerate(cases, start=1):
            for config in wanted:
                rounds = MAX_ROUNDS if config == "llm_retry" else 1
                outcome = BaselineOutcome(scenario_id=str(case["scenario_id"]), config=config)
                messages = [
                    {
                        "role": "system",
                        "content": "你是飞行训练排班员。按给定的世界与硬约束排出整周训练计划。",
                    },
                    {
                        "role": "user",
                        "content": f"{world}\n\n{CONSTRAINTS}\n\n{OUTPUT_SPEC}",
                    },
                ]
                for attempt in range(1, rounds + 1):
                    outcome.rounds_used = attempt
                    try:
                        text = provider.chat(LLMRequest(messages=messages)).text
                    except Exception as exc:
                        outcome.error = f"{exc.__class__.__name__}: {exc}"
                        break
                    parsed = extract_json(text)
                    if parsed is None:
                        outcome.parsed = False
                        feedback = "输出不是合法 JSON。只输出 JSON 对象，不要任何解释文字。"
                    else:
                        outcome.parsed = True
                        ok, errors, hard, n_sorties = grade(parsed, ctx, template)
                        outcome.schema_errors = errors
                        outcome.hard_violations = hard
                        outcome.num_sorties = n_sorties
                        if ok and not hard:
                            outcome.satisfied = True
                            break
                        feedback = (
                            f"格式错误：{errors[:5]}" if not ok else f"违反硬约束：{hard}"
                        ) + "。请修正后重新输出完整 JSON。"
                    if attempt < rounds:
                        messages = [
                            *messages,
                            {"role": "assistant", "content": text[:2000]},
                            {"role": "user", "content": feedback},
                        ]
                with out.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(outcome.to_json(), ensure_ascii=False, sort_keys=True) + "\n"
                    )
                print(
                    f"[{i:2d}/{len(cases)}] {outcome.scenario_id:10s} {config:10s} "
                    f"轮次={outcome.rounds_used} "
                    f"{'超时/报错' if outcome.error else ('解析=Y' if outcome.parsed else '解析=N')} "
                    f"架次={outcome.num_sorties:2d} 硬违规={outcome.hard_violations} "
                    f"满足={'✅' if outcome.satisfied else '❌'} "
                    f"| {(time.monotonic() - started) / 60:.1f}min",
                    flush=True,
                )
    print(f"\n观测已落盘：{out}")
    return 0


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    sys.exit(main())
