# M2-A 收工报告 · CP-SAT 求解内核

**窗口** M2-A（隔离窗口）· **日期** 2026-08-10/11 · **分支** `feat/m2a-solver`
**写给下一个窗口的自己看。** 你读不到我这次会话的上下文，本文件是唯一的交接面。

---

## 0. 一句话结论

基准周 2026W02 实测 **OPTIMAL**，**14 架次（9 带飞 + 5 单飞）**，与 v6 §1.4.3 的纸面推演
逐项吻合；**阻塞项恰好 7 条**，与 v6 §1.4.2 逐条一致；静态预筛后**候选 2276 个**
（已回填 v6 §3.1.3）。§6 六条门禁全绿，**644 个测试全过，覆盖率 92.68%**。

**三件需要你拍板的事**（详见 §7）：

| # | 事情 | 影响面 |
|---|---|---|
| **Q1** | **v6 §12.3 的 I1 / I4 / I5 实测不是 INFEASIBLE**，是可行的。三条预期都建立在被 D-1 推翻的「A 类需教员带飞」前提上 | 要不要用我给的 `I1'` / `I4'` / `I5'` 替换它们（已实现、已实测 INFEASIBLE） |
| **Q2** | **v6 §3.11 的「同 seed + 同 worker 数 → 逐字节可复现」在 OR-Tools 9.15 上不成立**（实测同一份模型 4 worker 三次连跑给出 3 个不同的等价最优解）。我加了一道单线程规范化解决它 | 要不要把这条实测结论写进 v6 §3.11 |
| **Q3** | v6 **附录 B 的 `Sortie` 契约与 §5.1.1 自相矛盾**：`person_id` 钉 `^P\d{2}$`、`airspace_id`/`runway_id` 是基准取值的 `Literal`，而 §5.1.1 说编号只固定前缀、机型由数据决定 | 换一批数据（9 个人 / 三位编号 / 别的空域名）时 `SchedulePlan` 会直接 ValidationError |

---

## 1. 开工第一件事的答案（v6 §1.4 的预期核对）

| 问题 | 实测 |
|---|---|
| 静态预筛后的候选数 | **2276**（带飞 1521 / 单飞 755；含 62 个 `is_recurrent`） |
| 求解状态 | **OPTIMAL** |
| 变量数 / 约束数 | **12 568 / 37 235** |
| 墙钟 | **21.0s**（优化阶段 12.3s + 规范化阶段 8.4s），预算 30s |
| 阻塞项是否恰好是 §1.4.2 的 7 条 | **是，逐条一致** |
| 架次数 | **14 = 9 带飞 + 5 单飞**，与 §1.4.3 完全一致 |

**没有 INFEASIBLE，不需要动任何约束。** 高敏感参数两处都按裁定实现：
S-12 是 `deadline = freq_days − 1`（**不是** `gap=999`；A 类实测 deadline=2 而非 0），
约束3 按 S-13 对**全部 4 名学员**生效（实测 4 名学员的 A 类周次数分别为 1/1/1/2）。

---

## 2. 交付物

| 文件 | 职责 |
|---|---|
| `backend/core/ruleset.py` | **新增**。`ruleset_v1.3.yaml` / `semantics.yaml` 的类型化加载器 |
| `backend/nodes/compile_spec.py` | **新增**。`ConstraintSpec` 编译 + `training_progress` 物化（S-01 展开 / S-11 锚点） |
| `backend/solver/data.py` | 按 `snapshot_id` 从 PG 读实体 → `ProblemData`；`ScenarioOverrides` 外部扰动 |
| `backend/solver/candidates.py` | 候选枚举 + 静态预筛 + 阻塞项 + 约束3/13/S-11 的统一要求集（§3.1、§3.5） |
| `backend/solver/model.py` | 14 条规则的 CP-SAT 编码 + assumption literals + 冻结/warm start/增量约束（§3.2~3.5、§3.8） |
| `backend/solver/objective.py` | 分阶段求解 + 词典序权重推导 + 规范化 + 求解预算（§3.7、§3.11） |
| `backend/solver/reschedule.py` | 三档冻结策略与扰动翻译（§3.8） |
| `backend/solver/diagnose.py` | 最小冲突集 / 归因 / 松弛提案 / 实证验证 / 探针预算池（§3.9、§3.10） |
| `backend/solver/solve.py` | 顶层入口，解 → `SchedulePlan`，三态严格分离 |

**测试**（新增 8 个测试文件、**194 个用例**，另 2 个 fixture 文件）：

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/unit/test_ruleset_loader.py` | 19 | ruleset/semantics 解析、R0 不可松弛、松弛阶梯、`req_max` 公式 |
| `tests/unit/test_solver_candidates.py` | 30 | §3.1.1 编成判定式、S-01/S-11/S-12、预筛八项、候选确定性 |
| `tests/unit/test_solver_model.py` | 33 | 14 条约束逐条「宁可不排也不违反」、六种增量约束 |
| `tests/unit/test_solver_objective.py` | 15 | 分阶段、词典序权重、割线下界的蕴含性、可复现性 |
| `tests/unit/test_solver_reschedule.py` | 13 | 三档冻结策略（参数化）、跑道冻结、汉明距离 |
| `tests/unit/test_solver_diagnose.py` | 25 | 冲突集/归因/提案/实证验证/预算熔断三条 |
| `tests/guardrail/test_solver_isolation.py` | 29 | 隔离与「不留半成品」逐文件检查 |
| `tests/integration/test_solver_baseline_live.py` | 30 | 基准周 + S-11 专项 + I1~I5 + I1'/I4'/I5' + 局部重排，直连 PG |

`tests/fixtures/solver_facts.py`（合成算例：机型 `TX-1`、人员 P41~P43、1~4 架飞机、
4 门课目 —— 规模/编号/机型全部不同于基准数据，用来证明求解器没写死 §1.3）·
`tests/fixtures/solver_asserts.py`（**只服务本窗口**的临时合规断言器，不进 `validator/`）

---

## 3. 关键决策与理由（**踩过的坑都在这里**）

### 3.1 ⚠️ 三处「等价重写」，不改语义，但**不写就跑不到 OPTIMAL**

照 v6 §3.1.3 / §3.2 的字面写法实现的第一版，基准周 **30s 内证不到最优**
（实测 gap 35.7%，状态停在 FEASIBLE）。三处改动都是**逐字等价**的重新编码：

| # | v6 原文 | 本实现 | 效果 |
|---|---|---|---|
| ① | `start[c]` 逐候选一个整数变量 | 按**架次时隙** `(受训人, 课目, 天)` 共享 | 2276 → 231 个时刻变量。等价依据：约束14 要求「候选集按 (person, mission, day) 唯一」，同一时隙至多产出一个架次 |
| ② | `rwy[c][r]` 逐候选一组跑道布尔 | 同样按时隙建，用 `x[c] → ¬rwy[s][r]` 挂机型限制 | 20 分钟窗口的区间数 4552 → 462 |
| ③ | 约束7「同机候选两两 reified 析取」 | **尾部延长 T 的区间 + `AddNoOverlap`** | 约束数 O(n²) → O(n)。等价依据：两条 `[s, s+dur+T)` 不重叠 ⟺ `s_j ≥ s_i+dur_i+T ∨ s_i ≥ s_j+dur_j+T`，就是那条析取 |

模型规模从 32 447 变量 / 77 531 约束降到 12 568 / 37 235，求解从「30s 证不到」变成「12s 证到」。
**这与 v6 §3.3 为约束9 放弃 O(n³) 朴素写法、改用原生 `AddCumulative` 是同一性质的选择：换编码，不换语义。**

维护时段另起一条 `NoOverlap`，用**未延长**的区间 —— 规格只说「维护时段内不得安排架次」，
没要求维护前后也留周转时间。

### 3.2 ⚠️ **CP-SAT 在固定 seed + 固定 worker 数下不保证返回同一个等价最优解**

**本窗口最反直觉的一个发现，也是铁律 9 差点过不去的地方。** 实测（同一份模型 proto，
逐字节相同，已验证）：

| workers | 3 次连跑状态 | 目标值 | **不同解的个数** | 胜出的子求解器 |
|---|---|---|---|---|
| 1 | FEASIBLE ×3 | 都是 82782140 | **1** | `main` |
| 4 | OPTIMAL ×3 | 都是 82782100 | **3** | `default_lp` / `scheduling_intervals_lns` / `rins_pump_lns` |

最优**值**是确定的，最优**解**不是 —— 哪个 worker 先撞上一个等价最优解取决于线程调度。
**v6 §3.11 的「同 seed + 同 num_search_workers → 逐字节可复现」在 OR-Tools 9.15 上不成立**（Q2）。

解决办法是两段式：多线程负责**找到并证明最优值**（§3.11 要求的 4 worker 照旧），
之后**单线程**把三个偏好分量各自钉在已证明的值上、清掉 warm start 提示、重解一次，
在等价最优解里挑出确定的那一个。代价是一次额外求解（基准周 8.4s）。

三个坑，按踩到的顺序：

1. **钉「合成目标 == 那个八位数」传播不动** —— 单线程 18 秒连可行解都找不到。
   必须**逐个分量钉**（`架次总量 == 14`、`峰值之和 == 13`、`起飞时刻之和 == 49`）。
   分量的值由合成目标唯一确定（权重是词典序分离的），所以两种钉法语义等价。
2. **规范化阶段不能带 warm start 提示** —— 提示来自多线程那一段，把随机性又带回来了。
3. **不能按「总预算 − 已耗时」给下一阶段分预算** —— 无论用墙钟还是用
   `deterministic_time`，那个减数都会抖，于是上限本身每次不同、切在哪儿也每次不同。
   预算改成**按固定比例预先切开**（`OPTIMIZE_BUDGET_RATIO = 0.75`）。

### 3.3 ⚠️ 「确定性时间」不是秒

`max_deterministic_time` 是与机器无关的工作量单位，本模型 4 worker 下实测约为墙钟的
1.2~1.7 倍。早先把它直接设成墙钟秒数，结果**它先到**、把本来能证到最优的求解切断
——集成测试里基准周因此偶发 FEASIBLE 且方案不一致。现在按 `DET_TIME_SLACK = 3.0`
放宽，让它只当兜底，墙钟才是真正的预算闸。

**可复现性的真实边界**（已写进代码文档与测试）：靠证明结束的求解（`OPTIMAL`）逐字节
可复现；被预算截断的求解（`FEASIBLE`）不保证 —— 而 `FEASIBLE` 这个状态本身就在说
「这不是最优解」。集成测试因此把两件事拆成两个用例：预算够不够用默认 30s 验，
可复现性给足 90s 验。

### 3.4 阶段1 的目标必须按「完成度」算，不能按 `Σ x` 算

v6 §3.7 写 `max Σ w_mission · x[c]`，括注是「**松弛档下才有取舍空间**」。这句括注决定了
唯一自洽的读法：**Tier 0 下阶段1 必须是常量**。照字面最大化 `Σ x[c]`，Tier 0 下求解器会
一路加架次直到撞上约束11/12/14 的上限（基准周从 14 架次涨到四名学员各飞满 10 架次），
那不是「进度完成度最优」，是「把机队塞满」。所以阶段1 取**要求满足度**
`Σ w_req · sat[req]`：Tier 0 下 `sat` 恒为 1、目标恒为常量（实现里直接跳过该阶段），
Tier 1/2 下约束13/约束3 降级为软目标，`sat` 才有取舍空间。

### 3.5 阶段3 比 v6 §3.7 多一项「架次总量」，权重是算出来的不是拍的

阶段1 取完成度之后，Tier 0 下「多排几个不必要的架次」在阶段1/2 里都是零代价，四项均衡项
也拦不住它（多排的架次只要摊匀，方差反而不涨）。没有这一项，基准周会排出 30~40 个
全部合规但没人想看的架次。它是 **R3 偏好项，不影响可行性**。

权重反过来推：先定优先次序（架次总量 → 负荷峰值 → 起飞时刻），再按
`w_k = 1 + Σ_{j>k} R_j·w_j` 算出保证该次序的**最小**权重，上界 `R_*` 从模型自身取
（约束11 的周上限、训练窗长度）。这样「词典序等价」是可证的，而不是「量级差得够开就行」。

### 3.6 方差用 min-max 替身，不用极差

CP-SAT 是整数线性求解器，真方差是二次的。三个「不均衡」项一律用**峰值负荷**表达。
**不用极差 `max − min`** 的原因很实在：机队里可能存在本周一架次都排不上的飞机
（基准周的两架 JL-9 —— 学员没有 JL-9 机型资质、刘斌本周又没有任何要求），
它把 `min` 恒钉在 0，极差退化成「峰值 − 0」，均衡项只剩噪声。

### 3.7 三族**蕴含下界**，缺一个就证不到最优

这三族约束都由既有约束**逻辑蕴含**，不改变可行集，作用是让 LP 松弛一上手就拿到正确下界：

| 下界 | 推导 | 不加它的后果（实测） |
|---|---|---|
| 每 (人, 课目) 的最少架次数、全周架次总数下界 | 滑窗的最小命中数（区间点覆盖，贪心即最优）；按 (人, 类别) 分桶相加，桶间互不相交 | 阶段「架次总量」14.8s → 8.9s |
| 峰值负荷的**子集**均值下界 `|S|·peak ≥ Σ_{i∈S}` | 峰值不低于任何子集的平均值。**关键是按机型分子集**：全 8 架取平均给 `14/8→2`，但 14 个架次全落在 6 架 JL-8 上，按机型子集才得到正确下界 `14/6→3` | 差这 1，阶段「负荷均衡」从秒级证明变成 18 秒证不完 |
| 起飞时刻之和的**割线族** | 7 分钟间隔全场统一 ⟹ 当天第 i 个起飞 `≥ sep·(i−1)` ⟹ `Σt ≥ sep·k(k−1)/2`；该函数在整数上凸，取每个整点的割线 `Σt ≥ f(k) + sep·k·(n−k)` | 先写成 reified 版（`n ≥ k` 则 `Σt ≥ f(k)`）**完全没用** —— enforcement literal 形式进不了 LP 松弛，下界只到 5（真最优 49）。改成纯线性割线后 LP 直接吃进去 |

### 3.8 `cp_model_probing_level = 0`

本模型的 presolve probing 每次要花 4~6 秒，而分阶段求解会反复调 `Solve()`。
实测基准周 probing=2 → 11.3s、probing=0 → 5.1s，**最优值一模一样**。
probing 只影响搜索效率，不影响可行集与最优值。

### 3.9 「一个架次都不排」不算解决方案

资源被抹平的场景（机队全维护、跑道全关）下，松弛 R1/R2 能「可行」只是因为把要求本身
撤掉了，探针给回来的是个**空方案**。那不是排班，是取消本周。照 v6 §12.3 I2 的口径，
这种情形的合格输出是**升级人工，提示需调配资源**。所以 `Diagnosis` 区分
`verified_proposals`（探针通过）与 `useful_proposals`（探针通过**且真的排出了架次**），
升级判定看后者。提案照常呈现（它如实写着代价 0 架次 + 10 项欠账），但标升级。

### 3.10 最小冲突集要补「结构性不可满足组」

CP-SAT 给的 core 是**极小**的：只要一组自相矛盾就够了，不会把「另一组其实也同样矛盾」
一并列出。I2 实测就是这样 —— sat core 只有 `C13_frequency`，而约束3 在同一场景下同样
一个候选都没有，v6 §12.3 恰恰把它列为预期冲突源。§12.3 要求「冲突源召回率 100%」，
极小 core 满足不了。所以在 core 之外再做一次**确定性结构判定**：某个要求的候选范围为
空集时，它所属的组必然不可满足，直接并入。不是猜，是逻辑必然，且不额外花一次求解。

同一性质的第二处：**归因后的规则编号**才是判断「是否 R0 根因」的依据。跑道全关时候选被
预筛清空，sat core 只剩 R2 的 C03/C13，真正的根因约束9 是归因阶段从 `DropReason` 补回来的。

### 3.11 预筛顺序有语义：类别资质检查必须排在先修检查**之前**

否则学员会因为够不到的课目（D/E/G/H 类，§1.4.1 的双重排除）冒出一堆假阻塞项。
基准周的阻塞项恰好 7 条，多一条就是这个顺序被改坏了。已有专门用例钉住。

### 3.12 诊断模式下**不能**抢先把放不进训练窗的候选压成 0

`I4'`（训练窗压到 30 分钟）第一次跑出来的冲突集里没有约束1 —— 因为常规路径上
「候选连一次都放不进窗口」是被无条件 `x==0` 掉的，assumption literal 沾不上。
诊断模式改为让 `post_c01` 用 gated 上下界表达，训练窗压缩这个根因才进得了最小冲突集。

### 3.13 约束8 的「休息 30 分钟」用前置计数表达，不枚举三元组

「连续 2 架次后休息 30 分」在单人单日 ≤3 的前提下等价于「前置架次数 ≥2 的那个架次，
与它之前的每个架次间隔 ≥30」（第 1 架次结束更早，那条由传递性自动成立）。
这样只要 O(n²) 的序变量，不需要 O(n³) 枚举三元组。

---

## 4. 出口标准逐条实测

| # | 出口标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 基准周求解结果（状态/候选/变量/约束/墙钟/worker） | ✅ | §1 与 §5.1 |
| 2 | 阻塞项与 v6 §1.4.2 的 7 条逐条一致（贴全量） | ✅ | §5.2 |
| 3 | 可行则 30s 内 OPTIMAL，贴 solver log | ✅ | 21.0s / OPTIMAL，log 见 §5.4 |
| 4 | 跑道分配正确性自测 | ✅ | §5.3；**JL-9 → RWY-1 另有专门构造**（见下） |
| 5 | I1~I5 全部判 INFEASIBLE | ❌ **I2/I3 是，I1/I4/I5 实测可行** | §5.5 + §7 Q1，另交付 `I1'/I4'/I5'` 三个真不可行构造 |
| 6 | 每个 I 场景 ≥1 个经 probe_solve 验证过的提案（I2 允许升级人工） | ✅ | §5.5 |
| 7 | S-11 专项 | ✅ | §5.6 |
| 8 | 局部重排三档各有用例（含跑道冻结） | ✅ | `test_solver_reschedule.py`，三档参数化 + 基准周实数据一例 |
| 9 | 探针预算池熔断有测试覆盖 | ✅ | 次数熔断 / 累计秒数熔断 / 单次时限裁剪 三条 |
| 10 | 同输入 + seed=42 + 同 worker 数连跑 3 次逐字节一致 | ✅ | §5.7（**前提是求解跑完，见 §3.3**） |
| 11 | §6 六条门禁全绿，无 TODO | ✅ | §4.1 |
| 12 | `rg "from backend.validator\|import validator" backend/solver/` 为空 | ✅ | §4.2 |

> **第 4 条的补充说明**：基准周的最优方案里**一个 JL-9 架次都没有**（学员无 JL-9 机型
> 资质，刘斌本周又不受任何频率约束），所以「JL-9 全在 RWY-1」在基准方案上是**空断言**。
> 为了让它变成实的，另构造了一例：把刘斌 C 类到期日提前到 01-04（S-11 复训窗口整周落在
> 周内）+ `PIN_RESOURCE` 把他钉到一架只有 RWY-1 服务的飞机上 → 实测该架次落在
> **AC95 / RWY-1**，且该机型的可用跑道集合就只有 `("RWY-1",)`。

### 4.1 §6 六条门禁

```
ruff check .            → All checks passed!
ruff format .           → 128 files left unchanged
mypy backend --strict   → Success: no issues found in 80 source files
bandit -r backend -ll   → No issues identified.（exit 0，11891 行）
lint-imports            → Contracts: 3 kept, 0 broken.
pytest -q --cov=backend --cov-fail-under=80
                        → 644 passed / 0 failed，Total coverage: 92.68%（门槛 80%）
```

```
rg -n "TODO|FIXME|NotImplementedError|待实现|待补充|后续补" backend/ frontend/ tests/
→ 只命中 tests/guardrail/test_solver_isolation.py 自己的检查正则，backend/ 与 frontend/ 为空 ✅
```

`backend/solver/` 各文件覆盖率：`data.py` 99% · `reschedule.py` 98% · `solve.py` 97% ·
`model.py` 96% · `objective.py` 96% · `candidates.py` 93% · `diagnose.py` 87%；
另 `nodes/compile_spec.py` 93% · `core/ruleset.py` 93%。

### 4.2 隔离验证（铁律 2）

- `rg "from backend.validator|import validator" backend/solver/` → **输出为空**
- `lint-imports` 禁令一（validator 不得 import solver）→ KEPT
- 新增 `tests/guardrail/test_solver_isolation.py`：求解链路 8 个文件逐个查
  「代码行里不出现 validator」「没有半成品标记」「没有把 `JL-8`/`AC10`/`P01` 写成代码常量」，
  外加一条动态检查（import 求解链路后 `sys.modules` 里不出现 `backend.validator`）
- **本窗口从未打开 `backend/validator/` 下任何文件**（该目录当前只有 `__init__.py`）
- 自测用的合规断言器写在 `tests/fixtures/solver_asserts.py`，**不在 `backend/validator/`**，
  也不预判它的接口形状。M2-B 看不到它（不在 `backend/` 里），M2-C 的三重独立验证也不用它

> ⚠️ **诚实交代**：铁律 2 的另一半「写 validator 的窗口不许打开 backend/solver/」
> **测不出来**，只能靠窗口纪律。我没有假装它被测到了。

---

## 5. 实测输出（原样粘贴）

### 5.1 候选规模与求解统计

```
===== 基准周 2026W02 实测（snapshot=snap_9724982865ee）=====
-- 静态预筛后的候选数 --
候选总数 2276    带飞 1521 / 单飞 755 / 其中 is_recurrent 62
架次时隙数 231   要求集 28 条   阻塞项 7 条
预筛剔除计数 {'NO_CLASS_QUAL': 20, 'PREREQ_UNMET': 7}
按受训人：P04(刘斌)=427  P05(罗磊)=667  P06(张勇)=550  P07(陈伟)=550  P08(何超)=82

-- 求解 --
状态=OPTIMAL  变量=12568  约束=37235  墙钟=21.0s  worker=4  seed=42
   阶段3 均衡与偏好: OPTIMAL obj=82782100.0 bound=82782100.0 wall=12.3s
   规范化 单线程重解（铁律 9：可复现性）: OPTIMAL wall=8.4s
gap=0.0  branches=62063  conflicts=3683
```

何超只有 82 个候选是 §1.4.2 的直接后果：5 门课目全 BLOCKED，只剩 A-1/A-2 两门单飞。

### 5.2 阻塞项（7 条，与 v6 §1.4.2 逐条一致）

```
   P06 | missionC-2 | missionC-1 未完成 | missing=['missionC-1']
   P07 | missionC-2 | missionC-1 未完成 | missing=['missionC-1']
   P08 | missionB-1 | missionA-2 未完成 | missing=['missionA-2']
   P08 | missionB-2 | missionA-2 未完成 | missing=['missionA-2']
   P08 | missionC-1 | missionA-2 未完成 | missing=['missionA-2']
   P08 | missionC-2 | missionC-1 未完成 | missing=['missionC-1']
   P08 | missionF-1 | missionA-2 未完成 | missing=['missionA-2']
```

措辞刻意做成 v6 §12.3 要求的「`missionA-2 未完成`」形态 —— Sheet 4 区块 4 直接用，
不让报表层二次拼装（拼两遍必然对不上）。

### 5.3 方案全量 + 跑道自测

```
-- 方案：14 架次（plan_id=2026W02-81b6c58df554）--
   S000001 2026-01-05 周一 06:00-06:54 missionB-2  RT1 AC27 RWY-1 recurrent=False [孙军教员，陈伟学员]
   S000002 2026-01-05 周一 06:07-06:34 missionA-2  SAB AC61 RWY-2 recurrent=False [罗磊单飞]
   S000003 2026-01-06 周二 06:00-06:35 missionC-1  IFR AC73 RWY-2 recurrent=False [吴鹏教员，张勇学员]
   S000004 2026-01-06 周二 06:07-06:34 missionA-2  SAB AC61 RWY-1 recurrent=False [何超单飞]
   S000005 2026-01-07 周三 06:00-06:35 missionC-1  IFR AC34 RWY-1 recurrent=False [吴鹏教员，陈伟学员]
   S000006 2026-01-07 周三 06:07-06:59 missionB-1  RT2 AC73 RWY-1 recurrent=False [高超教员，张勇学员]
   S000007 2026-01-08 周四 06:00-06:30 missionA-1  SAA AC10 RWY-2 recurrent=False [陈伟单飞]
   S000008 2026-01-08 周四 06:07-06:47 missionF-1  SAB AC73 RWY-1 recurrent=False [高超教员，罗磊学员]
   S000009 2026-01-09 周五 06:00-06:27 missionA-2  SAB AC10 RWY-2 recurrent=False [何超单飞]
   S000010 2026-01-09 周五 06:07-06:47 missionF-1  SAB AC34 RWY-1 recurrent=False [孙军教员，张勇学员]
   S000011 2026-01-10 周六 06:00-06:27 missionA-2  SAB AC10 RWY-2 recurrent=False [张勇单飞]
   S000012 2026-01-10 周六 06:07-06:47 missionF-1  SAB AC61 RWY-2 recurrent=False [高超教员，陈伟学员]
   S000013 2026-01-11 周日 06:00-06:54 missionB-2  RT1 AC49 RWY-1 recurrent=False [孙军教员，张勇学员]
   S000014 2026-01-11 周日 06:07-07:03 missionC-2  IFR AC27 RWY-2 recurrent=False [吴鹏教员，罗磊学员]
   debts=[]  content_sha256=81b6c58df5541b0fca57eb77a63059d7f299566be1169c6a482f870f11f99066

-- 跑道分配自测 --
   跑道分布 {'RWY-1': 7, 'RWY-2': 7}
   同跑道 20 分钟窗口 ≤2 次：通过
   全场任意两次起飞间隔 ≥7 分钟（跨跑道也算）：通过
   P05 A 类周次数 = 1   P06 = 1   P07 = 1   P08 = 2   （约束3 要求 ≥1）
```

三项已知扰动逐条核对：**吴鹏 01-05 不出现**（他的三个架次在 01-06/07/11）·
**AC73 01-09 不出现**（它的架次在 01-06/07/08）· 刘斌 C 类 01-07 到期 →
`training_progress` 落了 `is_recurrent=TRUE, recurrent_since=2026-01-08`，本周不强制安排。

### 5.4 solver log（阶段3 关键行）

```
Starting CP-SAT solver v9.15.6755
Parameters: random_seed: 42 max_time_in_seconds: 22.5 max_deterministic_time: 67.5
            cp_model_probing_level: 0 num_workers: 4
#Variables: 12'568 (#bools: 231 #ints: 234 in objective) (10'786 primary variables)
#kCumulative: 63 (#intervals: 889)   #kNoOverlap: 111 (#intervals: 1'808)
#kInterval: 3'957   #kLinear2: 10'551   #kLinearN: 2'442 (#terms: 52'224)
#Bound   1.10s best:inf   next:[187205,1.36387986e+09] initial_domain
Starting search at 1.12s with 4 workers.
#1       1.65s best:88772681 next:[6102883,88772680] fj_restart_decay_perturb_obj
#Bound   2.07s best:88772681 next:[65053738,88772680] default_lp
#Bound   8.58s best:88735233 next:[82519964,88735232] bool_core (num_cores=14)
#Bound  10.81s best:82782140 next:[82782100,82782139] default_lp
#Done   12.28s default_lp
--- 规范化阶段 ---
Parameters: random_seed: 42 max_time_in_seconds: 17.63 max_deterministic_time: 22.5 num_workers: 1
Starting search at 1.05s with 1 workers.
#1       8.36s main
```

### 5.5 v6 §12.3 的 I1~I5 实测 + 三个替代构造

```
❌ I1  两名教员整周不可用：候选 1210  状态=FEASIBLE  架次=14  60.1s（v6 预期 INFEASIBLE）
✅ I2  6 架 JL-8 整周维护：候选 140   状态=INFEASIBLE 架次=0  0.0s
✅ I3  IFR 整周容量 0：   候选 1698  状态=INFEASIBLE 架次=0  0.2s
❌ I4  训练窗 06:00-09:00：候选 2276  状态=OPTIMAL    架次=14  23.9s（v6 预期 INFEASIBLE）
❌ I5  RWY-2 关 + 窗 06:00-08:00：候选 2276 状态=OPTIMAL 架次=14 22.9s（v6 预期 INFEASIBLE）

✅ I1' 三名教员全部整周不可用：候选 755  状态=INFEASIBLE 0.1s
✅ I4' 训练窗 06:00-06:30：    候选 2276 状态=INFEASIBLE 0.3s
✅ I5' 学员机型可用跑道全关：  候选 0    状态=INFEASIBLE 0.0s
```

**I2**（v6 预期 约束6 × 约束3）：
```
最小冲突集：['C03_weekly', 'C13_frequency']  归因规则=['C03', 'C06', 'C07', 'C13']
sat_core=['C13_frequency']  structural=['C03_weekly', 'C13_frequency']
  · 罗磊(P05) 的 A 类 本周无任何可行候选 —— missionA-1: 当日适配机全部处于维护中 …
提案 TIER2 tier=2 verified=True 架次=0 欠账=10
升级人工=True：冲突集含 R0 安全刚性约束（C06, C07），松弛 R1/R2 撤不掉它 ——
             能验证通过的方案都是 0 架次（等于取消本周）。**升级人工，需调配资源。**
探针预算={'calls': 2.0, 'spent_s': 0.285, 'total_s': 120.0}
```
→ 与 v6 预期的「无 R2 方案可解 → 升级人工，提示需调配机队」一致，冲突源 C03/C06 都召回了。

**I3**（v6 预期 约束6 空域容量 × 约束13）：
```
最小冲突集：['C13_frequency']  归因规则=['C06', 'C13']
  · 罗磊(P05) 的 missionC-2 本周无任何可行候选 —— 该课目绑定的空域容量为 0（约束6，空域关闭）
提案 TIER1 tier=1 verified=True 架次=11 欠账=3     ← 推荐
提案 TIER2 tier=2 verified=True 架次=11 欠账=3
升级人工=False
```
→ 与 v6 预期的「C 类本周顺延，欠账记入下周」一致（3 项欠账 = P05 的 C-2、P06/P07 的 C-1）。

**I1'**：`归因规则=['C03','C04','C13']`，提案 TIER1 verified 架次=5 欠账=9（9 个带飞架次全欠）。
**I4'**：`最小冲突集=['C01_window','C13_frequency']`，**约束1 真的进了冲突集**；
subjects 里带出了训练窗长度与先修链（`missionC-2→missionC-1→A类`）。
**I5'**：`归因规则=['C03','C06','C07','C09','C13']`，**约束9 真的进了冲突集**（I5 的验证目标），
升级人工=True（0 架次方案不算解决方案）。

### 5.6 S-11 专项（刘斌 C 类到期日改到 2026-01-04）

```
状态=OPTIMAL  架次=15
   S000004 2026-01-06 06:07 missionC-2 AC95 RWY-1 is_recurrent=True 机组人数=1 角色=['复训']
```

三条断言全中：① 出现 ≥1 次刘斌的 C-1/C-2；② `is_recurrent=True`；③ **机组人数 1**。
另附带验证了「JL-9 只能用 RWY-1」（AC95 是 JL-9，落在 RWY-1）。

`compile_spec` 侧的锚点写入（基准周原始到期日 01-07）：
```
is_recurrent=TRUE 的行恰好 {('P04','missionC-1'), ('P04','missionC-2')}
recurrent_since 均为 2026-01-08     ← 到期次日
其余 74 行 is_recurrent=False 且 recurrent_since IS NULL
```

### 5.7 可复现性

```
run1 status=OPTIMAL sha=81b6c58df5541b0f
run2 status=OPTIMAL sha=81b6c58df5541b0f
run3 status=OPTIMAL sha=81b6c58df5541b0f
```
（同 snapshot + seed=42 + 4 worker；集成测试 `test_baseline_is_byte_reproducible` 钉住）

### 5.8 S-12 与约束3 的高敏感参数复核

```
last_done_date 全 76 行为 NULL，且 debt_basis 全部 is_debt=False（不计欠账）
missionA-2 的 FREQ_DEADLINE 截止日 = 2   ← 若写成 gap=999 会是 0（假性不可行的根源）
4 名学员的 A 类周次数 = 1 / 1 / 1 / 2    ← S-13 对全部学员生效，不论完成状态
```

---

## 6. 对 v6 的实现补充与偏离（逐条交代）

| # | 位置 | 性质 | 说明 |
|---|---|---|---|
| 1 | §3.1.3 / §3.2 约束7 / §3.3 | **等价重写** | 三处编码改写，语义逐字不变，见 §3.1。已在 `model.py` 模块文档里写清等价性推导 |
| 2 | §3.7 阶段1 | **口径澄清** | 目标取「要求满足度」而非 `Σ x`，依据是原文括注「松弛档下才有取舍空间」，见 §3.4 |
| 3 | §3.7 阶段3 | **新增一项** | 「架次总量」（R3 偏好，不影响可行性），见 §3.5 |
| 4 | §3.7 阶段3 | **替身选择** | 方差 → min-max 峰值负荷，见 §3.6 |
| 5 | §3.7 | **合并为一次求解** | 阶段3 三项合成一个带权和式（v6 原文就是和式），权重按词典序等价算出。分三级求解要付三次 presolve，且阶段间预算分配会破坏可复现性 |
| 6 | §3.11 | **参数补充** | `cp_model_probing_level=0`、`max_deterministic_time`、单线程规范化阶段。都不改变可行集与最优值 |
| 7 | §3.5.3 | **等价形式** | `first_exec_day ≤ deadline` 实现为 `Σ_{day ≤ deadline} x ≥ 1`，并补上「`deadline > 6` 时本周不构成约束」这一支（G/H 类 freq=14 落在这里） |
| 8 | §3.9 | **召回率补强** | sat core 之外补「结构性不可满足组」，见 §3.10 |
| 9 | 附录 A | **新增文件** | `backend/core/ruleset.py`（YAML 加载器）、`solver/data.py`、`solver/solve.py`、`solver/reschedule.py`。前者放 `core/` 的理由见其模块文档：它不表达任何约束，只做 YAML → 对象，两份解析器才是真隐患 |
| 10 | `.importlinter` | **收紧** | 禁令三的 source 增加 `backend.core.ruleset`（新增 core 模块纳入 egress 收口）。这是收紧不是放宽 |

**未偏离的地方也说一句**：D-1（学员 A 类单飞）、D-2（7 分钟全场统一）、D-4（通式）、
D-6（Tier 2 重定义）、S-01~S-14 全部按裁定实现，并各有专门用例。

---

## 7. 需要业务方拍板的三个问题

### Q1 ⚠️ v6 §12.3 的 I1 / I4 / I5 实测不是 INFEASIBLE

**三条预期都建立在被 D-1 推翻的前提上**。v6 §1.4.4 的告警框已经说明
「`SPEC_DECISIONS §B.4` 关于 16~24 个带飞架次的风险预警自 v6 起作废」，但 §12.3 的
I1/I4/I5 三条**没有跟着更新**。真实需求是 14 架次、摊到 7 天是 2 架次/天：

| 场景 | v6 预期 | 实测 | 算术 |
|---|---|---|---|
| I1 两名教员整周不可用 | INFEASIBLE | **FEASIBLE**（14 架次） | 单教员周上限 **12 ≥ 9** 个带飞架次；日上限 3 × 6 个可用日 = 18。v6 自己写的「单教员容量 12」也支持这个结论 |
| I4 训练窗 06:00-09:00 | INFEASIBLE | **OPTIMAL**（14 架次） | 180 分钟窗、最长课目 69 分钟，2 架次/天装得下 |
| I5 RWY-2 关 + 窗 06:00-08:00 | INFEASIBLE | **OPTIMAL**（14 架次） | 单跑道 20 分钟 ≤2 次 → 120 分钟窗仍可 **12 次/天 ≫ 2 次/天**。v6 自己算的「两跑道合计 4×9=36 起飞」同样远大于需求 |

**我已实现三个「够狠」的替代构造并实测 INFEASIBLE**（代码在
`test_i1_i4_i5_corrected_constructions_are_infeasible`，实测见 §5.5）：

| 替代 | 构造 | 冲突集 | 验的是什么 |
|---|---|---|---|
| `I1'` | **三名教员全部**整周不可用 | C03 / C04 / C13 | 教员容量真的不够（9 个带飞架次一个也排不了） |
| `I4'` | 训练窗压到 **06:00-06:30** | **C01** / C13 | 时长 > 30 分钟的课目全部装不进去，约束1 真的进冲突集 |
| `I5'` | **学员机型可用的跑道全部关闭** | C03 / C06 / C07 / **C09** / C13 | 跑道模型真的进冲突集（I5 的原始验证目标） |

**请裁定**：① 用 `I1'/I4'/I5'` 替换 v6 §12.3 的 I1/I4/I5（我改文档）；
② 还是保留原构造、把预期从 INFEASIBLE 改成 FEASIBLE（那样 §12.3「构造不可行 30 例」
里就只有 I2/I3 两族真不可行，30 例的构成需要重新设计）。
**在你裁定之前，测试如实断言实测结果**，两边都不假装。

### Q2 ⚠️ v6 §3.11 的可复现性表述与 OR-Tools 实测不符

实测证据见 §3.2：4 worker 下同一份模型三次连跑给出 **3 个不同的等价最优解**。
我用「多线程求最优值 + 单线程规范化」解决了，代价是每次求解多花 8~9 秒。

**请裁定**：要不要把这条实测结论与解决办法写进 v6 §3.11（我倾向要写 ——
下一个窗口若把规范化阶段当成"多余的一次求解"删掉，可复现性会静默失效，
而 §12.1 的黄金用例要过很久才会发现）。

### Q3 ⚠️ 附录 B 的 `Sortie` 契约与 §5.1.1 自相矛盾

附录 B 钉死了 `person_id: ^P\d{2}$`、`aircraft_id: ^AC\d{2}$`、`mission_id: ^mission[A-H]-\d$`、
`airspace_id: Literal["SAA","SAB","IFR","RT1","RT2","RNG"]`、`runway_id: Literal["RWY-1","RWY-2"]`；
而 §5.1.1 明确「编号只固定前缀约定、不限位数」「机型不是枚举」。M1 已按 §5.1.1 放宽了
ORM 与摄取校验（`^P\d+$` 等），**但附录 B 的 Pydantic 契约没跟着放宽**。

后果很具体：用户上传 9 个人（`P100`）、或空域叫 `LAC`、或跑道叫 `RWY-3`，
**摄取会通过、求解会通过、`SchedulePlan` 组装时 ValidationError**。

我**没有擅自改**（附录 B 是冻结契约，改它属于 CLAUDE.md §7 第 8 条）。
连带影响：`tests/fixtures/solver_facts.py` 的合成算例只能沿用基准的空域/跑道编号
（其余全部换掉了 —— 机型 `TX-1`、人员 P41~P43、1~4 架飞机、4 门课目）。

**请裁定**：把附录 B 的 pattern 放宽到与 §5.1.1 一致（`^P\d+$` / `^AC\d+$` /
`^mission[A-Z]-\d+$`，`airspace_id`/`runway_id` 改 `str` + 引用完整性校验）。

---

## 8. 已知限制

1. **`probe_solve` 的探针预算是「每请求」的，但没有跨请求的持久化计量。**
   v6 §3.9.2 只定义了单请求上限，够用；真要防「用户连点 10 次诊断」得在 M4-B 的
   编排层加节流。
2. **Tier 3（放宽 R1）只在诊断提案里走通，没有在主排班链路上跑过。**
   按 v6 §3.10 它「需人工审批后执行」，审批入口在 M4-B 的 `human_gate`。
   `RelaxationSettings` 的三个 bonus 字段已经是它的落点，倍增探测已实测（见 `R1_PROBE_STEPS`）。
3. **`ScenarioOverrides` 目前只能由代码构造，没有 UI/API 入口。**
   v6 §12.3 的单点/组合扰动测试集（60+60 例）要等 M9-A 的数据集窗口批量生成；
   本窗口只实现了扰动的**表达能力**并用 I 场景验证。
4. **局部重排的「已实际执行架次」靠调用方显式传 `executed_ids`。**
   激进档「仅保留已实际执行的历史架次」需要这个外部事实，求解器无从知道。
   M4-B 的 `commit_plan_node` 落库后，这个集合应当从 `sorties` 表读。
5. **空域容量的扫描线在同刻「先减后加」**（着陆与起飞同刻不算并发）。
   这是 `tests/fixtures/solver_asserts.py` 的口径；v6 §3.4 说 CP-SAT 侧用
   `[start, start+dur]` 的 Cumulative，语义一致（Cumulative 的区间是半开的）。
   **M2-B 若采用「同刻算并发」的闭区间口径，会与求解器判定分歧（FTS-3003）** —— 这条
   请在 M2-C 的三重验证里对齐。
6. **`num_search_workers` 上线期改 8 的影响没测。** §3.11 说开发期 4 / 上线期 8。
   实测 8 worker 更快（5~10s）但同样需要规范化阶段（worker 数变了最优解也可能变）。

---

## 9. 下一个窗口的前置条件与接口约定

### M2-B（独立校验器）—— ⚠️ 你**不许**打开 `backend/solver/` 下任何文件

- 依据 **v6 §3.2 对照表 + `rules/ruleset_v1.3.yaml`** 分别实现，规则参数从
  `backend.core.ruleset.get_ruleset()` 取（它不表达任何约束，只把 YAML 读成对象；
  两份 YAML 解析器才是真隐患）
- 需要「一个解」来测：**自己在 `tests/fixtures/` 手工构造**，不要跑求解器拿解
- 三处口径请特别对齐，否则必然 FTS-3003：
  1. **空域并发**：同刻着陆+起飞算不算并发（我按**不算**，见 §8.5）
  2. **20 分钟窗口**：半开 `[t, t+20)`，**按 (日, 跑道) 分组**；7 分钟间隔**全场统一**
  3. **约束2 对成熟飞行员**：按 S-11 判定而非字面，且**报告里要显式标注为授权改写**
- 契约在 `backend/schemas/plan.py`（`SchedulePlan` / `Sortie` / `BlockedItem` / `TrainingDebt`），
  已冻结、已被本窗口填满

### 给所有后续窗口的接口

```python
from backend.nodes.compile_spec import compile_spec, default_intent   # 规格编译 + 中性 intent
from backend.solver.solve import solve, solve_week                    # 求解
from backend.solver.reschedule import Disruption, local_reschedule    # 局部重排
from backend.solver.diagnose import ProbeBudget, diagnose, probe_solve # 诊断
from backend.solver.data import ScenarioOverrides                     # 外部扰动
```

- `compile_spec(...) → SpecBundle(spec, data, ruleset, semantics)`，**默认会物化
  `training_progress`**（重算 `prereq_met`/`blocked_reason`/`is_recurrent`/`recurrent_since`，
  按主键清旧行）。只读场景（探针、诊断复跑）传 `materialize=False`
- `solve(bundle) → SolveOutcome(status, stats, plan, blocked_items, debts, run, cset, built, bundle)`。
  **`plan is None` 当且仅当没有可行解**；`status` 三态严格分离，中途没有任何
  「兜底成 INFEASIBLE」的分支
- `week_start` 必须是周一，否则 `RequiredInputMissingError`
- `SolveIntent` 的四类旋钮都已接通：范围（`scope_persons`/`scope_missions`）、
  冻结策略（`reschedule.select_frozen`）、目标权重（`objective_weights.balance`
  为 0 时关掉均衡与早飞偏好项）、松弛档位（`RelaxationSettings`）。
  六种 `IncrementalConstraint` 全部实现并有用例（W7 的 `translate_revision` 只需产出它们）

### ⚠️ 四个会咬人的坑

1. **删掉规范化阶段 = 可复现性静默失效**（§3.2）。它看起来像"多余的一次求解"，
   它不是。若要改动 `objective.py` 的预算分配，先跑
   `test_baseline_is_byte_reproducible`。
2. **三族蕴含下界删一个就证不到最优**（§3.7）。它们看起来像冗余约束，
   注释里写了「删了会怎样」的实测数字。
3. **预筛顺序是规格的一部分**（§3.11）：类别资质检查必须在先修检查之前，
   否则基准周的 7 条阻塞项会变成十几条。
4. **诊断模式与常规模式是两套时间域**：常规模式 `start` 域即 `[lo, hi]`（约束1 结构性
   成立），诊断模式用宽域 + gated 上下界（否则训练窗压缩进不了冲突集，§3.12）。
   改 `build_variables` 时两边都要想到。

---

## 10. 本窗口对 `docs/` 的改动

**只改了一处，且是 v6 自己要求的回填**（铁律 6 的正向要求）：

- **§3.1.3 候选规模**：把「由 M2 窗口实测填入本节」替换为实测表格
  （候选 2276 / 变量 12 568 / 约束 37 235 / 墙钟 21.0s / 按受训人分布 / 预筛剔除计数），
  并补一句「变量数不是候选数」的说明。

其余想改的地方（§12.3 的 I1/I4/I5、§3.11 的可复现性表述、附录 B 的 pattern）
**一律没动**，列在 §7 等你裁定 —— CLAUDE.md §7 第 8 条。
