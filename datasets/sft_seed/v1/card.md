# Dataset Card · `sft_seed` v1

| 项 | 值 |
|---|---|
| 版本 | `v1` |
| 状态 | `approved` —— 业务方已确认；**2026-09-06 起仅作研究留存，不用于交付训练或部署** |
| 条数 | **123** |
| 数据文件 | `items.jsonl` |
| SHA256 | `58e8bbfdacb615fb30661d659057d04fc28d3593cad9af6a9a4464cf16669b4e` |
| 生成时间 | 2026-08-19T19:20:25Z |
| 业务方确认 | Alps · 2026-08-20 |

## 分层分布

| 分层 | 条数 |
|---|---|
| `entity` | 36 |
| `request` | 60 |
| `rule` | 14 |
| `semantic` | 13 |
| **合计** | **123** |

## 构造方法

60 条需求表述从 nl_360 的排班/指定/重排三层确定性抽样（每层按固定步长取 20）；14 条规则从 rules/ruleset_v1.3.yaml **读出来**；13 条语义假设从 rules/semantics.yaml 读；36 条实体来自 v6 §1.3 基准实体表。**没有一条是手抄的** —— 手抄会在下一次改规则时悄悄分叉。

## 判读上下文

| 键 | 值 |
|---|---|
| `pipeline_owner` | 历史 W12（M7 第一阶段之后的研究留存） |
| `ruleset_version` | 1.3.0 |
| `sampling` | 每层步长 = len(pool) // 20，确定性 |
| `semantics_version` | 1.1.0 |

## 规格依据

- v6 §15（冻结模型与取消训练；本集作为研究留存）
- v6 §1.1
- v6 §1.3
- v6 §12.2

## 已知局限

1. **本集只是历史种子数据，不属于交付运行路径。**
2. 60 条需求表述与 nl_360 同源 —— 这是历史研究复核时必须注意的**数据同源**风险；本集不进入当前交付运行路径。
3. 规则与语义假设跟着 ruleset_version=1.3.0 / semantics_version=1.1.0 走：任一版本变动，本集的 sha256 必变、批准状态自动失效。
4. 难负例（近音近形、歧义、注入）**不在种子里**。失败模式分布仅用于定位实现问题。

## 怎么用

```python
from backend.datasets.loader import load_eval_dataset

manifest, items = load_eval_dataset("sft_seed", require_approved=True)
```

加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 `DatasetIntegrityError`。本卡不授权改动数据文件；若未来仅为研究复核而获准改动，才运行 `python -m backend.datasets.cli refresh sft_seed`。
