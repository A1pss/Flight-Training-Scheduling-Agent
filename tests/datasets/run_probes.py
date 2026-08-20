"""跑 `memory_320` 的 320 条探针，把「召回上下文 + 回答」**冻结**下来。

```bash
PYTHONPATH=. VECTOR_BACKEND=chroma EMBED_PROVIDER=bge LLM_PROVIDER=ollama \\
  python tests/datasets/run_probes.py --limit 5 --out /tmp/probe_smoke.jsonl
```

## 为什么要冻结

`judge_calib_50` 的用途是**给 judge 当基准真值**：人工标注与 judge 判定必须面对
**同一批文本**，否则算出来的一致率没有意义。而 14B 的回答哪怕温度 0 也会随
上下文（语料、检索结果、提示词版本）变化 —— 所以这一批回答要落盘、要进版本控制、
要带上当时的环境指纹。

## 情景与程序记忆要先落库

320 条里有 200 条问的是情景/程序记忆。库里没有那批记忆，检索必然召回不到，
跑出来的回答一律是「没有记录」——那种回答拿去标注是浪费人力。所以本脚本先跑
`memory_seed.seed_timeline()`（122 条情景 + 蒸馏出的 25 条偏好），跑完**逐行清理**
并复核计数（M8 §3.9 那条教训的同款处置：往真库写过就要还原，并且要验证还原到位）。

## ⚠️ 每条探针一个会话 —— 这不是洁癖

W11 首次试跑时发现：**模型会编表名**（`instrument_ratings` / `instrument_ranks`
都不存在），而一条失败的 SQL 会让 PostgreSQL **把整个事务置为 aborted**，
之后同一会话里的每一次查询都直接失败：

```
psycopg.errors.InFailedSqlTransaction: current transaction is aborted,
commands ignored until end of transaction block
```

用一个长会话跑 320 条的话，**第一条编错表名的探针会毒掉它后面的全部** ——
而那些探针的回答会变成一片「查不到」，拿去标注纯属浪费。
所以每条探针独立开会话；一条坏 SQL 最多毁它自己那一条。

> 这条也值得记进收工报告：生产路径上 `build_deps` 同样是**整个图共用一个会话**
> （`runtime.py` 的 `_shared`）。一次工具调用编错表名，后面的 `commit_plan`
> 会跟着失败。W13 之前值得看一眼要不要给 `sql_query` 套个 SAVEPOINT。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from backend.agents.knowledge import ask
from backend.core.config import get_settings
from backend.core.db import session_scope
from backend.datasets.loader import load_eval_dataset
from backend.datasets.schemas import MemoryItem
from backend.harness import Harness
from backend.models.memory import EpisodicMemory, ProceduralMemory
from backend.retrieval.corpus import build_corpus
from backend.retrieval.rerank import build_reranker
from backend.retrieval.vector import build_vector_index
from backend.routing.entities import directory_from_session
from tests.datasets.memory_seed import seed_timeline
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot

#: 提问时点的参照日 —— 探针自带 `as_of`，这里只作兜底
DEFAULT_TODAY = date(2026, 5, 18)


def run(limit: int | None, out_path: Path) -> int:
    cfg = get_settings()
    probes: list[MemoryItem] = []
    _manifest, rows = load_eval_dataset("memory_320", require_approved=True)
    probes = [r for r in rows if isinstance(r, MemoryItem)]
    if limit is not None:
        probes = probes[:limit]

    fingerprint = {
        "llm_provider": cfg.LLM_PROVIDER,
        "llm_model": cfg.LLM_MODEL,
        "embed_provider": cfg.EMBED_PROVIDER,
        "vector_backend": cfg.VECTOR_BACKEND,
        "rerank_provider": cfg.RERANK_PROVIDER,
        "probe_count": len(probes),
    }
    print(f"环境指纹：{json.dumps(fingerprint, ensure_ascii=False)}", flush=True)

    records: list[dict[str, Any]] = []
    started = time.monotonic()

    # ① 播种（提交），并记下播种前就存在的偏好行 —— 清理时只删我们自己写的
    with session_scope() as setup:
        snapshot_id = ensure_baseline_snapshot(setup)
        pre_existing = set(setup.scalars(select(ProceduralMemory.memory_id)))
        setup.execute(delete(EpisodicMemory).where(EpisodicMemory.session_id.startswith("m9a-")))
        setup.flush()
        seed_timeline(setup)
        corpus = build_corpus(setup, snapshot_id)
        setup.commit()
    print(f"时间线已落库；语料 {len(corpus.docs)} 篇", flush=True)

    index = build_vector_index(corpus.filter(), backend=cfg.VECTOR_BACKEND)
    # 重排器建一次就够：不复用的话每条探针都要重新加载 bge-reranker（2.2 GB）
    reranker = build_reranker(cfg)
    print(
        f"向量后端 {cfg.VECTOR_BACKEND} · 嵌入 {cfg.EMBED_PROVIDER} · 重排 {cfg.RERANK_PROVIDER}",
        flush=True,
    )

    try:
        for number, probe in enumerate(probes, start=1):
            as_of = date.fromisoformat(probe.as_of)
            began = time.monotonic()
            # ★ 一条探针一个会话：模型编错表名会让整个事务作废，隔离它
            with session_scope() as session:
                # ★ **每条探针一个 Harness**：预算是**请求级**的（§7.6），
                # 复用同一个会让第 11 次 LLM 调用之后的探针全部降级 ——
                # 那不是模型的问题，是量具装错了。
                harness = Harness(snapshot_id=snapshot_id, settings=cfg)
                outcome = ask(
                    probe.query,
                    session=session,
                    snapshot_id=snapshot_id,
                    directory=directory_from_session(session, snapshot_id),
                    today=DEFAULT_TODAY,
                    as_of=as_of,
                    harness=harness,
                    corpus=corpus,
                    vector_index=index,
                    reranker=reranker,
                    settings=cfg,
                )
                elapsed = time.monotonic() - began
                records.append(
                    {
                        "item_id": probe.item_id,
                        "memory_type": probe.memory_type,
                        "probe_kind": probe.probe_kind,
                        "query": probe.query,
                        "as_of": probe.as_of,
                        "expected_doc_ids": list(probe.expected_doc_ids),
                        "retrieved_doc_ids": [d.doc_id for d in outcome.retrieval.contexts],
                        "answer": outcome.text,
                        "steps": outcome.steps,
                        "llm_calls": outcome.llm_calls,
                        "autonomous": outcome.autonomous,
                        "structured_answers": len(outcome.retrieval.answers),
                        "supported_ratio": outcome.answer.report.supported_ratio,
                        # ★ 逐句核验的结果直接就是**断言分解的初稿**（M5 的确定性核验器
                        # 产出，不是 LLM 判的）。judge_calib_50 拿它当骨架，
                        # 标签留空由业务方填。
                        "claims": [
                            {"claim": c.claim, "verifier_supported": bool(c.supported)}
                            for c in outcome.answer.report.claims
                        ],
                        "fallback": bool(outcome.answer.fallback),
                        "degraded": bool(outcome.answer.degraded),
                        "elapsed_s": round(elapsed, 2),
                    }
                )
                session.rollback()
            if number % 10 == 0 or number == len(probes):
                rate = (time.monotonic() - started) / number
                print(
                    f"  {number}/{len(probes)} · 平均 {rate:.1f}s/条 · "
                    f"预计剩余 {rate * (len(probes) - number) / 60:.1f} 分钟",
                    flush=True,
                )
    finally:
        # ② 清理：只删自己写的那些，并复核还原到位
        with session_scope() as cleanup:
            cleanup.execute(
                delete(EpisodicMemory).where(EpisodicMemory.session_id.startswith("m9a-"))
            )
            cleanup.execute(
                delete(ProceduralMemory).where(
                    ProceduralMemory.memory_id.notin_(pre_existing or {"__none__"})
                )
            )
            cleanup.commit()
            left_epi = cleanup.scalar(
                select(func.count())
                .select_from(EpisodicMemory)
                .where(EpisodicMemory.session_id.startswith("m9a-"))
            )
            left_proc = set(cleanup.scalars(select(ProceduralMemory.memory_id)))
            print(
                f"清理完成：残留 m9a 情景 {left_epi} 条；"
                f"偏好行还原一致={left_proc == pre_existing}",
                flush=True,
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"✅ {len(records)} 条已冻结到 {out_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_probes")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args.limit, args.out)


if __name__ == "__main__":
    sys.exit(main())
