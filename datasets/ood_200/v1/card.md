# Dataset Card · `ood_200` v1

| 项 | 值 |
|---|---|
| 版本 | `v1` |
| 状态 | `approved` —— 业务方已确认；仅作历史评测研究留存，不属于交付运行路径 |
| 条数 | **200** |
| 数据文件 | `items.jsonl` |
| SHA256 | `64d3f319ea1849cf87e5ab698ad7ee030fb0146fe80de349ad3f4d2f09a10ba5` |
| 生成时间 | 2026-08-19T19:15:14Z |
| 业务方确认 | Alps · 2026-08-20 |

## 分层分布

| 分层 | 条数 |
|---|---|
| `commonsense` | 40 |
| `instruction` | 40 |
| `language` | 40 |
| `multiturn` | 20 |
| `reasoning` | 40 |
| `refusal` | 20 |
| **合计** | **200** |

## 构造方法

全部自建（历史 §15.5：不使用任何外部数据集）。常识/语言/拒绝/多轮为手写，指令跟随与算术为程序生成（答案由构造过程直接给出）。**全部可程序判定**，五种判据无一依赖 LLM。选择题的正确项位置经程序均衡到 A/B/C/D 各四分之一。

## 判读上下文

| 键 | 值 |
|---|---|
| `judge_impl` | backend/datasets/ood_judge.py（grade / mcnemar_exact / regression_verdict） |
| `layer_warning` | 任一子层下降 >8 个百分点单列警示，不否决 |
| `ruling` | 历史裁定 O-A：确定性判据 + McNemar 配对精确检验 |
| `threshold` | 历史评测阈值：整体准确率绝对下降 ≤3 个百分点且 p ≥ 0.05（两个条件是「且」）；不作为当前交付门禁 |

## 规格依据

- v6 §15（冻结模型与取消训练；本集作为研究留存）
- v6 §12.4.1（与 32B judge 的判定口径边界）

## 已知局限

1. 拒绝层用的是**规则匹配**（命中拒绝标记 ∧ 未命中 forbidden），不如人判得准。历史用途是配对回归；同题、同判定器的系统性偏差会在 McNemar 的配对差分里抵消。**2026-09-06 后 M7 第二阶段取消，本集不再触发该准入。**
2. 「领域外」由 DOMAIN_TERMS 红线保证（18 个领域词一个都不许出现，加载期强制）。但这只挡得住**字面**重合；本集保留作研究复核，不得被写成当前交付模型的回归实测。
3. 200 条的量级决定了单个子层只有 20~40 条，子层的置信区间很宽；历史阈值只作研究记录，不作当前交付门禁。
4. 本集**不复用 §12.4.1 的 32B judge**；32B judge 仅用于 Faithfulness 与上下文利用率，不能外借到本集。

## 怎么用

```python
from backend.datasets.loader import load_eval_dataset

manifest, items = load_eval_dataset("ood_200", require_approved=True)
```

加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 `DatasetIntegrityError`。本卡不授权改动数据文件；若未来仅为研究复核而获准改动，才运行 `python -m backend.datasets.cli refresh ood_200`。
