# Dataset Card · `tool_calls_200` v1

| 项 | 值 |
|---|---|
| 版本 | `v1` |
| 状态 | `approved` —— 业务方已确认，可用于实验 |
| 条数 | **260** |
| 数据文件 | `items.jsonl` |
| SHA256 | `2944f27208e2cd8037d7457d4f7736175951328bde39996e413bccd021e8422b` |
| 生成时间 | 2026-08-19T18:31:24Z |
| 业务方确认 | Alps · 2026-08-20 |

## 分层分布

| 分层 | 条数 |
|---|---|
| `acl_violation` | 30 |
| `budget_exhaustion` | 30 |
| `valid` | 200 |
| **合计** | **260** |

## 构造方法

由实体表 + 工具 schema **反向构造**，标签天然正确：valid 层的参数由工具自己的 params_model 生成并校验；越权层的 (组件, 工具) 取自 ACL 矩阵的补集；超预算层是预算池设成 0 之后的必然结果。无一处依赖人的判断。

## 判读上下文

| 键 | 值 |
|---|---|
| `acl_source` | backend.harness.acl.ACL_MATRIX 的补集（字典序，可复现） |
| `error_codes` | 越权 FTS-4004 / Harness 预算 FTS-4003 / 探针池无错误码 |
| `floor_per_tool` | 2 |
| `weight_source` | trajectory_100 v1 的 242 个工具步骤频次 |

## 规格依据

- v6 §12.5.1
- v6 §7.7.2
- v6 §3.9.2
- v6 §9.3

## 已知局限

1. 200 条 valid 的权重取自 trajectory_100 的 242 个工具步骤 —— 那是目前唯一一份「工具在真实流程里各出现多少次」的数据。**它是一个可替换的假设**：W13 真实跑过之后应该用线上日志的频次重算，而不是继续用轨迹集的。
2. 每个工具设了 2 条地板，否则频率为 0 的工具（escalate / memory.write / render_workbook 等）一条都分不到，而 §12.5.1 的契约通过率要覆盖全部工具。
3. 越权层里 6 条是**凭空编出来的工具名**（六个确定性节点），它们不在目录里，`tool_exists=False`。这与「有工具但没权限」是两种不同的失败模式。
4. 超预算层的 6 条探针场景 `expected_error_code` 为 None —— 探针池耗尽时不抛错，优雅返回 BUDGET_EXHAUSTED 载荷（§3.9.2）。
5. 本集**不需要逐条人工复核**（标签是算出来的），需要复核的是分布。

## 怎么用

```python
from backend.datasets.loader import load_eval_dataset

manifest, items = load_eval_dataset("tool_calls_200", require_approved=True)
```

加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 `DatasetIntegrityError`。手工改过数据文件之后必须跑 `python -m backend.datasets.cli refresh tool_calls_200`。
