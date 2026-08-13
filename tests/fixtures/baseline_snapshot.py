"""按需把基准快照建起来（CLAUDE.md §6 的那条坑）。

> **集成测试不许断言「环境外状态」**——「库里应当已经有一个 ACTIVE 快照」这类
> 断言等于假设有人在测试之外先跑过某个命令，本地绿、CI 红。
> **要断言什么就在用例里自己建起来。**

## 为什么以前没人踩到

`tests/integration/` 里的文件按文件名顺序收集，而
`test_ingestion_pipeline_live.py` 恰好排在 `test_report_*` / `test_solver_*`
前面——它跑完顺手把 ACTIVE 快照留下了，后面那几个就都有得用。**那是巧合，
不是设计**：M4-B 新增的 `test_diagnosis_agent_live.py` 与 `test_graph_live.py`
按字母序排在 `test_ingestion_*` **之前**，于是在一个全新的 CI 库上，
它们会以「库里没有 ACTIVE 快照」直接红——而本地因为库里早有数据，全绿。

本模块把这件事变成显式的：**要快照就调 `ensure_baseline_snapshot()`**，
有就直接用，没有就现建一份，与 `python -m backend.ingestion.cli --baseline`
同口径（同一份 §5.5 裁定表、同一批答案）。

## 它不绕过人工门禁

`--baseline` 用 §5.5 的**已裁定**结果自动作答，批准人记进审计日志。这里复用的
是同一条路径（`gate.review` + `baseline_resolutions` + `baseline_answers`），
不是「跳过确认直接入库」。
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.orm import Session

from backend.core.config import PROJECT_ROOT
from backend.ingestion.gate import baseline_answers, baseline_resolutions, review
from backend.ingestion.loader import active_snapshot_id
from backend.ingestion.pipeline import prepare

#: 与 `backend/ingestion/cli.py::BASELINE_APPROVER` 同口径
TEST_APPROVER: Final[str] = "baseline(v6 §5.5 裁定表)"

ORIGIN_DIR: Final = PROJECT_ROOT / "data" / "origin"


def ensure_baseline_snapshot(session: Session) -> str:
    """返回一个 ACTIVE 快照 id；库里没有就现建一份。

    **已有就原样返回**，不重建——重建会换一个 `snapshot_id`，而基准周实测的
    `snap_9724982865ee` 是 M2-A/M3 一路引用的锚点。
    """
    existing = active_snapshot_id(session)
    if existing:
        return existing
    return build_baseline_snapshot(session)


def build_baseline_snapshot(session: Session) -> str:
    """跑一遍 `--baseline` 等价的摄取，返回新快照 id。

    `write_vectors=False` + `HashEmbedder`：这里要的是 PG 里的事实与
    `training_progress`，不是 Chroma 里的向量。CI 上没有 bge-m3 那 2.2GB 权重
    （`.data/` 已 gitignore），拉真模型进来只会让建快照这件事变慢又变脆。
    """
    from backend.ingestion.pipeline import commit
    from backend.memory.embeddings import HashEmbedder

    sources = sorted(ORIGIN_DIR.glob("*.pdf"))
    if not sources:
        raise FileNotFoundError(
            f"{ORIGIN_DIR} 下没有 PDF —— 基准快照建不起来。"
            "`data/origin/*.pdf` 是只读的原始业务资料，应当随仓库一起入库"
        )
    # `session=None`：本次不以任何既有 ACTIVE 快照为基线（这是「建基准」不是「增量」）
    prepared = prepare(sources, session=None)
    decision = review(
        prepared.changeset,
        baseline_resolutions(prepared.changeset, decided_by=TEST_APPROVER),
        answers=baseline_answers(prepared.changeset),
        approver=TEST_APPROVER,
    )
    if not decision.approved:
        raise RuntimeError(f"基准摄取未获批准：{decision.reasons}")
    result = commit(
        prepared,
        decision,
        session,
        ruleset_version=_ruleset_version(),
        embedder=HashEmbedder(),
        write_vectors=False,
    )
    session.commit()
    return str(result.snapshot_id)


def _ruleset_version() -> str:
    from backend.core.ruleset import get_ruleset

    return str(get_ruleset().version)


__all__ = [
    "ORIGIN_DIR",
    "TEST_APPROVER",
    "build_baseline_snapshot",
    "ensure_baseline_snapshot",
]
