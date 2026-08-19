# Dataset Card · `nl_360` v1

| 项 | 值 |
|---|---|
| 版本 | `v1` |
| 状态 | `approved` —— 业务方已确认，可用于实验 |
| 条数 | **360** |
| 数据文件 | `items.jsonl` |
| SHA256 | `20b105c23a78ec348424d7b6b350e8cb55f4f9c8038286373e0a48a8a5345ae1` |
| 生成时间 | 2026-08-19T19:35:30Z |
| 业务方确认 | Alps · 2026-08-20 |

## 分层分布

| 分层 | 条数 |
|---|---|
| `adversarial` | 60 |
| `ambiguous` | 60 |
| `disrupted_reschedule` | 60 |
| `info_query` | 60 |
| `standard_schedule` | 60 |
| `targeted_schedule` | 60 |
| **合计** | **360** |

## 构造方法

Claude Code 逐条构造（重复度高的层用程序化组合保证覆盖齐全）→ Alps 逐批人工复核。实体一律取自 v6 §1.3 基准实体表；构造代码见 tests/datasets/nl_catalog.py。

## 判读上下文

| 键 | 值 |
|---|---|
| `baseline_week` | 2026W02 |
| `eval_today` | 2026-01-05 |
| `ruling_missing_week` | 缺周次一律归歧义层，期望动作 ask_clarify（业务方 2026-08-19） |
| `ruling_multi_intent` | 取主意图执行；副意图的周次不进槽位（业务方 2026-08-19） |
| `ruling_typo` | 唯一候选就执行；候选不唯一则反问（业务方 2026-08-19） |
| `week_format` | YYYYWww（与 backend.schemas.intent 的 iso_week 正则一致） |

## 规格依据

- v6 §12.2
- v6 §1.3
- SPEC_DECISIONS §D
- v6 §7.2.1
- v6 §5.4
- v6 §12.5.3

## 已知局限

1. 六类意图中 ingest / export 只有 5 条样本 —— §12.2 的六层分布没有给这两类留独立分层，它们只出现在歧义层与对抗层的多意图样本里。意图分类准确率要按类分别报，这两类的置信区间会很宽。
2. 相对周表述（本周/下周）的判读依赖 context.eval_today=2026-01-05，换参照日会改变期望槽位。
3. 标注口径按 SPEC_DECISIONS §D：Claude Code 初稿 + Alps 人工复核，**不计算也不报告双人标注的 Cohen's Kappa**（v6 §12.7 必述项 2）。
4. 「约束修饰」槽位里 kind=OTHER 的条目共 6 条，它们表达的是冻结档位或目标权重偏好（R3），DSL 中没有对应的 IncrementalConstraint —— 修订翻译准确率统计时要把它们单列，不能算作翻译失败。

## 怎么用

```python
from backend.datasets.loader import load_eval_dataset

manifest, items = load_eval_dataset("nl_360", require_approved=True)
```

加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 `DatasetIntegrityError`。手工改过数据文件之后必须跑 `python -m backend.datasets.cli refresh nl_360`。
