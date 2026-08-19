# Dataset Card · `trajectory_100` v1

| 项 | 值 |
|---|---|
| 版本 | `v1` |
| 状态 | `sample` —— 送审样例 —— 仅供业务方复核口径，**不得用于实验** |
| 条数 | **15** |
| 数据文件 | `items.jsonl` |
| SHA256 | `e2f8de46796cde02613ac44a53a668662feca015ec5f0ba4908eb28fefd9b40f` |
| 生成时间 | 2026-08-19T16:31:15Z |

## 分层分布

| 分层 | 条数 |
|---|---|
| `diagnosis` | 4 |
| `ingest` | 1 |
| `query` | 5 |
| `reschedule` | 1 |
| `revision` | 2 |
| `schedule` | 2 |
| **合计** | **15** |

## 构造方法

Claude Code 逐条构造 → Alps 逐批人工复核。路径元素取自真实的图节点（backend/graph/graph.py 的 add_node）与工具目录；每个步骤的工具都经 ACL 矩阵校验，越权的组合根本写不进数据集。

## 判读上下文

| 键 | 值 |
|---|---|
| `acceptable_rules` | A 同层并列顺序 / B 信息足够时省略 / C 自治循环迭代次数 |
| `forbidden_rules` | D 跳过确定性节点 / E 弱工具替代强工具 / F 不调工具直接答 |
| `graph_source` | backend/graph/graph.py 的 add_node/destinations |
| `knowledge_max_steps` | 6（KNOWLEDGE_MAX_STEPS） |
| `probe_budget` | 5 次 / 单次 30s / 累计 120s（与 LLM 预算互不挤占） |

## 规格依据

- v6 §12.6
- v6 §7.5
- v6 §7.7.2
- v6 §3.9.1
- v6 §5.1
- SPEC_DECISIONS §D

## 已知局限

1. 当前为 15 条送审样例，全量 100 条待业务方确认「可接受的替代路径」口径后生成。
2. 路径判定用最长公共子序列相似度（§12.6.2），所以 acceptable_paths 给的是**完整序列**而不是规则描述；三条准入规则（A 顺序 / B 可省 / C 迭代次数）写在构造代码的模块文档里，供人复核，判定器不读它。
3. 摄取流程不在对话图内（走 POST /api/v1/ingest），其路径元素用 ingest.prepare / ingest.gate / ingest.commit 三个阶段名表示，它们不是图节点。
4. 标注口径按 SPEC_DECISIONS §D：Claude Code 初稿 + Alps 人工复核，**不计算也不报告双人标注的 Cohen's Kappa**。

## 怎么用

```python
from backend.datasets.loader import load_eval_dataset

manifest, items = load_eval_dataset("trajectory_100", require_approved=True)
```

加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 `DatasetIntegrityError`。手工改过数据文件之后必须跑 `python -m backend.datasets.cli refresh trajectory_100`。
