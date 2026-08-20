# Dataset Card · `sft_seed` v1

| 项 | 值 |
|---|---|
| 版本 | `v1` |
| 状态 | `approved` —— 业务方已确认，可用于实验 |
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
| `pipeline_owner` | W12（M7 微调前的数据合成） |
| `ruleset_version` | 1.3.0 |
| `sampling` | 每层步长 = len(pool) // 20，确定性 |
| `semantics_version` | 1.1.0 |

## 规格依据

- v6 §15.2
- v6 §1.1
- v6 §1.3
- v6 §12.2

## 已知局限

1. **本集只是种子，不是训练样本。** §15.2 的六步合成管线（指令扩写 → 学生自采样 → 确定性过滤 → 教师补硬样本 → 程序化生成 → 难负例挖掘）是 W12 的交付物。
2. 60 条需求表述与 nl_360 同源 —— 用它们合成的样本若拿去评 nl_360，会有**训练/评测同源**的问题。W12 合成时要么换池子、要么在报告里声明。
3. 规则与语义假设跟着 ruleset_version=1.3.0 / semantics_version=1.1.0 走：任一版本变动，本集的 sha256 必变、批准状态自动失效。
4. 难负例（近音近形、歧义、注入）**不在种子里** —— §15.2 把它们放在第 ⑥ 步「难负例挖掘」，输入是 §12.5.1 的失败模式分布表，那要 W13 跑完才有。

## 怎么用

```python
from backend.datasets.loader import load_eval_dataset

manifest, items = load_eval_dataset("sft_seed", require_approved=True)
```

加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 `DatasetIntegrityError`。手工改过数据文件之后必须跑 `python -m backend.datasets.cli refresh sft_seed`。
