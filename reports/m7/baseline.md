# M7 第一步 · 工具调用基线实测

数据集 `tool_calls_200` · 模型 `qwen2.5:14b-instruct-q4_K_M`

## 一、三种配置的主指标

| 配置 / 口径 | 调用数 | **一次通过率** | 最终通过率 | 重试系数 | 降级率 | 工具选择 | 参数精确 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `zero_shot / task` | 600 | **100.0%** | 100.0% | 1.000 | 0.0% | 100.0% | 88.0% |
| `production / task` | 600 | **99.5%** | 100.0% | 1.005 | 0.0% | 100.0% | 86.5% |
| `production / context` | 600 | **66.8%** | 92.5% | 1.440 | 7.2% | 69.0% | 0.5% |

## 二、失败模式分布（首次尝试，§15.2 ⑥ 的直接输入）

| 配置 / 口径 | missing_field | type_error | entity_hallucination | enum_out_of_range | json_malformed | 合计 |
|---|---:|---:|---:|---:|---:|---:|
| `zero_shot / task` | 0 | 0 | 0 | 0 | 0 | 0 |
| `production / task` | 0 | 0 | 0 | 0 | 3 | 3 |
| `production / context` | 24 | 9 | 160 | 3 | 25 | 221 |

## 三、确定性两层（越权 / 超预算）

| 配置 / 口径 | 越权拦截率 | 预算熔断正确率 |
|---|---:|---:|
| `zero_shot / task` | 90/90 = 100.0% | 90/90 = 100.0% |
| `production / task` | 90/90 = 100.0% | 90/90 = 100.0% |
| `production / context` | 90/90 = 100.0% | 90/90 = 100.0% |

## 四、一次通过率最低的工具（难负例挑样入口）

**`zero_shot / task`**：

**`production / task`**：
- `rerank` 80.0%

**`production / context`**：
- `blame_chain` 0.0%
- `min_conflict_set` 0.0%
- `prereq_cte` 0.0%
- `probe_solve` 0.0%
- `propose_solve_intent` 0.0%
- `classify_doc` 75.0%
- `sql_query` 78.6%
- `memory.search` 80.0%
- `vector_search` 80.0%
- `ask_user` 88.9%
