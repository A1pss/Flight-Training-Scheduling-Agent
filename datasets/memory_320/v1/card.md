# Dataset Card · `memory_320` v1

| 项 | 值 |
|---|---|
| 版本 | `v1` |
| 状态 | `approved` —— 业务方已确认，可用于实验 |
| 条数 | **320** |
| 数据文件 | `items.jsonl` |
| SHA256 | `78613c6b5bc50152d71a3bf3a27f269e881d16e762efdea7843f0a7a23f04bfb` |
| 生成时间 | 2026-08-19T16:07:13Z |
| 业务方确认 | Alps · 2026-08-20 |

## 分层分布

| 分层 | 条数 |
|---|---|
| `episodic` | 120 |
| `procedural` | 80 |
| `semantic` | 120 |
| **合计** | **320** |

## 构造方法

20 周合成会话历史（122 条情景记忆）落库 → 跑现有的 procedural.distill() 蒸馏出偏好 → 探针照着真实写入的记录标 gold id。情景与程序两类的 gold **不是编的**，由内容寻址 id 与蒸馏结果倒推，tests/datasets/test_memory_timeline_live.py 在真库上逐条验证。

## 判读上下文

| 键 | 值 |
|---|---|
| `archive_horizon` | 60 周（最长 cycle_weeks 20 × 3，Z-18）—— 20 周内不会有记忆被归档 |
| `preference_versions` | relaxation/preferred_tier 有两版：第 4 周对话推断 Tier 0 → 第 20 周排班确认记录 Tier 1 |
| `ruleset_version` | 1.3.0（rule: 前缀的 doc id 含它） |
| `timeline_events` | 122 = 20 周 × 6 条 + 2 条成对时效事件 |
| `timeline_start` | 2026-01-05（第 1 周 = 基准周） |
| `timeline_weeks` | 20（第 20 周 = 2026-05-18） |

## 规格依据

- v6 §12.4
- v6 §6.1
- v6 §6.2
- v6 §6.4
- v6 §1.3
- SPEC_DECISIONS §D

## 已知局限

1. **没有跑道事实探针**：`entity_docs()` 只为 person / aircraft / mission / airspace 四类发实体摘要文档，跑道在语料里没有召回单位。硬给它安一个 gold 会让那条题变成在测规则召回 —— 宁可缺这一类，也不做一条测错东西的题。
2. 程序记忆当前**没有 doc id**：`preference_docs()` 只返回句子。本集约定 `proc:<namespace>/<key>` 作为召回单位，W13 侧要补一个发 id 的适配（约 3 行），否则程序类的 Recall@5 无从计算。
3. 汇总类探针（如「一共几架 JL-8」）的 gold 有 6 条，Top-5 装不下 —— 这类题的正确判据是答案对不对，不是 gold 是否全进 Top-5，报数时要单列。
4. `absent` 负例的 gold 为空，不进 Recall@5 的分母，单独统计误召回率。
5. 程序记忆只覆盖 relaxation 与 phrasing 两个命名空间；`NAMESPACE_INSTRUCTOR`（教员排班习惯）至今没有可测定义，按铁律 5 不自造，故无探针。
6. 标注口径按 SPEC_DECISIONS §D：Claude Code 初稿 + Alps 人工复核，**不计算也不报告双人标注的 Cohen's Kappa**。

## 怎么用

```python
from backend.datasets.loader import load_eval_dataset

manifest, items = load_eval_dataset("memory_320", require_approved=True)
```

加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 `DatasetIntegrityError`。手工改过数据文件之后必须跑 `python -m backend.datasets.cli refresh memory_320`。
