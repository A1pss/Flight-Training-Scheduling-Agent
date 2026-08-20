# Dataset Card · `judge_calib_50` v1

| 项 | 值 |
|---|---|
| 版本 | `v1` |
| 状态 | `draft` —— 全量已生成、待业务方复核 —— **不得用于实验** |
| 条数 | **50** |
| 数据文件 | `items.jsonl` |
| SHA256 | `7eaf7d9f61245964f6a4e76e78f49c1f3f0b7b49294607b577893ede431ad9bc` |
| 生成时间 | 2026-08-20T05:05:35Z |

## 分层分布

| 分层 | 条数 |
|---|---|
| `high_risk` | 25 |
| `regular` | 25 |
| **合计** | **50** |

## 构造方法

从 320 条探针的**冻结回答**（answers_v1.jsonl，真 14B / 温度 0 / seed 42 / chroma + bge-m3 + bge-reranker）分层抽 50：高风险 25（三条确定性代理信号挑出，不是 judge 挑的）+ 常规 25（按 10/9/6 分层随机，seed 42）。断言分解优先用 M5 逐句核验器已切好的断言。**Claude Code 只做抽样与分解，标签栏交付时全空。**

## 判读上下文

| 键 | 值 |
|---|---|
| `acceptance_gate` | 一致率 ≥85% 且 Cohen's Kappa ≥0.70（§12.4.1） |
| `answers_file` | answers_v1.jsonl（320 条，与本集同目录） |
| `high_risk_target` | 25 |
| `regular_quota` | 语义 10 / 情景 9 / 程序 6 |
| `sampling_seed` | 42 |

## 规格依据

- v6 §12.4.1
- v6 §12.7 必述项 4
- v6 §6.5.2
- SPEC_DECISIONS §D

## 已知局限

1. 含 0 条**受控故障注入**造的合成负例（真实高风险样本不足 25 条时补足）。报一致率时**真实样本与含注入样本必须分开报** —— 业务方 2026-08-19 确认。
2. 分层用的三条代理信号（召回未命中 / 回答提到未召回的实体 / 逐句核验通过率 <0.8）与「断言有没有被召回支撑」相关但**不等价**；它们的用途是让负例足量，不是给断言打标签。
3. `verifier_supported` 是 M5 逐句核验器的判定，**只作参考不是标签** —— 它判的是「有没有出处」，与 Faithfulness 的「有没有被召回内容支撑」口径不同。
4. 本集**必须由业务方全程人工标注**（§12.4.1 的「一处例外」），不走「Claude Code 初稿 + 复核」。标签非空的条目会被 schema 直接拒绝。
5. 回答是**这一版语料 + 这一版提示词**下跑出来的。提示词版本变了要重跑并重标（§12.4.1 第 4 条：judge 的提示词纳入 prompt_version 治理，改动触发重跑）。

## 怎么用

```python
from backend.datasets.loader import load_eval_dataset

manifest, items = load_eval_dataset("judge_calib_50", require_approved=True)
```

加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 `DatasetIntegrityError`。手工改过数据文件之后必须跑 `python -m backend.datasets.cli refresh judge_calib_50`。
