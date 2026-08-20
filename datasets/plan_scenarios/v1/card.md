# Dataset Card · `plan_scenarios` v1

| 项 | 值 |
|---|---|
| 版本 | `v1` |
| 状态 | `approved` —— 业务方已确认，可用于实验 |
| 条数 | **200** |
| 数据文件 | `scenarios.json` |
| SHA256 | `5c3252a45a1d7d56d5d318cf0f90f9433c88d598e67487094516f19f27552f33` |
| 生成时间 | 2026-08-19T19:04:52Z |
| 业务方确认 | Alps · 2026-08-20 |

## 分层分布

| 分层 | 条数 |
|---|---|
| `baseline` | 1 |
| `boundary` | 40 |
| `combo` | 60 |
| `infeasible` | 30 |
| `reschedule` | 9 |
| `single` | 60 |
| **合计** | **200** |

## 构造方法

W4 由 tests/scenarios/catalog.py 程序化生成（实体编号一律从快照读，组合扰动用固定种子 20260812）。M9-A **只做核对与版本化，一条数据都没改**：核对了类别条数、单点扰动是否含跑道关闭、不可行是否为 I1~I5 五族且每族 6 个变体、以及每条不可行是否都标注了真实冲突源。

## 判读上下文

| 键 | 值 |
|---|---|
| `combo_seed` | 20260812 |
| `infeasible_families` | I1~I5，每族 6 个沿同一方向更紧的变体 |
| `snapshot_id` | snap_9724982865ee |
| `week_start` | 2026-01-05 |

## 规格依据

- v6 §12.3
- v6 §3.9
- v6 §1.4

## 已知局限

1. 单点扰动里跑道族只有 2 条 —— 全场就 2 条跑道，这是数据本身的上限，不是覆盖不足。
2. 单点/组合扰动的 expected_status 是 EITHER：**不预设可行与否**，那正是要跑出来的。预设了会诱导「为了对上预期而放宽约束」（CLAUDE.md §7 第 4 条）。
3. 构建记录在 build_manifest.json（快照 id / 种子 / 实体表）；本文件是数据集卡片，两者内容与用途都不同。
4. 边界场景的「恰好」由成对定义互证（恰好够 + 紧一格即不可行），标定过程在 calibration.json。

## 怎么用

```python
from backend.datasets.loader import load_eval_dataset

manifest, items = load_eval_dataset("plan_scenarios", require_approved=True)
```

加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 `DatasetIntegrityError`。手工改过数据文件之后必须跑 `python -m backend.datasets.cli refresh plan_scenarios`。
