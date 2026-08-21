"""实验一的批跑入口：`python -m backend.experiments.run_nl`。

**断点续跑**：键是 `(round_index, item_id, variant)`，中断后原样再跑一次
命令即可续上（与 M7 的 runner 同一套约定）。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from backend.core.config import Settings
from backend.core.db import get_session_factory
from backend.datasets.loader import load_eval_dataset
from backend.experiments.nl_eval import append_jsonl, iter_jsonl, run_item
from backend.harness import Harness
from backend.ingestion.loader import active_snapshot_id
from backend.planner.tools import planner_tool_handlers
from backend.routing.entities import directory_from_session

#: 数据集卡片里的 `context.eval_today` —— 相对周表述（「下周」）按它解析。
EVAL_TODAY = date(2026, 1, 5)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v6 §12.2 实验一：nl_360 批跑")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（估时用）")
    parser.add_argument(
        "--variant",
        default="main",
        choices=("main", "no_rules"),
        help="main=完整两级；no_rules=消融一，绕开一级规则全走 LLM",
    )
    parser.add_argument("--out", default="reports/m9b/exp1_nl360.jsonl")
    args = parser.parse_args(argv)

    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")
    out = Path(args.out)
    done = {(r["round_index"], r["item_id"], r.get("variant", "main")) for r in iter_jsonl(out)}
    if done:
        print(f"续跑：已有 {len(done)} 条观测")

    _, items = load_eval_dataset("nl_360", require_approved=True)
    records = [i.model_dump() for i in items]
    if args.limit:
        records = records[: args.limit]

    session = get_session_factory()()
    try:
        snapshot = active_snapshot_id(session)
        if not snapshot:
            print(
                "库里没有 ACTIVE 快照，先跑 python -m backend.ingestion.cli --baseline",
                file=sys.stderr,
            )
            return 2
        directory = directory_from_session(session, snapshot)

        def fresh_harness() -> Harness:
            """★ **每条样本一个新 Harness**，不能复用。

            `Harness` 持有的是**一次请求的预算账**（M4-A §8 第 4 条，
            上限 14 次 LLM 调用 —— `Z-34`）。整批共用一个的话，跑到第三四条
            预算就耗尽，此后每条都以 `degraded` 收场、`llm_calls=0`，
            而分类结果会**静默**退化成 `unknown` → 判成 `refuse`。
            本窗口第一次试跑正是这个形态：12 条里 8 条 degraded。

            接线同样要每次做一遍（图里由 `graph.py::_harness_for` 承担）。
            `prev_plan=None`：nl_360 全是首轮请求，没有「上一版方案」。
            """
            h = Harness(snapshot_id=snapshot, settings=cfg)
            h.registry.register_many(
                dict(
                    planner_tool_handlers(
                        directory=directory, today=EVAL_TODAY, prev_plan=None, user_role="director"
                    )
                )
            )
            return h

        total = len(records) * args.rounds
        n = 0
        started = time.monotonic()
        for rnd in range(1, args.rounds + 1):
            for item in records:
                n += 1
                key = (rnd, str(item["item_id"]), args.variant)
                if key in done:
                    continue
                obs = run_item(
                    item,
                    directory=directory,
                    today=EVAL_TODAY,
                    harness=fresh_harness(),
                    settings=cfg,
                    round_index=rnd,
                    use_rules=args.variant == "main",
                )
                payload = obs.to_json() | {"variant": args.variant}
                append_jsonl(out, payload)
                elapsed = time.monotonic() - started
                print(
                    f"[{n:4d}/{total}] r{rnd} {obs.item_id:12s} {obs.layer:20s} "
                    f"{obs.observed_intent:10s} conf={obs.confidence:.2f} "
                    f"src={obs.source:8s} llm={obs.llm_calls} {obs.wall_s:5.1f}s "
                    f"| 累计 {elapsed / 60:.1f}min" + (f" ⚠️{obs.error[:40]}" if obs.error else ""),
                    flush=True,
                )
    finally:
        session.rollback()
        session.close()
    print(f"\n观测已落盘：{out}")
    return 0


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    sys.exit(main())
