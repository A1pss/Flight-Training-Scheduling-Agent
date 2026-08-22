"""把召回 doc id 还原成**原文**（`judge_calib_50` 的标注前置）。

## 为什么要还原而不是重跑

`answers_v1.jsonl` 只记了召回条目的 **id**。但 Faithfulness 的判定对象是
「这条断言有没有被**召回内容**支撑」——**没有原文，标注者与 judge 都无从判断**。

重跑一遍能拿到原文，但那会得到**另一批回答**，而 §12.4.1 的全部意义在于
「人工与 judge 面对同一批文本」。所以这里**不重跑 LLM**：语料与结构化事实都是
确定性的，按 id 反查即可。

三类 id 三条还原路径：

| 前缀 | 还原方式 |
|---|---|
| `ent:` / `rule:` / `epi:` | 语料（`build_corpus`，与跑批时同一份构造） |
| `pg:` | `memory/semantic.py` 的 `*_fact(...).doc()` —— 路 A 发 id 的同一处代码 |
| `proc:` | `list_preferences` + `preference_docs` |
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.memory import semantic
from backend.memory.procedural import list_preferences
from backend.models.memory import EpisodicMemory, ProceduralMemory
from backend.retrieval.corpus import build_corpus
from tests.datasets.memory_catalog import at_hour
from tests.datasets.memory_seed import seed_timeline
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot

SNIPPET_LIMIT: Final[int] = 400


def build_text_index(session: Session) -> dict[str, str]:
    """doc_id → 原文。**调用方负责事务边界**（本函数会写时间线，请在可回滚的会话里用）。"""
    snapshot_id = ensure_baseline_snapshot(session)
    session.execute(delete(ProceduralMemory))
    session.execute(delete(EpisodicMemory).where(EpisodicMemory.session_id.startswith("m9a-")))
    session.flush()
    seed_timeline(session)

    index: dict[str, str] = {}
    for doc in build_corpus(session, snapshot_id).docs:
        index[doc.doc_id] = doc.text

    for person in semantic.all_persons(session, snapshot_id):
        pdoc = person.doc()
        index[pdoc.doc_id] = pdoc.text
        for qual in semantic.qualification_facts(session, snapshot_id, person.person_id):
            qdoc = qual.doc()
            index[qdoc.doc_id] = qdoc.text
    # ★ 每个循环用各自的变量名：四类实体的 `doc()` 返回的不是同一个类型，
    #   复用一个 `doc` 变量会让 mypy --strict 按第一次赋值把类型钉死。
    for plane in semantic.all_aircraft(session, snapshot_id):
        adoc = plane.doc()
        index[adoc.doc_id] = adoc.text
    for mission in semantic.all_missions(session, snapshot_id):
        mdoc = mission.doc()
        index[mdoc.doc_id] = mdoc.text
    for airspace in semantic.airspace_facts(session, snapshot_id):
        sdoc = airspace.doc()
        index[sdoc.doc_id] = sdoc.text

    at = at_hour(20, 23)
    for row in list_preferences(session, at=at):
        value = "、".join(f"{k}={row.value[k]}" for k in sorted(row.value))
        index[f"proc:{row.namespace}/{row.key}"] = (
            f"偏好 {row.namespace}/{row.key}：{value}（来源：{row.source}）"
        )
    return index


def snippet(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:SNIPPET_LIMIT]


__all__ = ["SNIPPET_LIMIT", "build_text_index", "snippet"]
