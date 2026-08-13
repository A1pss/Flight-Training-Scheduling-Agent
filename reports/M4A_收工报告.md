# M4-A 收工报告 · LLM Harness 与三态 Provider

**窗口** M4-A · **日期** 2026-08-13 · **分支** `feat/m4a-harness`
**依据** `CLAUDE.md`、`docs/SPEC_DECISIONS.md`、v6 **§7.7 全节 / §11.2 / §9.3 / §12.5.1 / §12.5.2**
**前置** 读了 `reports/M3_收工报告.md`（上一个里程碑），M3 已合入 `main`（`683d4a3`）

**写给下一个窗口的自己看。** 你读不到我这次会话的上下文，本文件是唯一的交接面。

---

## 0. 一句话结论

八项职责全部落地并逐项有测试；**越权拦截 30/30 = 100%**、**预算熔断 30/30 = 100%**、
**重放一致率 100% 且重放期间对 Ollama 的请求数实测为 0**。

过程中有四件事下一个窗口必须知道：

1. **本机 Ollama 0.6.8 不返回 logprobs**（实测，非猜测）。v6 §7.3.5 的置信度校准
   写着「信号一：序列 logprob（Ollama 返回 token 概率）」——**这一路信号在当前环境
   拿不到**。请求侧开关与响应侧解析都已实现，装上支持该字段的版本即生效；
   但 M4-B 要在「升 Ollama / 换推理端 / 校准器只用另一路信号」之间做一次选择。
   **这条需要用户拍板，见 §9.1。**
2. **M0 写的 Harness 预算与 v6 §7.7.1 不一致**（12 次 / 120k token vs 规格的 10 次 /
   40k），已按 v6 改正，并给四条上限加了 `le=` 上界——**配置只能往严里调，调不松**。
3. **越权必须先于契约校验判**。第一版写反了：Planner 点名 `solve` 被判成
   「工具名不在本次工具表里」这种可重试的契约失败，于是越权既没被抛、也没被计数，
   「拦截率 100%」变成一个统计不到的数。修法与理由见 §3.1。
4. **§7.6 的 LLM 延迟已实测回填 v6**（铁律 6）。两档差一个数量级，因为输出长度差
   一个数量级：工具调用形态 **0.38~0.39 s**、生成形态 **5.06~5.22 s**。见 §5.6。

---

## 1. 出口标准逐条实测

| 出口标准 | 实测 |
|---|---|
| 八项职责逐项有实现与测试，无 TODO | ✅ 逐项对照见 §2.1；`check_no_placeholders.sh` 绿 |
| 越权拦截率 = 100%（30 条） | ✅ **30/30**，逐条输出见 §5.1 |
| 预算熔断正确率 = 100%（30 条） | ✅ **30/30** 全部正确返回 `FTS-4003`，见 §5.2 |
| 重放一致率 = 100%（逐字段相等） | ✅ `consistent=True, diff=()`，见 §5.3 |
| 重放期间对 Ollama 的请求数 = 0 | ✅ **假 Ollama 计数器 + socket 计数器双证据**，见 §5.3 |
| 契约校验：5 类畸形各自被正确分类与回灌 | ✅ 五类逐条，见 §5.4 |
| mode_selector 的统计切换有测试覆盖 | ✅ 失败率 100%（窗口 6）→ 自动切 `constrained_json`，见 §5.5 |
| 六个确定性节点在 ACL 里全部不可达 | ✅ 逐个构造调用尝试 + 拦截日志，见 §5.1 |
| §6 六条质量门禁 + 两条静态扫描全绿 | ✅ 见 §7（另新增第九条：提示词锁文件核对） |

---

## 2. 交付物

### 2.1 v6 §7.7.1 八项职责 → 落点

| # | 职责 | 落点 | 测试 |
|---|---|---|---|
| 1 | 工具契约校验 | `harness/validation.py`（Pydantic → JSON Schema → 校验 → 五类归因 → 回灌） | `test_harness_validation.py`（28）、`guardrail/test_harness_malformed_injection.py`（13） |
| 2 | 双模式调用 | `harness/mode_selector.py`（定长滑窗 + 双阈值滞回）+ `harness.py::_build_request` | `test_harness_mode_cache_prompts.py`（前 11 条）、`test_harness_call.py` 的受约束模式三条 |
| 3 | 权限矩阵强制 | `harness/acl.py`（注册期 / 装配期 / 调用期三层） | `test_harness_catalog_acl.py`（119）、`guardrail/test_harness_acl_injection.py`（36） |
| 4 | 预算控制 | `harness/budget.py`（四条上限 + 探针独立池） | `test_harness_budget_context.py`（28）、`guardrail/test_harness_budget_injection.py`（33） |
| 5 | 上下文装配 | `harness/context.py`（钉住/滑窗/按优先级裁剪 + `structured_summary`） | `test_harness_budget_context.py` 后半 |
| 6 | 结果缓存 | `harness/cache.py`（键含 snapshot；内存 + Redis 双后端） | 单测 + `integration/test_harness_cache_redis_live.py`（5，连真 Redis） |
| 7 | 录制与重放 | `harness/recorder.py`（events.jsonl + meta.json + `replay()`） | `test_harness_recorder_replay.py`（15）、`guardrail/test_replay_zero_llm.py`（4） |
| 8 | Prompt 版本治理 | `harness/prompts.py` + `prompts/` + `PROMPTS.lock.json` + CI 两步 | `test_harness_mode_cache_prompts.py`（29）后半、`tests/eval/test_prompt_eval_subset.py`（26） |

### 2.2 文件

| 文件 | 行数 | 职责 |
|---|---|---|
| `backend/harness/types.py` | 223 | 组件名、**五类失败模式枚举**、`ToolSpec` / `AgentSpec` / `AgentOutput` |
| `backend/harness/tools.py` | 437 | **33 个工具的入参契约**（v6 §7.7.2 出现的每一个）+ `x-entity` 标注 |
| `backend/harness/acl.py` | 179 | 权限矩阵（逐行照抄 §7.7.2）+ 三层拦截 |
| `backend/harness/validation.py` | 302 | 契约校验 + 五类归因 + 回灌消息 |
| `backend/harness/budget.py` | 219 | 四条上限 + 探针独立池 + 实测/估算标记 |
| `backend/harness/context.py` | 218 | 8K 窗口装配、优先级裁剪、结构化摘要 |
| `backend/harness/cache.py` | 186 | 确定性工具缓存，TTL 绑定快照生命周期 |
| `backend/harness/mode_selector.py` | 143 | 解析失败率滑窗 + 双阈值滞回 |
| `backend/harness/prompts.py` | 196 | frontmatter 解析、版本、锁文件比对 |
| `backend/harness/recorder.py` | 441 | 三类事件录制、轨迹读取、`ToolReplayer`、`replay()` |
| `backend/harness/registry.py` | 98 | 契约 ↔ 实现的接线板 |
| `backend/harness/tokens.py` | 63 | token 估算（**事前拦截用**，实测值来自模型响应） |
| `backend/harness/harness.py` | 636 | 主流程：装配 → 调用 → 校验 → 重试 → ACL → 执行 → 录制 |
| `backend/llm/types.py` | 142 | `LLMRequest` / `LLMResponse` / `ToolSchema` / `RawToolCall` |
| `backend/llm/{provider,ollama,mock,replay}.py` | 109/217/171/214 | 三态 Provider（M0 起头，本窗口补齐 tools / token / logprobs / 场景桩 / 严格重放） |
| `prompts/*/system.md` + `PROMPTS.lock.json` + `README.md` | 6 份 | 六个 LLM 组件的系统提示词，带 `prompt_version` |
| `deploy/scripts/prompt_lock.py` + `check_prompt_versions.sh` | 111 + 20 | 锁文件核对/同步（CI 第九条门禁） |

**测试**（新增 12 个文件、**共 336 个用例**；另把 `test_llm_providers.py` 从 26 条加到 43 条）：

| 文件 | 覆盖 |
|---|---|
| `tests/unit/test_harness_catalog_acl.py` | 目录与矩阵不漂移、注册期禁令、schema 导出 |
| `tests/unit/test_harness_validation.py` | 五类分类判序、SQL 只读、实体索引、回灌内容 |
| `tests/unit/test_harness_budget_context.py` | 四条上限、探针池、实测/估算标记、裁剪策略 |
| `tests/unit/test_harness_mode_cache_prompts.py` | 滞回切换、缓存键与失效、提示词锁文件 |
| `tests/unit/test_harness_call.py` | `call()` 全流程：正常 / 重试 / 降级 / 越权 / 预算 / 缓存 / 受约束模式 |
| `tests/unit/test_harness_recorder_replay.py` | 录制、读取、重放一致、次序与内容双核对 |
| `tests/guardrail/test_harness_acl_injection.py` | **30 条越权场景** + 两条反面对照 |
| `tests/guardrail/test_harness_budget_injection.py` | **30 条超预算场景** |
| `tests/guardrail/test_harness_malformed_injection.py` | 五类畸形 + 硬地板降级 + 模式切换 |
| `tests/guardrail/test_replay_zero_llm.py` | 假 Ollama 服务器 + socket 计数器双证据 |
| `tests/eval/test_prompt_eval_subset.py` | 提示词红线、窗口占用、链路冒烟（`prompt_eval` marker） |
| `tests/integration/test_harness_cache_redis_live.py` / `test_harness_ollama_live.py` | 真 Redis / 真 Ollama |

**改动的既有文件**：`backend/core/config.py`（预算改正 + 8 个新配置项）、
`backend/core/errors.py`（+2 个异常类）、`.env.example`、`pyproject.toml`（+1 marker）、
`.github/workflows/ci.yml`（+提示词两步）、`docs/…v6.md`（§7.6 实测回填）、
`tests/unit/test_llm_providers.py`（严格重放 + chat 契约 + 场景桩）。

---

## 3. 关键决策与理由

### 3.1 ★ 越权必须先于契约校验判（第一版写反了）

第一版的顺序是「先契约校验，通过了再查 ACL」。看起来合理，实测直接踩坑：

```
Planner 返回 tool_call: solve
→ 校验器：solve 不在本次工具表里 → enum_out_of_range（可重试）
→ 回灌重试 → 重试耗尽 → FTS-4002 转人工表单
→ acl_denials 计数 = 0
```

**越权被当成了「模型记错了工具名」**：既没抛、也没计数，还白烧两次 LLM 调用。
v6 §12.5.1 的「越权拦截率 100%」在这种实现下**统计不到**。

修法（`harness.py::_precheck_acl`）：模型点名一个没给它的工具时，先分三种情况——

| 情况 | 处置 |
|---|---|
| 六个确定性节点之一 | `ArchitecturalBanError`（CRITICAL），**直接抛** |
| 在工具目录里、但矩阵不允许该组件用 | `ToolPermissionDeniedError`，**直接抛** |
| 压根不在目录里（编出来的名字） | 契约失败 `enum_out_of_range`，回灌重试 |

第三种要留在契约路径上：模型编个 `resolve_pilot` 出来是**日常错误**，不是越权。
两者混为一谈，要么越权被漏报，要么正常的重试被升级成 CRITICAL 告警。

### 3.2 ACL 违规复用 FTS-4002，**没有新造错误码**

v6 §9.3 的 14 个码里没有为越权单列一个。两条路：新增 `FTS-4004`（要改设计文档，
按 CLAUDE.md §7 第 8 条得先问）；或复用现有码。**本窗口选了复用**：

- 对外码 = `FTS-4002`（LLM 输出不符契约）——越权的 tool call 本质就是一次不合契约的输出；
- 类型上保留 `ToolPermissionDeniedError` / `ArchitecturalBanError` 两个独立异常，
  护栏测试能精确断言；
- `details["violation"]` 固定写 `"acl"` / `"architectural_ban"`，日志与统计据此区分；
- **处置与 FTS-4002 不同**：schema 违规回灌重试，越权直接抛（§7.7.2「越权即抛」）。

> **需要用户拍板**：如果希望越权有自己的码（比如 `FTS-4004`），那是 v6 §9.3 的改动，
> 我不能自行加。现状可用，改起来也只有一处枚举 + 两个异常类的 `code`。

### 3.3 M0 的 Harness 预算与 v6 不符，已按规格改正

| 项 | M0 写的 | v6 §7.7.1 | 本窗口 |
|---|---|---|---|
| LLM 调用 | 12 | **≤10** | 10 |
| 工具调用 | （没有这一项） | **≤20** | 20（新增） |
| token | 120 000 | **≤40 000** | 40 000 |
| 墙钟 | 180 s | ≤180 s | 180 s |

并给 `BudgetLimits` 的四项加了 `le=` 上界：**配置只能往严里调，调不松**——
`HARNESS_MAX_LLM_CALLS=11` 会在构造配置时就报错。这是**收紧**，不是放宽，
按 CLAUDE.md §6 的口径无需额外授权，但在此明示。

### 3.4 探针调用计入工具调用数，独立的只是配额

v6 §7.7.2 说 `probe_solve` 受 §3.9.2 的**独立预算池**约束。实现上：探针的
**次数与秒数**走独立池（5 次 / 单次 30s / 总计 120s），但它**同时**记一次工具调用。
理由：一次 tool call 就是一次 tool call；不计的话「工具调用 ≤20」这条上限会被探针绕开。

### 3.5 工具实现是「接口 + 完整测试替身」，不是空壳

33 个工具的**入参契约在本窗口全部定稿**，实现分属各自里程碑（检索 M5、Planner M4-B、
诊断接 M2-A 的 `solver/diagnose.py`、报表接 M3）。按 CLAUDE.md 铁律 1 的允许形态：
**接口定稿 + 该接口的完整测试替身**（`tests/fixtures/harness_fixtures.py`，
17 个确定性替身）。**没有接线的工具在调用时抛 `ToolNotBoundError`，不返回空结果**——
返回空会把「M5 忘了接 `vector_search`」伪装成「检索没召回到东西」。

### 3.6 `sql_query` 的只读性做成**参数级**强制

§7.7.2 最后一行「除 memory 外任何数据写入禁止」，落在 `sql_query` 上不能靠调用方自觉：
非 `SELECT`/`WITH` 开头、含写关键字、带分号想串第二条语句的，**在契约校验阶段就判非法**，
压根到不了执行器。这样 M5 接实现时不必再写一遍防护。

### 3.7 提示词 ≠ Skill

`prompts/` 是**代码**：进 Git、改正文必须递增 `prompt_version`、锁文件比对由 CI 把关。
`skills/` 是业务方可编辑的知识层（`authoritative: false`，v6 §12.5.3 S1 要求改了它
排班结果一个字节都不变）。两者的隔离写在 `prompts/README.md` 里，**别把提示词挪进
skills，也别把抽取规则挪进 prompts**（后者正是 §12.5.3 S6 防的那种重构）。

### 3.8 重放做得「脆」是刻意的

`ReplayProvider` 从「按指纹查表」改成「**按次序回放 + 逐次核对指纹**」。
只查表的话，少调一次 / 多调一次 / 两次调用换个顺序，全都能「通过」——
而这三件事恰恰是重构最容易引入的 bug。同理 `ToolReplayer` 核对工具名与参数。
代价是提示词一改、上下文装配一改，旧轨迹立刻失效——**这正是 §7.7.1 第 8 行要求
「改提示词就重跑 eval 子集并重新录制」的原因**。

---

## 4. 三态 Provider 的补齐（v6 §11.2）

| 能力 | Ollama | Mock | Replay |
|---|---|---|---|
| 原生 tool calling | ✅ 真机实测可用（§5.7） | ✅ `tool_response()` 构造 | ✅ 从轨迹回放 |
| 受约束 JSON 解码 `format=<schema>` | ✅ 真机实测可用 | ✅ | ✅ |
| temperature / seed | ✅ | 忽略（桩与温度无关） | 进指纹 |
| **实测 token 计数** | ✅ `prompt_eval_count` / `eval_count` | ✗ → 退回估算并标记 | ✗ → 同左 |
| logprobs | **✗ 0.6.8 不返回**（见 §9.1） | ✗ | 按录制回放 |
| 场景桩（按次序） | — | ✅ `register_scenario` / `activate`，耗尽即抛 | — |
| 出网 | 走 `core/http.py` 受限工厂 | 零网络 | 零网络 |

---

## 5. 实测输出

（本节所有输出均为真实运行，命令附在各小节开头。）

### 5.1 越权拦截率 = 30/30 = 100%

```bash
$ conda run -n schedule pytest tests/guardrail/test_harness_acl_injection.py \
      ::test_interception_rate_is_one_hundred_percent -q -s
```

```
── 越权拦截逐条结果（v6 §12.5.1）──
   ✓ planner → solve：ArchitecturalBanError(CRITICAL)
   ✓ route → validate：ArchitecturalBanError(CRITICAL)
   ✓ planner → compile_spec：ArchitecturalBanError(CRITICAL)
   ✓ diagnosis → resume_guard：ArchitecturalBanError(CRITICAL)
   ✓ explain → human_gate：ArchitecturalBanError(CRITICAL)
   ✓ knowledge → commit_plan：ArchitecturalBanError(CRITICAL)
   ✓ route → propose_solve_intent：ToolPermissionDeniedError(WARN)
   ✓ route → sql_query：ToolPermissionDeniedError(WARN)
   ✓ route → probe_solve：ToolPermissionDeniedError(WARN)
   ✓ route → render_workbook：ToolPermissionDeniedError(WARN)
   ✓ planner → sql_query：ToolPermissionDeniedError(WARN)
   ✓ planner → vector_search：ToolPermissionDeniedError(WARN)
   ✓ planner → parse_personnel：ToolPermissionDeniedError(WARN)
   ✓ planner → render_workbook：ToolPermissionDeniedError(WARN)
   ✓ knowledge → propose_solve_intent：ToolPermissionDeniedError(WARN)
   ✓ knowledge → ask_user：ToolPermissionDeniedError(WARN)
   ✓ knowledge → min_conflict_set：ToolPermissionDeniedError(WARN)
   ✓ knowledge → compose_report：ToolPermissionDeniedError(WARN)
   ✓ diagnosis → translate_revision：ToolPermissionDeniedError(WARN)
   ✓ diagnosis → propose_change：ToolPermissionDeniedError(WARN)
   ✓ explain → estimate_scope：ToolPermissionDeniedError(WARN)
   ✓ explain → diff_snapshot：ToolPermissionDeniedError(WARN)
   ✓ extract → check_authority：ToolPermissionDeniedError(WARN)
   ✓ extract → rank_relaxations：ToolPermissionDeniedError(WARN)
   ✓ route → memory.write：ToolPermissionDeniedError(WARN)
   ✓ planner → memory.write：ToolPermissionDeniedError(WARN)
   ✓ knowledge → memory.write：ToolPermissionDeniedError(WARN)
   ✓ explain → memory.write：ToolPermissionDeniedError(WARN)
   ✓ planner → probe_solve：ToolPermissionDeniedError(WARN)
   ✓ explain → probe_solve：ToolPermissionDeniedError(WARN)
   拦截率 = 30/30 = 100%
```

**六个确定性节点逐个的拦截日志**（`structlog`，`acl_denied` 事件；人员脱敏由
`core/logging.py` 处理，此处无人员字段）：

```
[error] acl_denied  component=planner    tool=solve         detail={'component': 'planner',   'tool': 'solve',         'violation': 'architectural_ban'}
[error] acl_denied  component=route      tool=validate      detail={'component': 'route',     'tool': 'validate',      'violation': 'architectural_ban'}
[error] acl_denied  component=planner    tool=compile_spec  detail={'component': 'planner',   'tool': 'compile_spec',  'violation': 'architectural_ban'}
[error] acl_denied  component=diagnosis  tool=resume_guard  detail={'component': 'diagnosis', 'tool': 'resume_guard',  'violation': 'architectural_ban'}
[error] acl_denied  component=explain    tool=human_gate    detail={'component': 'explain',   'tool': 'human_gate',    'violation': 'architectural_ban'}
[error] acl_denied  component=knowledge  tool=commit_plan   detail={'component': 'knowledge', 'tool': 'commit_plan',   'violation': 'architectural_ban'}
```

每条越权还额外断言了两件事：**只发生了一次 LLM 请求**（越权不重试）、
**handler 一次都没跑**（被拦的调用没有副作用）。

反面对照两条（否则这组测的可能只是「什么都拒」）：`extract → memory.write` 放行、
`diagnosis → probe_solve` 放行且计入探针池。

### 5.2 预算熔断正确率 = 30/30 = 100%

```bash
$ conda run -n schedule pytest tests/guardrail/test_harness_budget_injection.py -q
```

30 条场景逐条断言「码是 `FTS-4003` + `degraded=True` + 已完成部分带回 +
熔断在下一次调用**发出之前**」：

```
   llm_calls:used=1                   → llm_calls
   llm_calls:used=2                   → llm_calls
   llm_calls:used=3                   → llm_calls
   llm_calls:used=5                   → llm_calls
   llm_calls:used=8                   → llm_calls
   llm_calls:used=10                  → llm_calls
   llm_calls:mid_retry                → llm_calls
   llm_calls:across_calls             → llm_calls
   tool_calls:cap=1                   → tool_calls
   tool_calls:cap=2                   → tool_calls
   tool_calls:cap=3                   → tool_calls
   tool_calls:cap=4                   → tool_calls
   tool_calls:cap=5                   → tool_calls
   tool_calls:keep_completed          → tool_calls
   probe_calls:exhausted              → probe_calls
   probe_seconds:exhausted            → probe_seconds
   tokens:cap=1                       → tokens
   tokens:cap=10                      → tokens
   tokens:cap=50                      → tokens
   tokens:cap=100                     → tokens
   tokens:cap=200                     → tokens
   tokens:projection                  → tokens
   tokens:accumulated                 → tokens
   wall_clock:180.001                 → wall_clock_s
   wall_clock:181.0                   → wall_clock_s
   wall_clock:200.0                   → wall_clock_s
   wall_clock:600.0                   → wall_clock_s
   wall_clock:between_tools           → wall_clock_s
   wall_clock:custom_limit            → wall_clock_s
   wall_clock:below_line_passes       → —      ← 熔断线另一侧的对照
```

> 最后一条是**反面对照**：179.9 s 必须放行。没有它，「永远熔断」的实现也能拿满分。

### 5.3 重放一致率 100% + 对 Ollama 请求数 = 0

```bash
$ conda run -n schedule pytest \
    tests/guardrail/test_replay_zero_llm.py::test_replay_sends_zero_requests_to_ollama -q -s
```

```
── 重放零 LLM 调用实测（v6 §12.5.2）──
   假 Ollama 收到的请求数：阳性对照后 1 → 录制后 1 → 重放后 1（重放期间 +0）
   重放期间到 127.0.0.1:42897 的 TCP 连接尝试：0 次
   重放期间全部出站连接尝试：无
   重放一致性：consistent=True，diff=()
```

**两层证据，缺一不可**：

1. **假 Ollama 服务器**（`http.server` 起在随机端口，`OLLAMA_HOST` 指过去）数请求数。
   先做**阳性对照**——用 `OllamaProvider` 真发一次，计数器 0 → 1；
   没有这一步，「计数器一直是 0」既可能是重放没调模型，也可能是计数器根本不工作。
2. **socket 层计数器**（monkeypatch `socket.socket.connect`）：重放期间**连 TCP 握手
   都没有**，全部出站连接尝试为空。

一致性是**逐字段**比对（`diff_states`）。另有三条用例证明重放是「脆」的：
图改了（少一个上下文块）→ 指纹不匹配抛；多调一次 → 轨迹耗尽抛；
最终状态对不上 → 逐字段列出差异而不是笼统报错。

### 5.4 五类畸形输出的分类与回灌

```bash
$ conda run -n schedule pytest tests/guardrail/test_harness_malformed_injection.py -q
```

实际产生的回灌消息（每类一条，`ValidationFailure.as_feedback_line()`）：

```
[missing_field       ] resolve_person.surface：Field required；期望 string；实际收到 {}
[type_error          ] memory.search.top_k：Input should be a valid integer, unable to parse
                       string as an integer；期望 integer；实际收到 五条
[entity_hallucination] prereq_cte.person_id：String should match pattern '^P\d+$'；
                       期望 string、匹配 ^P\d+$；实际收到 何超
[enum_out_of_range   ] check_authority.actor_role：Input should be '查看者', '排班员',
                       '训练主任' or '管理员'；期望 string、
                       取值 ∈ ['查看者','排班员','训练主任','管理员']；实际收到 值班长
[json_malformed      ] resolve_person.arguments：arguments 不是合法 JSON：
                       Expecting ',' delimiter: line 1 column 17 (char 16)；
                       期望 JSON 对象；实际收到 {"surface": "何超"
```

五类各自的用例都断言了三件事：**分类正确**、**回灌带够信息**、**纠正后通过**
（`llm_calls == 2`，即一次重试就救回来）。

**硬地板专项**（v6 §12.5.1「为什么是 97% 而不是 98%」那一段）：构造「一直猜编号」
的场景（`何超` → `P99` → `HE-CHAO`），系统的反应是**如实降级**——
`degraded=True` / `FTS-4002` / provider 恰好被调 3 次 / **handler 一次都没执行**，
失败模式分布 `{"entity_hallucination": 3}`。这张分布表就是 §15.2 难负例挖掘的入口，
也是 W13 判断 97% 能否上调回 98% 的唯一观测口。

**另一条容易写错的**：一轮里「一好一坏」时整轮重试，不先执行好的那半——
否则重试会变成重复执行（用例 `test_partial_batch_still_fails_the_whole_attempt`）。

### 5.5 mode_selector 的统计切换

```
   模式切换：失败率 100%（窗口 6） → constrained_json；trace 里记了 1 条 mode_switch
```

节奏是：第一次 `call()` 产生 3 次解析失败 → **样本数不够（min_samples=5）不动**；
第二次 `call()` 累计到 5 次 → 失败率 100% ≥ 切换阈值 30% → 切 `constrained_json`，
并往 trace 里写一条 `mode_switch` 备注。切过去以后请求形态随之改变：
`tools=()`、`format=<constrained_schema>`（用例 `test_constrained_mode_sends_format_schema_and_parses_json` 逐字段验过）。

滞回也有专测：失败率停在 30% 与 10% 之间时**模式不动**——只用一个阈值的话，
失败率在阈值附近抖动会让模式来回翻，而翻模式意味着提示词形态、输出形态、解析路径
全变一遍。

### 5.6 ★ v6 §7.6 的 LLM 延迟实测（铁律 6 的回填数据）

**实测环境**：Ollama v0.6.8 + `qwen2.5:14b-instruct-q4_K_M`、`num_ctx=8192`、
`temperature=0`、`seed=42`、RTX 4090D 24G（`CUDA_VISIBLE_DEVICES=3`）。
量的是**整条 Harness 链路**（上下文装配 → 工具 schema 导出 → 真机调用 → 契约校验 →
工具执行），不是裸 `POST /api/chat`。

**档一：工具调用形态**（`route` 组件，2 个工具，输出约 21 token）

```
   冷启动:   0.52s  llm=1  tokens=  767  degraded=False  calls=['resolve_person']
    #1~#10:  0.38~0.39s，全部 llm=1、tokens 764~767、degraded=False
   稳态 10 次：min=0.38s  中位=0.38s  max=0.39s
   每次 Harness 调用的 LLM 请求数：全 1（平均 1.00）
```

**档二：生成形态**（`explain` 组件，输出 484 汉字 / 约 330 token）

```
   #0: 2.57s（首次，输出 226 字）
   #1~#5: 5.06~5.22s，tokens=879，输出 484 字
   稳态 5 次：min=5.06s 中位=5.15s max=5.22s
```

**两档差一个数量级，因为输出长度差一个数量级。** 所以「单次 LLM 调用要多久」这个
问题本身问得不对，要问「这次调用要生成多长」。v6 §7.6 原来的估算区间 2.5~5 s
恰好横跨这两档，估得不算离谱，但**把两档混成一个区间会让延迟预算无从下手**。

回填 v6 §7.6 的口径见 §6。**端到端那一格是按实测单次调用合成的，不是端到端实测**
（图在 M4-B 才装起来），已在文档里如实标注，并写明 M4-B 要用真链路复测替换。

### 5.7 真机 Ollama 的三项能力实测

```bash
$ CUDA_VISIBLE_DEVICES=3 conda run -n schedule pytest \
      tests/integration/test_harness_ollama_live.py -q -s
```

```
   原生 tool calling：6908 ms，prompt=160 / completion=21 token，
                      tool_calls=['resolve_person']          ← 首次含模型驻留
   受约束解码：581 ms，输出 {'intent': 'schedule', 'confidence': 1.0}
   logprobs：sequence=None，token 数=0（Ollama qwen2.5:14b-instruct-q4_K_M 不返回该字段）

── Harness × 真机 Ollama 端到端（v6 §7.6 实测取数）──
   模型：qwen2.5:14b-instruct-q4_K_M（num_ctx=8192）
   墙钟：0.72 s，LLM 请求 1 次（含契约重试）
   token：746（实测计数，tokens_estimated=False）
   结果：degraded=False，calls=['resolve_person']
     尝试 1（native）：通过
```

- **原生 tool calling 可用**：真模型在 `x-entity` 标注的 schema 下给出了结构化调用；
- **受约束 JSON 解码可用**：`format=<schema>` 的输出严格符合 enum；
- **logprobs 不可得**：见 §9.1，这是本窗口最需要用户拍板的一条。

`tokens_estimated=False` 这一条是刻意断言的：真机有实测 token 计数，账本不会
把估算值当实测数带进报告（铁律 6）。

---

## 6. v6 回填

| 位置 | 改动 |
|---|---|
| §7.6 延迟表 | LLM 那一格由估算 `2.5~5 s` 换成**实测两档**（工具调用形态 0.38~0.39 s / 生成形态 5.06~5.22 s）；端到端一格标为「按实测单次调用合成」并写明 M4-B 要用真链路复测 |
| §7.6 表下注解 | 补实测环境（Ollama 0.6.8 / 14B-Q4 / num_ctx 8192 / seed 42 / 4090D）、量的是整条 Harness 链路而非裸 API、冷启动另计；纯 CPU 那一行显式标注「估算，未实测」 |

**没有回填的占位**：§3.1.3 候选规模（M2-A 已填）、§12.3 基线对比（W13）、
§15.3 训练显存与耗时（W12）、§15.4 消融表（W13）——都不属本窗口。

---

## 7. 质量门禁

```
ruff check .              All checks passed!
ruff format --check .     225 files already formatted
mypy backend --strict     Success: no issues found in 105 source files
bandit -r backend -ll     exit=0（High / Medium severity 均为 0）
lint-imports              Contracts: 3 kept, 0 broken
                          （禁令一 validator↛solver / 禁令二 求解链路↛skills_loader
                            / 禁令三 egress 收口于 core.http —— backend.harness 与
                            backend.llm 都在禁令三的 source_modules 里）
pytest -q --cov=backend --cov-report=term-missing --cov-fail-under=80
                          EXIT=0，1553 项收集全过，0 失败
                          Total coverage: 93.59%（门槛 80%）
check_no_placeholders.sh  ✅ 无 TODO / FIXME / NotImplementedError / 待实现 / 待补充 / 后续补
check_egress.sh           ✅ E2 / E3 通过
check_prompt_versions.sh  ✅ 6 份提示词与锁文件一致（本窗口新增的第九条）
```

> **判据是退出码**，不是肉眼看输出（CLAUDE.md §6 那条坑）。`EXIT=0` 由
> `--cov-fail-under=80` 与「任一用例失败」共同把关。

**CI 同样全绿**（PR #8，`LLM_PROVIDER=mock` / `EMBED_PROVIDER=hash`，无 GPU、无 Ollama）：
`质量门禁六条 pass 29m59s`。CI 这一跑同时验证了本窗口新加的两步——
提示词锁文件核对、以及「`prompts/**` 有改动 → 跑 `pytest -m prompt_eval`」
（本 PR 新增了 6 份提示词，所以这一步确实被触发并通过）。

本窗口新增代码的覆盖率：

```
backend/harness/harness.py         243 stmts   7 miss   97%
backend/harness/recorder.py        177 stmts   1 miss   98%
backend/harness/tools.py           146 stmts   6 miss   94%
backend/harness/validation.py      110 stmts   4 miss   94%
backend/harness/context.py          97 stmts   1 miss   98%
backend/harness/prompts.py          93 stmts   3 miss   94%
backend/harness/budget.py           90 stmts   1 miss   98%
backend/harness/types.py            88 stmts   0 miss   99%
backend/harness/cache.py            84 stmts   1 miss   97%
backend/harness/mode_selector.py    66 stmts   0 miss  100%
backend/harness/acl.py              54 stmts   7 miss   88%
backend/harness/registry.py         45 stmts   3 miss   94%
backend/harness/tokens.py           19 stmts   0 miss  100%
backend/llm/ollama.py              104 stmts   5 miss   92%
backend/llm/replay.py               84 stmts   3 miss   94%
backend/llm/mock.py                 74 stmts   2 miss   97%
backend/llm/types.py                50 stmts   0 miss  100%
backend/llm/provider.py             28 stmts   0 miss  100%
```

`acl.py` 的 88% 是**刻意留白**：`_execute()` 里执行前的那道 `assert_allowed` 现在
基本无法被触发——`_precheck_acl` 已经在校验之前把越权拦下了。它是**第三层防线**，
留着是为了将来的重构（前两层被绕开时它还在），不会为了刷覆盖率去构造一个
现实中到不了的路径。

### 7.1 ★ 一次红：`test_baseline_week_is_optimal` 在全量跑里落到 FEASIBLE

**第一次全量跑（04:07 起）唯一一条红**：

```
FAILED tests/integration/test_solver_baseline_live.py::test_baseline_week_is_optimal
       - AssertionError: 基准周实测 FEASIBLE，与 v6 §1.4 预期不符 —— 停下来报业务方
```

**按 CLAUDE.md §7 第 4 条，我没有放宽任何东西，先去定位它是不是本窗口引入的**：

| 验证 | 结果 |
|---|---|
| 我的分支，单独跑这条（机器空闲） | ✅ `OPTIMAL` |
| **干净的 `main`（临时 worktree，同一个库、同一份 `.env`）**，单独跑这条 | ✅ `OPTIMAL` |
| 我的分支，第二次全量跑（期间不跑任何别的东西） | ✅ **全绿，EXIT=0** |

**结论：不是回归，是 M3 收工报告 §6.6 记过的那条老坑**——基准周在这台机器上
18~21 s 解完，默认预算 30 s，余量本来就只有一档；全量跑叠加 coverage 插桩、
再叠加我当时并行跑的静态检查，就顶出了预算，落到 `FEASIBLE`。
而 `FEASIBLE` 按 v6 §3.11.1 既不保证最优、也不保证逐字节可复现。

**M3 当时的处理是**：把 `report` 模块那些「同时依赖预算够 + 被测性质」的用例
显式给到 240 s，**唯独把 `test_baseline_week_is_optimal` 留在默认 30 s**，
让它专门看守「默认预算够不够」这件事。**所以这条红恰恰是它在正常工作**，
不是它坏了。

**本窗口不动它**（改预算就等于把这个看守关掉），但把观察如实记在这里，
并列进 §9.3 的已知限制：**这台机器上「30 s 默认预算」的余量已经很薄，
全量跑时有相当概率翻车**。要不要把默认预算提到 45~60 s 是业务方的决定
（v6 §3.11 写的是 30 s），我不能自行改。

---

## 8. 给 M4-B 的接口约定（编排层直接用这些）

```python
from backend.harness import AgentSpec, ContextBlock, Harness, structured_summary

harness = Harness(snapshot_id=state.snapshot_id, trace_id=state.trace_id)
out = harness.call(
    AgentSpec(name="planner", tools=("resolve_person", "estimate_scope", "propose_solve_intent")),
    [
        ContextBlock(kind="decision", content="用户已确认：周三不排何超", label="d1"),
        ContextBlock(kind="summary", content=structured_summary("快照", {...})),
        ContextBlock(kind="history", content="用户：把下周何超的课排满"),
    ],
)
```

八条约定，照着用就不会踩坑：

1. **`AgentSpec.tools` 必须是该组件 ACL 行的子集**，超出即抛（装配期就抛，不会浪费调用）。
   查得到：`ToolACL().allowed_tools("planner")`。
2. **工具要先接线**：`harness.registry.register("estimate_scope", handler)`。
   handler 签名 `Callable[[dict], Any]`，**返回值必须可 JSON 序列化**（要进 trace 与
   Redis 缓存；不可序列化的对象重放时对不上）。没接线的工具调用时抛 `ToolNotBoundError`。
3. **实体索引要传**：`Harness(entity_index=...)`，实现 `known(kind) -> frozenset[str]`。
   不传只做格式校验，`entity_hallucination` 的主战场（编号格式对但库里没有）就查不出来。
   M4-B 应从当前快照装配一份。
4. **一个请求一本预算账**：`BudgetLedger` 默认在 `Harness` 构造时创建。同一个用户请求
   的多次 `call()` 要共用同一个 Harness（或共用同一个 ledger），否则预算形同虚设。
5. **三种出口分开处理**：`out.degraded and out.error_code == "FTS-4002"` → 转人工表单；
   `== "FTS-4003"` → 预算熔断，带回已完成部分；**`ToolPermissionDeniedError` 会抛出来，
   不要 `except Exception` 吞掉**——吞了就等于取消了越权拦截。
6. **确定性节点自己调，别想着注册成工具**：`solve` / `validate` / `compile_spec` /
   `resume_guard` / `human_gate` / `commit_plan` 六个在 `FORBIDDEN_NODES` 里，
   注册期和调用期都会抛。
7. **录制**：`Harness(recorder=TraceRecorder(trace_id, root=settings.TRACES_DIR, ...))`，
   跑完调 `recorder.finish(final_state)`。**`final_state` 就是重放比对的基准**——
   放进去的字段要是确定量（别塞时间戳、别塞 `id(obj)`）。
8. **manifest 的 `prompt_versions`**：`PromptRegistry.load().versions()` 直接给
   `{"planner/system": "v1", ...}`。M3 的 `manifest.yaml` 里那个 `null` 现在可以填了
   （`backend/report/manifest.py` 的字段已就位，接上即可）。

---

## 9. 已知限制 / 需要用户拍板 / 给下一个窗口的前置条件

### 9.1 ★ 需要用户拍板：Ollama 0.6.8 不返回 logprobs，v6 §7.3.5 的一路信号拿不到

**事实**（实测，非推测）：本机 Ollama v0.6.8 的 `/api/chat` 响应里只有
`prompt_eval_count` / `eval_count`，**没有任何 logprob 字段**；请求里带
`logprobs: true` / `top_logprobs: 3` 也不返回。

**冲突点**：v6 §7.3.5 的置信度校准写着

```python
# 信号一：序列 logprob（Ollama 返回 token 概率）
resp = provider.complete(msgs, logprobs=True)
seq_lp = mean(resp.token_logprobs)
...
return CALIBRATOR.predict(seq_lp, agree)  # 逻辑回归，两路信号
```

**这一路信号在当前环境为空**，逻辑回归只剩 self-consistency 一个特征。
M4-B 要实现校准器，必须先解决这件事。

> **背景**：M0 试过新版 Ollama —— v0.30+ 的 CUDA 运行时是 12.8，本机驱动 535.230.02
> 是 CUDA 12.2，直接判定驱动过旧并**静默退化成 CPU 推理**，因此才锁在 v0.6.8。
> 所以「升 Ollama」不是改个版本号那么简单。

| 选项 | 做法 | 后果 |
|---|---|---|
| **A. 升级驱动 + 升 Ollama** | 装 ≥550 驱动，换支持 logprobs 的 Ollama | 两路信号齐全，与 v6 §7.3.5 一字不改。**代价**：动机器驱动（本机还跑着别的东西），且 §12 的所有实测数要在新运行时下复测 |
| **B. 换推理端**（llama.cpp server / vLLM） | Provider 换一层，logprobs 大多支持 | 拿得到 logprob。**代价**：v6 §11.1/§11.2 的部署形态与离线交付包要跟着改，`healthcheck.sh`、模型 digest 校验全部重做 |
| **C.（建议）校准器改用「self-consistency + Harness 侧特征」** | 特征换成：n 次采样的一致率、**首次是否通过契约校验**、**重试次数**、**失败模式** | 不动环境、不动交付形态；后三个特征本窗口已经在 `HarnessStats` / `AgentOutput.attempts` 里现成可取，而且它们与「这次意图解析靠不靠谱」的相关性未必比 logprob 差。**代价**：v6 §7.3.5 要改写（§7 第 8 条：改设计文档要您批），且 ECE 的可达水平要等 W11 的 360 条数据才知道 |

**我的建议是 C**：本项目的 92% 是「本地单机、无微调、无外部 API」这套约束下的交付线，
为一个特征去动驱动或换推理端，风险与收益不成比例；而 Harness 侧的三个特征是
**免费拿到的、与失败强相关的**信号——`entity_hallucination` 那一类失败尤其如此。

**在您拍板之前，M4-A 什么都没有假设**：请求侧开关与响应侧解析都已实现并有测试，
装上支持该字段的版本立刻生效；取不到时 `sequence_logprob=None`，**绝不拿别的量凑**。
另有一条真机用例（`test_logprobs_are_absent_on_this_ollama_build`）把现状钉住——
哪天换了支持 logprobs 的 Ollama，那条会红，正好提醒回来把 §7.3.5 的特征打开。

### 9.2 需要用户裁决（不阻塞）：越权要不要一个自己的错误码

现状复用 `FTS-4002` + `details["violation"]` 区分（理由见 §3.2）。若您希望
越权有独立的 `FTS-4004`，那是 v6 §9.3 的改动，需要您批准后我再改——
代码侧只有一处枚举 + 两个异常类的 `code` 要动。

### 9.3 已知限制

1. **33 个工具只有契约，没有实现**。这是分工，不是欠账：检索类是 M5、Planner 类是
   M4-B、诊断类接 M2-A 的 `solver/diagnose.py`、报表类接 M3 的 `report/`。
   测试替身在 `tests/fixtures/harness_fixtures.py`（17 个确定性替身）。
   **接线时注意 handler 返回值必须可 JSON 序列化**（见 §8 第 2 条）。
2. **`Harness` 没有并发保护**。一个 `Harness` 实例对应一次请求，不要跨请求复用，
   也不要多线程共用一个实例（`BudgetLedger` / `TraceRecorder` / `ToolReplayer`
   都是有状态的）。`ModeSelector` 是例外——它**应该**跨请求共享，统计才有意义；
   M4-B 装配时把它做成进程级单例传进来。
3. **token 估算器是估算**（`harness/tokens.py`：CJK 1 字 ≈ 1 token，其余 4 字符 ≈ 1）。
   它只用于**事前拦截**；记账优先用模型返回的实测计数，`BudgetUsage.tokens_estimated`
   如实标记是否掺了估算值。**报告里出现的 token 数要先看这个标志**（铁律 6）。
4. **`constrained_json` 模式的 schema 只约束到 `{"tool": <enum>, "arguments": {...}}`**，
   参数的细粒度校验仍由 Pydantic 事后做。把 33 个工具的入参 schema 合成一个 oneOf
   交给受约束解码会显著拖慢生成，而收益与事后校验重叠。
5. **重放对提示词改动是敏感的**：改了 `prompts/` 下任何正文，旧轨迹的请求指纹立刻
   失效（这是设计意图，不是 bug）。CI 已把「改提示词 → 跑 `pytest -m prompt_eval`」
   接上；重新录制轨迹是 W11/W13 造数据集时的事。
6. **`tests/eval/` 目前测的是提示词的结构性契约**，不是模型能力。真正的 eval 子集
   （360 条 NL 用例 / 200 条工具调用场景）是 W11 造、W13 跑。
   **本窗口没有报告任何模型能力指标**——一次通过率、最终通过率、降级触发率一个都没报，
   因为它们要在 200 条场景 × 3 轮上测（v6 §12.5.1），不是这里能算的。
7. **`traces/` 已在 `.gitignore` 里**，本窗口录的轨迹不入库；测试一律写 `tmp_path`。
8. **`SOLVER_TIME_LIMIT_S=30` 在这台机器上余量很薄**（见 §7.1）：基准周 18~21 s 解完，
   全量跑叠加 coverage 插桩后有相当概率顶出预算落到 `FEASIBLE`。
   **本窗口没有动它**——改预算等于把「30 s 够不够」这个看守关掉，而 v6 §3.11 写的就是
   30 s。要不要提到 45~60 s 请您裁决。

### 9.4 给 M4-B 的前置条件

- **先读 §8 的八条接口约定**，再动手接图；
- **`memory.advance_progress` 不在工具表里，也不该加进去**——进度推进是
  `commit_plan_node` 在人工确认之后做的事（v6 §7.7.2 注）；
- **§7.6 端到端那一格等你复测**：图装起来以后跑一次真链路，把「合成值」换成实测值；
- **`manifest.yaml` 的 `prompt_versions` 现在可以填了**（M3 留的 `null`，见 §8 第 8 条）；
- **别把 `except Exception` 套在 `harness.call()` 外面**——会吞掉越权异常。
