"""把构造代码的产物落成 `datasets/<name>/<version>/` 下的数据文件。

```bash
PYTHONPATH=. python tests/datasets/write_datasets.py nl_360
```

**不是测试**，是生成器入口。`tests/datasets/test_nl_360.py` 会断言仓库里的
`items.jsonl` 与构造代码的输出**逐字节相同** —— 于是「手改了数据但忘了改代码」
和「改了代码但忘了重生成」两种漂移都会在 CI 上变成红。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from backend.datasets.card import render_card
from backend.datasets.loader import dataset_dir, load_eval_dataset
from backend.datasets.manifest import DatasetManifest, load_manifest, write_jsonl, write_manifest
from tests.datasets import memory_catalog, memory_probes, nl_catalog, trajectory_catalog


def _previous(directory: Path) -> DatasetManifest | None:
    """上一版清单（用于判断批准状态能不能延续）。"""
    try:
        return load_manifest(directory)
    except FileNotFoundError:
        return None


def write_nl_360() -> None:
    rows = nl_catalog.build()
    directory = dataset_dir("nl_360")
    sha = write_jsonl(directory / "items.jsonl", rows)
    strata: dict[str, int] = {}
    for row in rows:
        layer = str(row["layer"])
        strata[layer] = strata.get(layer, 0) + 1

    previous = _previous(directory)
    keep = previous is not None and previous.sha256 == sha
    manifest = DatasetManifest(
        name="nl_360",
        version="v1",
        stage=previous.stage if (keep and previous is not None) else "draft",
        item_count=len(rows),
        strata=dict(sorted(strata.items())),
        sha256=sha,
        generated_at=(
            previous.generated_at
            if keep and previous
            else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        method=(
            "Claude Code 逐条构造（重复度高的层用程序化组合保证覆盖齐全）→ Alps 逐批人工复核。"
            "实体一律取自 v6 §1.3 基准实体表；构造代码见 tests/datasets/nl_catalog.py。"
        ),
        spec_refs=[
            "v6 §12.2",
            "v6 §1.3",
            "SPEC_DECISIONS §D",
            "v6 §7.2.1",
            "v6 §5.4",
            "v6 §12.5.3",
        ],
        known_limitations=[
            "六类意图中 ingest / export 只有 5 条样本 —— §12.2 的六层分布没有给这两类"
            "留独立分层，它们只出现在歧义层与对抗层的多意图样本里。意图分类准确率要按类"
            "分别报，这两类的置信区间会很宽。",
            "相对周表述（本周/下周）的判读依赖 context.eval_today=2026-01-05，换参照日会"
            "改变期望槽位。",
            "标注口径按 SPEC_DECISIONS §D：Claude Code 初稿 + Alps 人工复核，"
            "**不计算也不报告双人标注的 Cohen's Kappa**（v6 §12.7 必述项 2）。",
            "「约束修饰」槽位里 kind=OTHER 的条目共 6 条，它们表达的是冻结档位或目标权重"
            "偏好（R3），DSL 中没有对应的 IncrementalConstraint —— 修订翻译准确率统计时"
            "要把它们单列，不能算作翻译失败。",
        ],
        context={
            "eval_today": "2026-01-05",
            "baseline_week": "2026W02",
            "week_format": "YYYYWww（与 backend.schemas.intent 的 iso_week 正则一致）",
            "ruling_typo": "唯一候选就执行；候选不唯一则反问（业务方 2026-08-19）",
            "ruling_multi_intent": "取主意图执行；副意图的周次不进槽位（业务方 2026-08-19）",
            "ruling_missing_week": "缺周次一律归歧义层，期望动作 ask_clarify（业务方 2026-08-19）",
        },
        approved_by=previous.approved_by if keep and previous else None,
        approved_at=previous.approved_at if keep and previous else None,
    )
    write_manifest(directory, manifest)
    (directory / "card.md").write_text(render_card(manifest), encoding="utf-8", newline="\n")
    loaded_manifest, items = load_eval_dataset("nl_360")
    print(f"✅ nl_360 {len(items)} 条 · {loaded_manifest.sha256[:16]}… · {loaded_manifest.strata}")


def write_memory_320() -> None:
    """探针集 + 20 周时间线。

    时间线与探针一起版本化：`episodic_timeline.jsonl` 是 122 条情景记忆的
    **规格**（不是导出的库内容），`memory_seed.seed_timeline()` 照着它写库。
    两者由 `epi:` 的内容寻址 id 绑在一起 —— 时间线改一个字，gold id 全变，
    `test_memory_timeline_live.py` 当场红。
    """
    rows = memory_probes.build_full()
    directory = dataset_dir("memory_320")
    sha = write_jsonl(directory / "items.jsonl", rows)
    write_jsonl(
        directory / "episodic_timeline.jsonl",
        [
            {
                "memory_id": record.memory_id(),
                "doc_id": memory_catalog.epi_doc_id(record),
                "session_id": record.session_id,
                "kind": record.kind,
                "summary": record.summary,
                "content": dict(record.content),
                "occurred_at": record.occurred_at.isoformat(),
            }
            for record in memory_catalog.timeline_records()
        ],
    )
    strata: dict[str, int] = {}
    for row in rows:
        key = str(row["memory_type"])
        strata[key] = strata.get(key, 0) + 1

    previous = _previous(directory)
    keep = previous is not None and previous.sha256 == sha
    manifest = DatasetManifest(
        name="memory_320",
        version="v1",
        stage=previous.stage if (keep and previous is not None) else "draft",
        item_count=len(rows),
        strata=dict(sorted(strata.items())),
        sha256=sha,
        generated_at=(
            previous.generated_at
            if keep and previous
            else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        method=(
            "20 周合成会话历史（122 条情景记忆）落库 → 跑现有的 procedural.distill() "
            "蒸馏出偏好 → 探针照着真实写入的记录标 gold id。情景与程序两类的 gold "
            "**不是编的**，由内容寻址 id 与蒸馏结果倒推，"
            "tests/datasets/test_memory_timeline_live.py 在真库上逐条验证。"
        ),
        spec_refs=["v6 §12.4", "v6 §6.1", "v6 §6.2", "v6 §6.4", "v6 §1.3", "SPEC_DECISIONS §D"],
        known_limitations=[
            "**没有跑道事实探针**：`entity_docs()` 只为 person / aircraft / mission / "
            "airspace 四类发实体摘要文档，跑道在语料里没有召回单位。硬给它安一个 gold "
            "会让那条题变成在测规则召回 —— 宁可缺这一类，也不做一条测错东西的题。",
            "程序记忆当前**没有 doc id**：`preference_docs()` 只返回句子。本集约定 "
            "`proc:<namespace>/<key>` 作为召回单位，W13 侧要补一个发 id 的适配"
            "（约 3 行），否则程序类的 Recall@5 无从计算。",
            "汇总类探针（如「一共几架 JL-8」）的 gold 有 6 条，Top-5 装不下 —— "
            "这类题的正确判据是答案对不对，不是 gold 是否全进 Top-5，报数时要单列。",
            "`absent` 负例的 gold 为空，不进 Recall@5 的分母，单独统计误召回率。",
            "程序记忆只覆盖 relaxation 与 phrasing 两个命名空间；`NAMESPACE_INSTRUCTOR`"
            "（教员排班习惯）至今没有可测定义，按铁律 5 不自造，故无探针。",
            "标注口径按 SPEC_DECISIONS §D：Claude Code 初稿 + Alps 人工复核，"
            "**不计算也不报告双人标注的 Cohen's Kappa**。",
        ],
        context={
            "timeline_start": "2026-01-05（第 1 周 = 基准周）",
            "timeline_weeks": "20（第 20 周 = 2026-05-18）",
            "timeline_events": "122 = 20 周 × 6 条 + 2 条成对时效事件",
            "archive_horizon": "60 周（最长 cycle_weeks 20 × 3，Z-18）—— 20 周内不会有记忆被归档",
            "preference_versions": "relaxation/preferred_tier 有两版：第 4 周对话推断 Tier 0 → 第 20 周排班确认记录 Tier 1",
            "ruleset_version": "1.3.0（rule: 前缀的 doc id 含它）",
        },
        approved_by=previous.approved_by if keep and previous else None,
        approved_at=previous.approved_at if keep and previous else None,
    )
    write_manifest(directory, manifest)
    (directory / "card.md").write_text(render_card(manifest), encoding="utf-8", newline="\n")
    loaded, items = load_eval_dataset("memory_320")
    print(f"✅ memory_320 {len(items)} 条 · {loaded.sha256[:16]}… · {loaded.strata}")


def write_trajectory_100() -> None:
    """轨迹标注。分层字段是 `flow`，两处受控自治必须过半（§12.6.2）。"""
    rows = trajectory_catalog.build_full()
    directory = dataset_dir("trajectory_100")
    sha = write_jsonl(directory / "items.jsonl", rows)
    strata: dict[str, int] = {}
    for row in rows:
        key = str(row["flow"])
        strata[key] = strata.get(key, 0) + 1

    previous = _previous(directory)
    keep = previous is not None and previous.sha256 == sha
    manifest = DatasetManifest(
        name="trajectory_100",
        version="v1",
        stage=previous.stage if (keep and previous is not None) else "draft",
        item_count=len(rows),
        strata=dict(sorted(strata.items())),
        sha256=sha,
        generated_at=(
            previous.generated_at
            if keep and previous
            else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        method=(
            "Claude Code 逐条构造 → Alps 逐批人工复核。路径元素取自真实的图节点"
            "（backend/graph/graph.py 的 add_node）与工具目录；每个步骤的工具都经 "
            "ACL 矩阵校验，越权的组合根本写不进数据集。"
        ),
        spec_refs=["v6 §12.6", "v6 §7.5", "v6 §7.7.2", "v6 §3.9.1", "v6 §5.1", "SPEC_DECISIONS §D"],
        known_limitations=[
            "自治两类（query 30 + diagnosis 25 = 55 条）按 §12.6.2 占了一半以上；"
            "排班/重排/修订三类的期望路径是**固定序列**，轨迹评估在那里只验「没跑偏」。",
            "路径判定用最长公共子序列相似度（§12.6.2），所以 acceptable_paths 给的是"
            "**完整序列**而不是规则描述；三条准入规则（A 顺序 / B 可省 / C 迭代次数）"
            "写在构造代码的模块文档里，供人复核，判定器不读它。",
            "摄取流程不在对话图内（走 POST /api/v1/ingest），其路径元素用 "
            "ingest.prepare / ingest.gate / ingest.commit 三个阶段名表示，"
            "它们不是图节点。",
            "标注口径按 SPEC_DECISIONS §D：Claude Code 初稿 + Alps 人工复核，"
            "**不计算也不报告双人标注的 Cohen's Kappa**。",
        ],
        context={
            "graph_source": "backend/graph/graph.py 的 add_node/destinations",
            "acceptable_rules": "A 同层并列顺序 / B 信息足够时省略 / C 自治循环迭代次数",
            "forbidden_rules": "D 跳过确定性节点 / E 弱工具替代强工具 / F 不调工具直接答",
            "knowledge_max_steps": "6（KNOWLEDGE_MAX_STEPS）",
            "probe_budget": "5 次 / 单次 30s / 累计 120s（与 LLM 预算互不挤占）",
        },
        approved_by=previous.approved_by if keep and previous else None,
        approved_at=previous.approved_at if keep and previous else None,
    )
    write_manifest(directory, manifest)
    (directory / "card.md").write_text(render_card(manifest), encoding="utf-8", newline="\n")
    loaded, items = load_eval_dataset("trajectory_100")
    print(f"✅ trajectory_100 {len(items)} 条 · {loaded.sha256[:16]}… · {loaded.strata}")


WRITERS = {
    "nl_360": write_nl_360,
    "memory_320": write_memory_320,
    "trajectory_100": write_trajectory_100,
}


def main(argv: list[str]) -> int:
    names = argv[1:] or sorted(WRITERS)
    for name in names:
        WRITERS[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
