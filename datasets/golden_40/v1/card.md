# Dataset Card · `golden_40` v1

| 项 | 值 |
|---|---|
| 版本 | `v1` |
| 状态 | `approved` —— 业务方已确认，可用于实验 |
| 条数 | **40** |
| 数据文件 | `items.jsonl` |
| SHA256 | `d0f849eb35a7e63b6c8ddef7230f719c93e38a67e2bdebc854a45428fe6c958b` |
| 生成时间 | 2026-08-19T19:04:52Z |
| 业务方确认 | Alps · 2026-08-20 |

## 分层分布

| 分层 | 条数 |
|---|---|
| `INFEASIBLE` | 2 |
| `OPTIMAL` | 38 |
| **合计** | **40** |

## 构造方法

W4 由 pytest-regressions 落的 40 份基线快照（tests/golden/test_golden_plans/*.yml）。M9-A **只抽索引与指纹，不复制数据本体**：用例名、状态、架次数、候选数、content_sha256、两条校验通道的判定、阻塞项与欠账条数。

## 判读上下文

| 键 | 值 |
|---|---|
| `aggregate_fingerprint` | deploy/scripts/golden_fingerprint.py（两条部署路径的门禁） |
| `baseline_dir` | tests/golden/test_golden_plans/ |
| `m8_fingerprint` | 4dc4df24…f0c0aca（native 与 compose 两条路径同值） |

## 规格依据

- v6 §12.1
- v6 §3.11.1
- v6 §11.4

## 已知局限

1. 40 条里 2 条是 INFEASIBLE（空域关闭、关闭叠跑道）——它们没有方案，因而没有 content_sha256。这与 Z-26 一致：两种状态都确定性可复现，**唯一不许出现的是 FEASIBLE**（被预算截断，不保证逐字节可复现，§3.11.1）。
2. 38 个 OPTIMAL 用例只有 30 个互不相同的指纹 —— 有 8 条与别的用例排出了**逐字节相同**的方案（合成场景规模小，不同旋钮可能落到同一个最优解）。这不影响回归价值：变化仍然会被看见。
3. 本集是**索引**，不是数据本体。更新基线的唯一正确姿势仍是 `pytest tests/golden -q --force-regen` 然后逐行读 diff。

## 怎么用

```python
from backend.datasets.loader import load_eval_dataset

manifest, items = load_eval_dataset("golden_40", require_approved=True)
```

加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 `DatasetIntegrityError`。手工改过数据文件之后必须跑 `python -m backend.datasets.cli refresh golden_40`。
