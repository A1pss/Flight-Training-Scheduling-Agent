"""实验三的批跑入口：`python -m backend.experiments.run_memory`。

四个 variant 各跑 320 条：`main` + 三条消融（§12.4）。

⚠️ **跑之前会把 20 周时间线写进真库，跑完逐行清理并复核计数** ——
与 `tests/datasets/run_probes.py` 同一套约定（M8 §3.9 的教训：往真库写过就要
还原，并且要验证还原到位）。中途 Ctrl-C 会跳过清理，那时手工跑一次
`--cleanup-only`。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from sqlalchemy import delete, func, select

from backend.core.config import Settings
from backend.core.db import session_scope
from backend.datasets.loader import load_eval_dataset
from backend.experiments.memory_eval import run_probe
from backend.harness import Harness
from backend.ingestion.loader import active_snapshot_id
from backend.models.memory import EpisodicMemory, ProceduralMemory
from backend.retrieval.corpus import build_corpus
from backend.retrieval.pipeline import RetrievalConfig
from backend.retrieval.rerank import build_reranker
from backend.retrieval.vector import build_vector_index
from backend.routing.entities import directory_from_session
from tests.datasets.memory_seed import seed_timeline

#: 提问时点的参照日（与 `run_probes.py` 一致）。
DEFAULT_TODAY = date(2026, 5, 18)

#: 四个 variant：主配置 + §12.4 的三条消融。
VARIANTS: dict[str, dict[str, bool]] = {
    "main": {},
    "no_structured": {"enable_structured": False},
    "no_rewrite": {"enable_rewrite": False},
    "no_time_filter": {"enable_time_filter": False},
}


def _cleanup() -> dict[str, int]:
    """把种下去的记忆逐行删掉，并把计数报出来（要验证还原到位）。"""
    with session_scope() as session:
        session.execute(delete(EpisodicMemory))
        session.execute(delete(ProceduralMemory))
        session.commit()
        return {
            "episodic_left": int(
                session.execute(select(func.count(EpisodicMemory.memory_id))).scalar() or 0
            ),
            "procedural_left": int(
                session.execute(select(func.count(ProceduralMemory.memory_id))).scalar() or 0
            ),
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v6 §12.4 实验三：memory_320 检索层批跑")
    parser.add_argument("--variants", default="main,no_structured,no_rewrite,no_time_filter")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default="reports/m9b/exp3_memory320.jsonl")
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args(argv)

    if args.cleanup_only:
        print("清理：", _cleanup())
        return 0

    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("", encoding="utf-8")

    _manifest, rows = load_eval_dataset("memory_320", require_approved=True)
    probes = [r.model_dump() for r in rows]
    if args.limit:
        probes = probes[: args.limit]

    with session_scope() as session:
        snapshot = active_snapshot_id(session)
        if not snapshot:
            print("库里没有 ACTIVE 快照", file=sys.stderr)
            return 2
        print("① 写入 20 周时间线 …", flush=True)
        seed_timeline(session)
        session.commit()
        n_epi = int(session.execute(select(func.count(EpisodicMemory.memory_id))).scalar() or 0)
        n_pro = int(session.execute(select(func.count(ProceduralMemory.memory_id))).scalar() or 0)
        print(f"   情景 {n_epi} 条 · 偏好 {n_pro} 条", flush=True)

        print("② 建语料 / 向量索引 / 精排器 …", flush=True)
        directory = directory_from_session(session, snapshot)
        corpus = build_corpus(session, snapshot)
        vector_index = build_vector_index(corpus.filter(), backend=cfg.VECTOR_BACKEND)
        reranker = build_reranker(cfg)
        print(f"   语料 {len(corpus.docs)} 篇 · 向量后端 {cfg.VECTOR_BACKEND}", flush=True)

        started = time.monotonic()
        wanted = [v.strip() for v in args.variants.split(",") if v.strip()]
        for variant in wanted:
            overrides = VARIANTS[variant]
            config = RetrievalConfig.from_settings(cfg, **overrides)
            print(f"\n③ variant={variant} overrides={overrides or '（无，主配置）'}", flush=True)
            for i, probe in enumerate(probes, start=1):
                # 每条探针一个新 Harness：预算是**每请求**一本账（Z-34，上限 14）。
                harness = Harness(snapshot_id=snapshot, settings=cfg)
                outcome = run_probe(
                    probe,
                    session=session,
                    snapshot_id=snapshot,
                    directory=directory,
                    today=DEFAULT_TODAY,
                    corpus=corpus,
                    vector_index=vector_index,
                    reranker=reranker,
                    harness=harness,
                    config=config,
                    variant=variant,
                )
                with out.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(outcome.to_json(), ensure_ascii=False, sort_keys=True) + "\n"
                    )
                if i % 20 == 0 or i == len(probes):
                    print(
                        f"   [{i:3d}/{len(probes)}] {outcome.item_id} "
                        f"hit@5={'Y' if outcome.hit_at_5 else 'n'} "
                        f"| 累计 {(time.monotonic() - started) / 60:.1f}min",
                        flush=True,
                    )

    print("\n④ 清理：", _cleanup())
    print(f"观测已落盘：{out}")
    return 0


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    sys.exit(main())
