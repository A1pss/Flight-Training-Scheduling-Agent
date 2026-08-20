# Dataset Card · `judge_calib_50` v1

| 项 | 值 |
|---|---|
| 版本 | `v1` |
| 状态 | `draft` —— 全量已生成、待业务方复核 —— **不得用于实验** |
| 条数 | **50** |
| 数据文件 | `items.jsonl` |
| SHA256 | `bba13a37189c742d7b591724ea01e284e695bb46a158f1b3e2b5d415589ad2ae` |
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

1. **业务方已于 2026-08-20 完成标注**（155 条断言 + 49 条召回条目）。stage 仍保持 draft —— 业务方裁定：**等 W13 真的拿它算过一次一致率、确认标注在实际判定中站得住之后再 approve**。
2. ★ **标签边际极度偏斜**：SUPPORTED 137 / PARTIAL 16 / NOT_SUPPORTED 2，偶然一致率 p_e ≈ 0.792。于是 §12.4.1 的两条门槛一严一松：「一致率 ≥85%」只对应 Kappa ≈ 0.28，而「Kappa ≥0.70」实际要求一致率 ≥93.8%（155 条里最多错 9 条）。**报数时要同时给出一致率、Kappa、以及少数类各自的召回率** ——第三个数才说得清 judge 是「整体不准」还是「只是抓不住少数类」。
3. ⚠️ **重新生成这一集会清空标注**。`write_datasets.py` 里有一道闸挡着（需 `FTS_ALLOW_CALIB_OVERWRITE=1` 显式放行）。按 §12.4.1 第 4 条，**重跑本来就意味着重标** —— 成本是 60 分钟跑批 + 204 行重标，要提前算进排期。
4. 回答是**旧 LLM 预算（每请求 10 次）**下跑出来的，其中若干条是降级形态。judge 一致性验证不受影响（人工与 judge 面对同一批文本），但 §12.4 的**生成层指标**应当在 `Z-34` 的新预算（14）下重跑。
5. 含 0 条**受控故障注入**造的合成负例（真实高风险样本不足 25 条时补足）。报一致率时**真实样本与含注入样本必须分开报** —— 业务方 2026-08-19 确认。
6. 分层用的三条代理信号（召回未命中 / 回答提到未召回的实体 / 逐句核验通过率 <0.8）与「断言有没有被召回支撑」相关但**不等价**；它们的用途是让负例足量，不是给断言打标签。
7. `verifier_supported` 是 M5 逐句核验器的判定，**只作参考不是标签** —— 它判的是「有没有出处」，与 Faithfulness 的「有没有被召回内容支撑」口径不同。
8. 本集**必须由业务方全程人工标注**（§12.4.1 的「一处例外」），不走「Claude Code 初稿 + 复核」。标签非空的条目会被 schema 直接拒绝。

## 怎么用

```python
from backend.datasets.loader import load_eval_dataset

manifest, items = load_eval_dataset("judge_calib_50", require_approved=True)
```

加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 `DatasetIntegrityError`。手工改过数据文件之后必须跑 `python -m backend.datasets.cli refresh judge_calib_50`。
