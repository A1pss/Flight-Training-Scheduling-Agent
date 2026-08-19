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
from backend.datasets.manifest import (
    DatasetManifest,
    load_manifest,
    sha256_of,
    write_jsonl,
    write_manifest,
)
from tests.datasets import (
    legacy_catalog,
    memory_catalog,
    memory_probes,
    nl_catalog,
    ood_catalog,
    seed_catalog,
    tool_call_catalog,
    trajectory_catalog,
)


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


def write_tool_calls_200() -> None:
    """工具调用场景。**程序化生成，标签天然正确** —— 需要复核的是分布不是逐条。"""
    rows = tool_call_catalog.build()
    directory = dataset_dir("tool_calls_200")
    sha = write_jsonl(directory / "items.jsonl", rows)
    strata: dict[str, int] = {}
    for row in rows:
        key = str(row["stratum"])
        strata[key] = strata.get(key, 0) + 1

    previous = _previous(directory)
    keep = previous is not None and previous.sha256 == sha
    manifest = DatasetManifest(
        name="tool_calls_200",
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
            "由实体表 + 工具 schema **反向构造**，标签天然正确："
            "valid 层的参数由工具自己的 params_model 生成并校验；"
            "越权层的 (组件, 工具) 取自 ACL 矩阵的补集；"
            "超预算层是预算池设成 0 之后的必然结果。无一处依赖人的判断。"
        ),
        spec_refs=["v6 §12.5.1", "v6 §7.7.2", "v6 §3.9.2", "v6 §9.3"],
        known_limitations=[
            "200 条 valid 的权重取自 trajectory_100 的 242 个工具步骤 —— 那是目前唯一一份"
            "「工具在真实流程里各出现多少次」的数据。**它是一个可替换的假设**："
            "W13 真实跑过之后应该用线上日志的频次重算，而不是继续用轨迹集的。",
            "每个工具设了 2 条地板，否则频率为 0 的工具（escalate / memory.write / "
            "render_workbook 等）一条都分不到，而 §12.5.1 的契约通过率要覆盖全部工具。",
            "越权层里 6 条是**凭空编出来的工具名**（六个确定性节点），它们不在目录里，"
            "`tool_exists=False`。这与「有工具但没权限」是两种不同的失败模式。",
            "超预算层的 6 条探针场景 `expected_error_code` 为 None —— "
            "探针池耗尽时不抛错，优雅返回 BUDGET_EXHAUSTED 载荷（§3.9.2）。",
            "本集**不需要逐条人工复核**（标签是算出来的），需要复核的是分布。",
        ],
        context={
            "weight_source": "trajectory_100 v1 的 242 个工具步骤频次",
            "floor_per_tool": "2",
            "acl_source": "backend.harness.acl.ACL_MATRIX 的补集（字典序，可复现）",
            "error_codes": "越权 FTS-4004 / Harness 预算 FTS-4003 / 探针池无错误码",
        },
        approved_by=previous.approved_by if keep and previous else None,
        approved_at=previous.approved_at if keep and previous else None,
    )
    write_manifest(directory, manifest)
    (directory / "card.md").write_text(render_card(manifest), encoding="utf-8", newline="\n")
    loaded, items = load_eval_dataset("tool_calls_200")
    print(f"✅ tool_calls_200 {len(items)} 条 · {loaded.sha256[:16]}… · {loaded.strata}")


def write_plan_scenarios() -> None:
    """**只加卡片，不动数据。**

    `items_file` 直接指向 W4 落的 `scenarios.json`（138 KB 的 JSON 数组）——
    加载器为此支持了数组载体。复制成 jsonl 会立刻产生两个真相。
    写卡片之前先跑一遍核对（条数 / 跑道关闭 / I1~I5 五族），有问题就不发卡片。
    """
    rows = legacy_catalog.scenario_rows()
    problems = legacy_catalog.verify_scenarios(rows)
    if problems:
        raise SystemExit("plan_scenarios 核对未通过：\n  " + "\n  ".join(problems))

    directory = dataset_dir("plan_scenarios")
    strata: dict[str, int] = {}
    for row in rows:
        key = str(row["category"])
        strata[key] = strata.get(key, 0) + 1

    previous = _previous(directory)
    sha = sha256_of(directory / "scenarios.json")
    keep = previous is not None and previous.sha256 == sha
    manifest = DatasetManifest(
        name="plan_scenarios",
        version="v1",
        stage=previous.stage if (keep and previous is not None) else "draft",
        items_file="scenarios.json",
        item_count=len(rows),
        strata=dict(sorted(strata.items())),
        sha256=sha,
        generated_at=(
            previous.generated_at
            if keep and previous
            else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        method=(
            "W4 由 tests/scenarios/catalog.py 程序化生成（实体编号一律从快照读，"
            "组合扰动用固定种子 20260812）。M9-A **只做核对与版本化，一条数据都没改**："
            "核对了类别条数、单点扰动是否含跑道关闭、不可行是否为 I1~I5 五族"
            "且每族 6 个变体、以及每条不可行是否都标注了真实冲突源。"
        ),
        spec_refs=["v6 §12.3", "v6 §3.9", "v6 §1.4"],
        known_limitations=[
            "单点扰动里跑道族只有 2 条 —— 全场就 2 条跑道，这是数据本身的上限，不是覆盖不足。",
            "单点/组合扰动的 expected_status 是 EITHER：**不预设可行与否**，那正是要跑出来的。"
            "预设了会诱导「为了对上预期而放宽约束」（CLAUDE.md §7 第 4 条）。",
            "构建记录在 build_manifest.json（快照 id / 种子 / 实体表）；本文件是数据集卡片，"
            "两者内容与用途都不同。",
            "边界场景的「恰好」由成对定义互证（恰好够 + 紧一格即不可行），"
            "标定过程在 calibration.json。",
        ],
        context={
            "snapshot_id": "snap_9724982865ee",
            "week_start": "2026-01-05",
            "combo_seed": "20260812",
            "infeasible_families": "I1~I5，每族 6 个沿同一方向更紧的变体",
        },
        approved_by=previous.approved_by if keep and previous else None,
        approved_at=previous.approved_at if keep and previous else None,
    )
    write_manifest(directory, manifest)
    (directory / "card.md").write_text(render_card(manifest), encoding="utf-8", newline="\n")
    loaded, items = load_eval_dataset("plan_scenarios")
    print(f"✅ plan_scenarios {len(items)} 条 · {loaded.sha256[:16]}… · {loaded.strata}")


def write_golden_40() -> None:
    """黄金用例的**索引 + 指纹**。yml 本体留在 tests/golden/，不复制。"""
    rows = legacy_catalog.golden_rows()
    directory = dataset_dir("golden_40")
    sha = write_jsonl(directory / "items.jsonl", rows)
    strata: dict[str, int] = {}
    for row in rows:
        key = str(row["status"])
        strata[key] = strata.get(key, 0) + 1

    previous = _previous(directory)
    keep = previous is not None and previous.sha256 == sha
    manifest = DatasetManifest(
        name="golden_40",
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
            "W4 由 pytest-regressions 落的 40 份基线快照（tests/golden/test_golden_plans/*.yml）。"
            "M9-A **只抽索引与指纹，不复制数据本体**：用例名、状态、架次数、候选数、"
            "content_sha256、两条校验通道的判定、阻塞项与欠账条数。"
        ),
        spec_refs=["v6 §12.1", "v6 §3.11.1", "v6 §11.4"],
        known_limitations=[
            "40 条里 2 条是 INFEASIBLE（空域关闭、关闭叠跑道）——它们没有方案，"
            "因而没有 content_sha256。这与 Z-26 一致：两种状态都确定性可复现，"
            "**唯一不许出现的是 FEASIBLE**（被预算截断，不保证逐字节可复现，§3.11.1）。",
            "38 个 OPTIMAL 用例只有 30 个互不相同的指纹 —— 有 8 条与别的用例排出了"
            "**逐字节相同**的方案（合成场景规模小，不同旋钮可能落到同一个最优解）。"
            "这不影响回归价值：变化仍然会被看见。",
            "本集是**索引**，不是数据本体。更新基线的唯一正确姿势仍是 "
            "`pytest tests/golden -q --force-regen` 然后逐行读 diff。",
        ],
        context={
            "baseline_dir": "tests/golden/test_golden_plans/",
            "aggregate_fingerprint": "deploy/scripts/golden_fingerprint.py（两条部署路径的门禁）",
            "m8_fingerprint": "4dc4df24…f0c0aca（native 与 compose 两条路径同值）",
        },
        approved_by=previous.approved_by if keep and previous else None,
        approved_at=previous.approved_at if keep and previous else None,
    )
    write_manifest(directory, manifest)
    (directory / "card.md").write_text(render_card(manifest), encoding="utf-8", newline="\n")
    loaded, items = load_eval_dataset("golden_40")
    print(f"✅ golden_40 {len(items)} 条 · {loaded.sha256[:16]}… · {loaded.strata}")


def write_ood_200() -> None:
    """领域外通用能力回归集。判定口径见 backend/datasets/ood_judge.py。"""
    rows = ood_catalog.build()
    directory = dataset_dir("ood_200")
    sha = write_jsonl(directory / "items.jsonl", rows)
    strata: dict[str, int] = {}
    for row in rows:
        key = str(row["layer"])
        strata[key] = strata.get(key, 0) + 1

    previous = _previous(directory)
    keep = previous is not None and previous.sha256 == sha
    manifest = DatasetManifest(
        name="ood_200",
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
            "全部自建（§15.5：不使用任何外部数据集）。常识/语言/拒绝/多轮为手写，"
            "指令跟随与算术为程序生成（答案由构造过程直接给出）。"
            "**全部可程序判定**，五种判据无一依赖 LLM。"
            "选择题的正确项位置经程序均衡到 A/B/C/D 各四分之一。"
        ),
        spec_refs=["v6 §15.4", "v6 §15.5", "v6 §12.4.1（判定口径的分界）"],
        known_limitations=[
            "拒绝层用的是**规则匹配**（命中拒绝标记 ∧ 未命中 forbidden），不如人判得准。"
            "它的用途是配对回归 —— 同题、同判定器、基线 vs 微调，判定器的系统性偏差在 "
            "McNemar 的配对差分里会抵消。**报告里必须写清这一点**，不能让它看起来像"
            "一个绝对水平的分数。",
            "「领域外」由 DOMAIN_TERMS 红线保证（18 个领域词一个都不许出现，加载期强制）。"
            "但这只挡得住**字面**重合；如果微调让模型整体更倾向结构化短输出，"
            "指令跟随层可能反而变好 —— 那不是遗忘，报数时要照实说。",
            "200 条的量级决定了单个子层只有 20~40 条，子层的置信区间很宽。"
            "所以门槛设在整体指标上，子层下降 >8 个点只作**警示**不作否决。",
            "本集**不复用 §12.4.1 的 32B judge** —— 那个口径至今未经业务方裁定，"
            "按铁律 5 不得自行套用（v6 §12.4.1 末尾原文）。",
        ],
        context={
            "ruling": "业务方 2026-08-19 裁定 O-A：确定性判据 + McNemar 配对精确检验",
            "threshold": "整体准确率绝对下降 ≤3 个百分点 且 p ≥ 0.05（两个条件是「且」）",
            "layer_warning": "任一子层下降 >8 个百分点单列警示，不否决",
            "judge_impl": "backend/datasets/ood_judge.py（grade / mcnemar_exact / regression_verdict）",
        },
        approved_by=previous.approved_by if keep and previous else None,
        approved_at=previous.approved_at if keep and previous else None,
    )
    write_manifest(directory, manifest)
    (directory / "card.md").write_text(render_card(manifest), encoding="utf-8", newline="\n")
    loaded, items = load_eval_dataset("ood_200")
    print(f"✅ ood_200 {len(items)} 条 · {loaded.sha256[:16]}… · {loaded.strata}")


def write_sft_seed() -> None:
    """SFT 种子。**只备种子，合成管线是 W12 的事。**"""
    rows = seed_catalog.build()
    directory = dataset_dir("sft_seed")
    sha = write_jsonl(directory / "items.jsonl", rows)
    strata: dict[str, int] = {}
    for row in rows:
        key = str(row["kind"])
        strata[key] = strata.get(key, 0) + 1

    previous = _previous(directory)
    keep = previous is not None and previous.sha256 == sha
    manifest = DatasetManifest(
        name="sft_seed",
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
            "60 条需求表述从 nl_360 的排班/指定/重排三层确定性抽样（每层按固定步长取 20）；"
            "14 条规则从 rules/ruleset_v1.3.yaml **读出来**；13 条语义假设从 "
            "rules/semantics.yaml 读；36 条实体来自 v6 §1.3 基准实体表。"
            "**没有一条是手抄的** —— 手抄会在下一次改规则时悄悄分叉。"
        ),
        spec_refs=["v6 §15.2", "v6 §1.1", "v6 §1.3", "v6 §12.2"],
        known_limitations=[
            "**本集只是种子，不是训练样本。** §15.2 的六步合成管线（指令扩写 → 学生自采样 "
            "→ 确定性过滤 → 教师补硬样本 → 程序化生成 → 难负例挖掘）是 W12 的交付物。",
            "60 条需求表述与 nl_360 同源 —— 用它们合成的样本若拿去评 nl_360，"
            "会有**训练/评测同源**的问题。W12 合成时要么换池子、要么在报告里声明。",
            "规则与语义假设跟着 ruleset_version=1.3.0 / semantics_version=1.1.0 走："
            "任一版本变动，本集的 sha256 必变、批准状态自动失效。",
            "难负例（近音近形、歧义、注入）**不在种子里** —— §15.2 把它们放在第 ⑥ 步"
            "「难负例挖掘」，输入是 §12.5.1 的失败模式分布表，那要 W13 跑完才有。",
        ],
        context={
            "ruleset_version": "1.3.0",
            "semantics_version": "1.1.0",
            "sampling": "每层步长 = len(pool) // 20，确定性",
            "pipeline_owner": "W12（M7 微调前的数据合成）",
        },
        approved_by=previous.approved_by if keep and previous else None,
        approved_at=previous.approved_at if keep and previous else None,
    )
    write_manifest(directory, manifest)
    (directory / "card.md").write_text(render_card(manifest), encoding="utf-8", newline="\n")
    loaded, items = load_eval_dataset("sft_seed")
    print(f"✅ sft_seed {len(items)} 条 · {loaded.sha256[:16]}… · {loaded.strata}")


WRITERS = {
    "nl_360": write_nl_360,
    "memory_320": write_memory_320,
    "trajectory_100": write_trajectory_100,
    "tool_calls_200": write_tool_calls_200,
    "plan_scenarios": write_plan_scenarios,
    "golden_40": write_golden_40,
    "ood_200": write_ood_200,
    "sft_seed": write_sft_seed,
}


def main(argv: list[str]) -> int:
    names = argv[1:] or sorted(WRITERS)
    for name in names:
        WRITERS[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
