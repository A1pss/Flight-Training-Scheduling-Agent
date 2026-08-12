# M2-C 收工报告 · 求解器与校验器的交叉验收

**窗口** M2-C（第三方独立实现者）· **日期** 2026-08-12 · **分支** `feat/m2c-crosscheck`
**依据** `CLAUDE.md`、`docs/SPEC_DECISIONS.md`、`docs/M0_规格锁定.md`、
v6 **§1.4 / §3.2 / §3.3 / §3.4 / §3.5 / §3.9 / §12.1 / §12.3**、
`reports/M2A_收工报告.md`、`reports/M2B_收工报告.md`

**写给下一个窗口的自己看。** 你读不到我这次会话的上下文，本文件是唯一的交接面。

---

## 0. 一句话结论

**双通道交叉验证抓到了一条真的规格分歧，而且是文档层面的。**

属性测试 `test_solver_output_always_passes_validator` 的第 1 个反例在**基准周真实
数据**上一字不差地复现：S-11 复训要求的粒度，求解器按「类别」、校验器按「课目」，
两侧代码都没写错 —— 错的是 **v6 §3.2 约束13 行与 §12.3 验收断言 ① 互相矛盾**。
业务方 2026-08-12 裁定取**类别**粒度（`Z-8`），校验器与 naive checker 已对齐，
求解器一行未改。**这条是 CLAUDE.md 铁律 2 那套隔离唯一能抓到的东西**：两边若共用
一份约束表达代码，这条矛盾会被一起实现成同一个读法，永远不会浮出水面。

裁定落地后的实测（全部来自真实运行，铁律 6）：

| 出口标准 | 实测 |
|---|---|
| 属性测试 500 例全绿 | ✅ **500 passing / 0 failing**（另 25 组属性各 30~200 例，全绿） |
| 主校验器 vs naive checker 在 200 场景上逐条一致 | ✅ **100%**，分歧 **0** 条 |
| I1~I5 全部 30 个判 `INFEASIBLE`，无一 `UNKNOWN` | ✅ **30/30**，全集 `UNKNOWN` = **0** |
| 最小冲突集召回率 = 100% / 精确率 ≥60% | ✅ 召回 **100%**（micro & macro）· 精确 **74.2% / 80.9%** |
| I4 冲突集含约束1、I5 含约束9 | ✅ 各 6/6 变体 |
| BLOCKED 专项四条断言 | ✅ 基准周 7 条阻塞项逐字一致 |
| S-11 专项三条断言 | ✅（裁定后） |
| 200 场景：硬约束满足率 100% / 格式校验 100% | ✅ **97/97 · 100%** |
| 黄金用例 | ✅ 40 个基线固化，121 项断言全绿 |
| §6 六条门禁 + 两条静态扫描 | ✅ 全绿，**1087 项测试、覆盖率 92.97%** |

详见 §5、§6 与 `reports/M2_交叉验收报告.md`。

---

## 1. 交付物

| 文件 | 行数 | 职责 |
|---|---|---|
| `tests/naive_checker.py` | 1131 | **第三方独立校验器**：pandas O(n²) 暴力实现 14 条（v6 §12.3 度量方式第 2 条） |
| `tests/property/scenario.py` | 816 | `arbitrary_scenario()`：随机人员/飞机/空域/跑道/异常组合，**双投影**喂两条通道 |
| `tests/property/world.py` | 403 | 注入用的固定小世界 + **手工排**的 5 架次合规基线 |
| `tests/property/plans.py` | 84 | `arbitrary_schedule_plan(ctx)`：随机**合法**方案（构造保证，不靠过滤） |
| `tests/property/injections.py` | 542 | 29 种确定性注入 + 10 种与布局无关的注入，14 条规则逐条覆盖 |
| `tests/scenarios/catalog.py` | 839 | 200 场景的程序化构造（标签天然正确） |
| `tests/scenarios/calibrate.py` | 413 | 边界场景「恰好」的单调旋钮二分标定 |
| `tests/scenarios/runner.py` | 390 | 逐场景求解 + 三重校验 + 冲突集度量 |
| `tests/scenarios/run_suite.py` | 156 | CLI：`generate` / `show` / `run` |
| `tests/scenarios/report.py` | 218 | 只读结果 JSON → 验收报告用的四张表（不重跑求解） |

**测试**（新增 6 个测试文件 + 40 个黄金基线）：

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/property/test_solver_validator_agreement.py` | 21 | **核心不变量 500 例** + 三态分离 / BLOCKED / 扰动可见性 / 编成 / 可复现性 |
| `tests/property/test_injected_violations.py` | 75 | 基线合法性 + 29 种确定性注入 ×2 通道 + 4 处必测形态 + 5 条随机注入属性 |
| `tests/property/test_scenario_generator.py` | 21 | 生成器自洽（引用完整性 13 条 + 双投影一致 8 条） |
| `tests/property/test_s11_class_scope.py` | 4 | **原 FTS-3003 的回归**（含反方向：整类不飞仍须报 C13） |
| `tests/unit/test_naive_checker_independence.py` | 21 | naive checker 的独立性护栏 |
| `tests/golden/test_golden_plans.py` | 121 | 40 个黄金用例 × 3 组断言 + 目录规模 |
| `tests/integration/test_crosscheck_live.py` | 19 | BLOCKED 专项 / S-11 专项 / 基准周三重对拍（连库） |

**数据集**：`datasets/plan_scenarios/v1/`（`scenarios.json` 200 条 + `manifest.json` +
`calibration.json` 标定记录）。

**改动的既有文件（三处，均已于 2026-08-12 经业务方裁定）**：
`backend/validator/checks.py::check_c13`（S-11 按类别归组）、
`rules/ruleset_v1.3.yaml`（约束13 补 S-11 粒度）、
`docs/FTS_…_v6.md`（`Z-8` + §3.2 约束13 行 + §12.3 断言 ① 注）、`CLAUDE.md`（§4 速查表 + §11 两条反模式）。

---

## 2. 独立性的诚实交代（**这一节请务必读**）

第三方校验器的全部价值建立在「它不是主校验器的一份拷贝」上。能自动测到的那一半在
`tests/unit/test_naive_checker_independence.py`（不 import 判定模块、14 条齐全、
不写死基准取值、确实用了 pandas）。测不到的那一半如实交代如下：

**做到了的：**

- `tests/naive_checker.py` 的 14 条**全部写完、跑完对拍之后**，我才第一次打开
  `backend/validator/checks.py`。编写依据只有三样：v6 §3.2 的 14 条规格表（含
  §3.3/§3.4/§3.5/§3.1.1 的展开）、`rules/*.yaml`、`data/origin/rules.pdf` 的原文
  （原文我是用 `pdfplumber` 现抽出来读的，见 §5.1 的粘贴）。
- `backend/solver/` 在写 naive checker 期间也没打开过。

**没做到 / 必须打折的：**

1. **我按窗口提示词的【前置】要求读了 `reports/M2B_收工报告.md`，其中 §2 是一张
   「14 条 × 校验器侧判定依据」的对照表。** 那张表写的是**语义**（不含代码），但它
   确实是「M2-B 怎么读规格」的一份摘要。所以本窗口的 naive checker 是
   **「规格级重新推导」，不是「盲写」** —— 这个折扣必须记在这里，不能让验收方以为
   它是完全盲的第三方实现。
   > 补一句判断：这个折扣**没有掩盖掉分歧**。证据就是 §3 那条 FTS-3003 —— naive
   > checker 与主校验器在 S-11 上站在同一侧，而两边都与求解器不同。如果 naive
   > checker 只是主校验器的复读机，它同样会复读出这个读法；但它是**独立从 §3.2
   > 推的**，这一点由「两处实现细节完全不同」佐证（naive 用逐分钟前缀和数空域并发，
   > 主校验器用扫描线；naive 用两两配对查周转，主校验器按机分组排序）。
2. **裁定之后我改了 `check_c13`**，那时已经读过它。此后 naive checker 的 C13
   与主校验器的 C13 不再是互相独立写出来的 —— 两者都按同一条裁定实现。
   **C13 这一条的对拍从此只有回归价值，没有发现价值。** 其余 13 条不受影响。
3. 「打开 solver 瞄一眼再照着写」这类行为查不出来。本窗口不写 solver，
   但我为了定位 FTS-3003 读了 `solver/candidates.py` 的 S-11 段落 —— 那是
   **诊断分歧**必须做的事，不是实现参考。

---

## 3. ★ FTS-3003：S-11 复训粒度（本窗口最重要的产出）

### 3.1 反例长什么样

属性测试跑到第 1 个反例时给出的是一个合成场景，我立刻拿**基准周真实数据 + v6 §12.3
S-11 专项的原构造**（刘斌 C 类到期日提前至 2026-01-04）复现：

```
状态=OPTIMAL  架次=15
   S000004 2026-01-06 missionC-2 is_recurrent=True 机组=[('P04','复训')]
progress P04（is_recurrent 的行）:
   ('missionC-1', True, recurrent_since=2026-01-05, 'COMPLETED')
   ('missionC-2', True, recurrent_since=2026-01-05, 'COMPLETED')

MAIN  all_passed: False
   C13 HARD 刘斌(P04) 的 missionC-1（每 7 天 ≥1 次，锚点缺失（S-12：自本周周一起算））本周一次都未安排
NAIVE C13×2
   P04 的 missionC-1 在周内窗口 [第0天, 第6天] 内一次都没安排（freq_days=7）
   P04 的 missionC-1 首次执行须不晚于第 6 天（freq_days=7），实际安排在 本周未安排
```

### 3.2 两侧各自读的是 v6 的哪一段

| 侧 | 实现 | 依据 |
|---|---|---|
| 求解器 | `candidates.py` 下一条 `S11\|<人>\|<类别>` 要求，**整类 ≥1 次** | §12.3 S-11 专项断言 ①「≥1 次刘斌的 C-1 **或** C-2」 |
| 校验器 + naive | 按 `training_progress` 的 `is_recurrent` 行**逐 (人, 课目)** 判 7 天滑窗 | §3.2 约束13 行「S-11……**同样受本条约束**」，而约束13 的粒度就是 person×mission |

**两边都没写错代码。** 根因是 v6 内部自相矛盾。

### 3.3 裁定与落地

业务方 2026-08-12 裁定：**取类别粒度**。理由（我提问时给的分析，业务方采纳）：
S-11 的语义是「保持资质有效性」，与 S-02（A 类整体 ≥1 次，同为「保持熟练度」）同构；
而约束13 主体的语义是「推进训练进度」，粒度本就不同（§3.5.4 那张对照表）。

落地五处：

| # | 位置 | 改动 |
|---|---|---|
| 1 | `validator/checks.py::check_c13` | `is_recurrent` 的行按 `(人, 类别)` 归入 `recurrent_groups`，循环后**整类合并计数**判一次；窗口起算取组内最早 `recurrent_since`，锚点取组内最晚 `last_done_date` |
| 2 | `tests/naive_checker.py::frequency_requirements` | 同一裁定的独立实现：复训行合成**一条** `_Requirement`，`mission_ids` 是该类全部课目 |
| 3 | v6 §3.2 约束13 行 + 一段 ⚠️ 说明 | 写清粒度是类别、以及「这条是交叉验证抓出来的」 |
| 4 | v6 §12.3 断言 ① | 补注「这个『或』是**规格**，不是笔误」 |
| 5 | v6 本版说明 `Z-8`、`CLAUDE.md` §4 速查表 + §11 反模式两条 | 索引与护栏 |

**求解器一行未改。**

### 3.4 回归怎么钉住的

`tests/property/test_s11_class_scope.py` 四条，其中两条是**反方向**的：

- `test_flying_one_mission_of_the_class_satisfies_the_recurrency` —— 若哪天有人把
  求解器改成「整类都飞」，这条会红；
- `test_missing_the_whole_class_is_still_a_violation` —— 若有人把 C13 的复训分支
  整个删掉，这条会红。**裁定放宽的是粒度，不是要求本身。**

连库版在 `tests/integration/test_crosscheck_live.py::test_s11_scenario_passes_all_three_channels`。

---

## 4. 「恰好」的判定口径（业务方 2026-08-12 确认）

v6 §12.3 只写了「资源恰好够 / 恰好差 1 架次」，没定义怎么判。本窗口提的方案获批：

> **成对定义、互为证明。** 取一个**单调收紧**的资源旋钮，二分找到临界档 `L*`：
> 「恰好够」= 拧到 `L*` 且可解；「恰好差 1」= 拧到 `L*+1` 且不可解。两个场景都进
> 测试集，于是「恰好」由这一对的两个求解结果**直接判定**，不需要额外断言，也不
> 依赖任何人的主观判断。

**差的是 1 格旋钮**（1 名教员 / 1 架飞机 / 1 格容量 / 1 天 / 10 分钟窗口），
**不是「1 个架次」** —— 架次数是求解的产物，不是可以拧的输入；要按架次定义就得先
解析地算出容量上界，而容量上界受 14 条约束交织（跑道密度、空域并发、周转、休息）
没有闭式解。这一点在提问时写清楚了，业务方选了旋钮口径。

标定实测（`datasets/plan_scenarios/v1/calibration.json` 有完整探针序列）：
26 个候选旋钮里 **20 个存在临界档**，6 个「拧到底仍可解」被丢弃 ——
后者本身是个有意思的事实，例如：

- `instructor_off_P01/P02/P03`（单个教员整周不可用）→ 拧到底仍 OPTIMAL，
  与 v6 §12.3 关于旧 I1 的算术一致（单教员周上限 12 ≥ 9 个带飞架次）；
- `window_end_hours` 拧到 `06:00-07:00` 仍可解 —— 因为**不同架次可以时间重叠**
  （约束7 只管同机、约束4 只管同人），60 分钟窗口里排 2 个不同机不同人的架次完全合法。
  这一条纠正了我一开始的直觉，也解释了为什么 I4 必须压到 30 分钟才不可行。

---

## 5. 出口标准逐条实测

### 5.1 属性测试 500 例（Hypothesis 统计原样粘贴）

```
tests/property/test_solver_validator_agreement.py::test_solver_output_always_passes_validator:

  - during generate phase (137.04 seconds):
    - Typical runtimes: ~ 2-301 ms, of which ~ 1-3 ms in data generation
    - 500 passing, 0 failing, and 140 invalid test cases
    - Events:
      * 8.91%, invalid because: failed to satisfy assume() in test_solver_output_always_passes_validator (line 74)

  - Stopped because settings.max_examples=500
```

`assume()` 过滤掉的 8.91% 是求解判 INFEASIBLE 的随机场景（核心不变量只对
「出解」的场景成立，这是 v6 §12.1 原文的 `assume`）。同一批生成器上另有 26 组属性
（三态分离、BLOCKED 出现 0 次、披露率、Tier 0 无欠账、请假/维修/空域关闭/跑道关闭
四类扰动的可见性、S-09、D-1 判定式、复训单人、训练窗、架次号、逐字节可复现、
INFEASIBLE 可诊断），**全部 0 failing**，完整统计见
`reports/M2C_属性测试统计.txt`。

### 5.2 注入违规：14 条规则逐条命中，两条通道判定一致

29 种确定性注入的实测（`main` = 主校验器 HARD 违规集合，`naive` = 第三方实现）：

```
OK  c01_duration_mismatch                  exp=['C01'] main=['C01'] naive=['C01']
OK  c01_before_window                      exp=['C01'] main=['C01'] naive=['C01']
OK  c01_weekday_mismatch                   exp=['C01'] main=['C01'] naive=['C01']
OK  c02_instructor_unavailable             exp=['C02'] main=['C02'] naive=['C02']
OK  c03_solo_a_class_with_instructor       exp=['C03','C05'] main=['C03','C05'] naive=['C03','C05']   ★④
OK  c03_weekly_a_class_missing             exp=['C03'] main=['C03'] naive=['C03']
OK  c03_instructor_as_trainee              exp=['C03','C04'] main=['C03','C04','C05','C13'] naive=同
OK  c04_missing_class_qualification        exp=['C04'] main=['C04'] naive=['C04']
OK  c04_person_time_overlap                exp=['C04','C08'] main=['C04','C08'] naive=同
OK  c05_aircraft_type_not_held             exp=['C05'] main=['C05'] naive=['C05']
OK  c05_dual_flown_solo                    exp=['C03','C05'] main=['C03','C05'] naive=同
OK  c06_airspace_over_capacity             exp=['C06'] main=['C06'] naive=['C06']                     ★①
OK  c06_wrong_airspace                     exp=['C06'] main=['C06'] naive=['C06']
--  c06_unknown_aircraft                   exp=['C06'] main=['C05','C06'] naive=['C06']   ← 见下
OK  c07_turnaround_too_short               exp=['C07'] main=['C07'] naive=['C07']
OK  c07_inside_maintenance                 exp=['C07'] main=['C07'] naive=['C07']
OK  c08_gap_below_minimum                  exp=['C08'] main=['C08'] naive=['C08']
OK  c08_rest_after_two                     exp=['C08'] main=['C08'] naive=['C08']
OK  c09_three_takeoffs_same_runway         exp=['C09'] main=['C09'] naive=['C09']                     ★②
OK  c09_seven_minute_across_runways        exp=['C09'] main=['C09'] naive=['C09']                     ★③
OK  c09_runway_not_serving_type            exp=['C09'] main=['C09'] naive=['C09']
OK  c10_daily_minutes_exceeded             exp=['C10'] main=['C10'] naive=['C10']
OK  c11_weekly_sorties_exceeded            exp=['C11'] main=['C11','C14'] naive=同
OK  c12_person_daily_exceeded              exp=['C12'] main=['C12'] naive=['C12']
OK  c12_aircraft_daily_exceeded            exp=['C12'] main=['C12'] naive=['C12']
OK  c13_blocked_mission_scheduled          exp=['C13'] main=['C13'] naive=['C13']
OK  c13_frequency_window_missed            exp=['C13'] main=['C13'] naive=['C13']
OK  c14_exact_duplicate                    exp=['C14'] main=['C04','C07','C08','C09','C14'] naive=同
OK  c14_req_max_exceeded                   exp=['C14'] main=['C14'] naive=['C14']
```

**28/29 两条通道判定集合完全相等。** 唯一的一处差异 `c06_unknown_aircraft`
（机号不在册）不是规格分歧：主校验器额外报 C05（「查不到机型 → 无法证明机组持有
该机型资质」），naive 只报 C06。**「机号不在册」是引用完整性问题，v6 §4.3 把它归在
闸门2（`check_referential_integrity`），不属于闸门1 的 14 条**，所以对拍的前置条件
就是「方案先过闸门2」。这条注入因此标 `exclusive=False` 且不进对拍，如实记在这里
而不是悄悄改掉某一侧。

**v6 §12.1 点名的四处形态（★①②③④）全部 `exclusive=True`，即「只准命中那一条」**：

```
★③ c09_seven_minute_across_runways（D-2 口径的唯一守门人）
   两次起飞相隔 3 分钟、分属 RWY-7 与 RWY-8
   main: 恰好 1 条 C09 违规；naive: 恰好 1 条 C09 违规
   → 7 分钟若被实现成「按跑道分组」，这里会是 0 条
```

### 5.3 黄金用例 40 个

`tests/golden/test_golden_plans/*.yml` 共 40 份基线，每份含：状态 / 候选数 /
`content_sha256` / 全部架次的每个字段 / 阻塞项 / 欠账 / **主校验器 14 条逐条
`passed`+`checked_items`+违规数** / naive 判定。生成后立即重跑一遍确认稳定
（121 项全绿）。另有两组配套断言：全部用例必须证到 `OPTIMAL`（逐字节比对的前提，
v6 §3.11.1），以及每个用例的解都过双通道。

### 5.4 §6 六条门禁 + 两条静态扫描

```
ruff check .            → All checks passed!
ruff format .           → 14 files reformatted, 146 files left unchanged
mypy backend --strict   → Success: no issues found in 84 source files
bandit -r backend -ll   → No issues identified.（14 498 行）
lint-imports            → Contracts: 3 kept, 0 broken.
pytest -q --cov=backend --cov-fail-under=80
                        → 见 §5.5

bash deploy/scripts/check_no_placeholders.sh → ✅ 无占位符
bash deploy/scripts/check_egress.sh          → ✅ E2 通过 / ✅ E3 通过
```

> ⚠️ **占位符扫描踩到了 M2-A 记过的同一个坑**：新增的
> `tests/unit/test_naive_checker_independence.py` 本身要字面包含 `TODO|FIXME|…`
> 才能检查它们。按 M2-A 留下的逐行豁免机制（`# placeholder-scan: allow`）放行，
> 并且**照它的提醒把 token 抽成单行常量**，避免 formatter 把豁免注释与 token 撕开。

### 5.5 测试总数与覆盖率

```
$ conda run -n schedule alembic upgrade head
$ conda run -n schedule pytest -q --cov=backend --cov-report=term-missing --cov-fail-under=80

  ........................................................................ [100%]
  TOTAL                                     6724    354   2044    214    93%
  Required test coverage of 80% reached. Total coverage: 92.97%
```

**1087 项收集、1086 passed / 1 skipped / 0 failed**（M2-B 交付时 805 项，本窗口
新增 282 项）。skip 的仍是 M2-B 那条 `test_exit_criterion_ripgrep_is_empty`
（`rg` 不在 conda 环境的 PATH 上，同一断言由逐行扫描版本覆盖）。
全局覆盖率 **92.97%**（门槛 80%）。

> **两处踩坑，都记在这里，因为它们是同一类错的两种形态。**
>
> **① `test_naive_checker_pulls_in_no_solver_at_runtime` 跑单文件绿、跑全量红。**
> 它查的是「import naive checker 之后 `sys.modules` 里不该有 `backend.solver`」，
> 但全量 pytest 会话里 `tests/property/` 早就把求解器 import 进来了 —— 那不是
> naive checker 拉的。改成**在干净子进程里查**。这与 M1「本地手工迁移过所以全绿、
> CI 全新库所以全红」是同一个毛病：**验证时的进程状态必须与被验证的命题一致**。
>
> **② `test_infeasible_scenarios_are_diagnosable` 是个会漂的用例。**
> 它原本靠 `assume(status == INFEASIBLE)` 去随机场景里捞不可行的样本，S-11 裁定
> 改了生成器之后不可行率下降，Hypothesis 直接抛 `FailedHealthCheck`
> （`filter_too_much`）。**过滤率会随生成器的任何调整而漂，今天绿明天红。**
> 改成构造性的：把全部空域容量压到 0 → 必然不可行，其余维度照旧随机。
> **不要用 `suppress_health_check` 把它压下去** —— 那只是把「样本几乎全被丢掉」
> 这件事藏起来，属性测试的有效样本数会悄悄归零。

---

## 6. 200 场景测试集（v6 §12.3）

清单于 2026-08-12 经业务方审核批准后跑的全量，**一次跑完 200 个，0 个运行错误、
0 个 `UNKNOWN`**。单场景平均墙钟约 18 秒（含求解 + 三通道校验 +（不可行时）诊断），
全程约 65 分钟。原始结果 `reports/M2C_200场景运行结果.json`，汇总表见
`reports/M2_交叉验收报告.md` §4~§6。

### 6.1 总览

| v6 §0.3 断言 | 口径 | 实测 |
|---|---|---|
| 输出方案 100% 合规 | 出解场景中主校验器 0 条 HARD 违规的比例 | **100.0%**（97/97） |
| 格式校验 100% 通过 | 出解场景通过闸门2 的比例 | **100.0%** |
| 阻塞项 100% 披露 | 出解场景无披露缺口的比例 | **100.0%** |
| 主校验器 vs naive checker 逐条一致 | 判定集合相等的比例 | **100.0%**（200 个场景） |

运行错误 **0** 个 · `UNKNOWN` **0** 个（铁律 8：UNKNOWN 不得与 INFEASIBLE 混为一谈）

| 类别 | 数量 | OPTIMAL | FEASIBLE | INFEASIBLE | UNKNOWN |
|---|---|---|---|---|---|
| baseline | 1 | 1 | 0 | 0 | 0 |
| single | 60 | 42 | 2 | 16 | 0 |
| combo | 60 | 22 | 4 | 34 | 0 |
| boundary | 40 | 12 | 8 | 20 | 0 |
| infeasible | 30 | 0 | 0 | 30 | 0 |
| reschedule | 9 | 6 | 0 | 3 | 0 |

**分歧：无。** 两条独立实现在全部场景上判定逐条一致。

### 6.2 清单构成与三处需要说明的地方

| 类别 | 数量 | 构造方式 |
|---|---|---|
| 基准周 | 1 | 2026-W02 原始数据，零扰动 |
| 单点扰动 | 60 | 人请假 15 / 机维修 15 / 资质到期 14 / 空域容量 14 / 跑道关闭 2 |
| 组合扰动 | 60 | 固定种子（`COMBO_SEED=20260812`）从单点池抽 2~4 个叠加 |
| 边界场景 | 40 | 20 组单调旋钮各出一对（恰好够 / 恰好差 1） |
| 构造不可行 | 30 | I1~I5 各 6 个「沿同一方向更紧」的变体 |
| 局部重排 | 9 | 3 种扰动 × 3 档冻结策略 |

**① 跑道只有 2 个单点构造，配额改成 15/15/14/14/2**（业务方 2026-08-12 确认）。
全场只有 2 条跑道，「1 跑道关闭」这个单点扰动**只存在 2 个互不相同的构造**；凑到
12 个要么重复、要么就不再是「单点」。缺口按「人/机/资质/空域」补齐到 60。
「跑道关闭」这一维并没有因此覆盖不足 —— I5 族 6 个变体 + 边界对
`BD-08 runways_closed` 都在打它。

**② 组合扰动里 34/60 判 INFEASIBLE**，比例偏高。这不是缺陷（v6 §12.3 对组合族
没有规定期望状态），但要记一笔：单点池里本来就有 16/60 不可行，两两叠加之后不可行
是常态。**它的实际作用因此更偏向「诊断链路的压力测试」而不是「带扰动求解」**，
下个窗口若要用这一族评估求解质量，先按状态筛一遍。

**③ 边界场景里 8 个「恰好够」落在 `FEASIBLE` 而非 `OPTIMAL`**（BD-01/02/03/07/11/13/14/18）。
资源被拧到临界档的算例本来就更难证明最优，30 秒预算内只找到可行解。**这不影响
「恰好」的判定**（判据是可解 vs 不可解），但按 v6 §3.11.1，`FEASIBLE` 的解**不保证
逐字节可复现** —— 这 8 个场景不能拿来做黄金基线。

### 6.3 不可行族与最小冲突集

| 场景 | 状态 | 标注冲突源 | 归因规则 | `sat_core_ids` | `structural_ids` | 召回 | 精确 | 升级人工 | 有效提案 |
|---|---|---|---|---|---|---|---|---|---|
| I1-01 | INFEASIBLE | ['C03', 'C04', 'C13'] | ['C03', 'C04', 'C13'] | ['C13_frequency'] | ['C13_frequency'] | 100% | 100% | False | 2 |
| I1-02 | INFEASIBLE | ['C03', 'C04', 'C13'] | ['C03', 'C04', 'C13'] | ['C13_frequency'] | ['C13_frequency'] | 100% | 100% | False | 2 |
| I1-03 | INFEASIBLE | ['C03', 'C04', 'C13'] | ['C03', 'C04', 'C13'] | ['C13_frequency'] | ['C13_frequency'] | 100% | 100% | False | 2 |
| I1-04 | INFEASIBLE | ['C03', 'C04', 'C13'] | ['C03', 'C04', 'C13'] | ['C13_frequency'] | ['C13_frequency'] | 100% | 100% | False | 2 |
| I1-05 | INFEASIBLE | ['C03', 'C04', 'C13'] | ['C03', 'C04', 'C13'] | ['C13_frequency'] | ['C13_frequency'] | 100% | 100% | False | 2 |
| I1-06 | INFEASIBLE | ['C03', 'C04', 'C13'] | ['C03', 'C04', 'C13'] | ['C13_frequency'] | ['C13_frequency'] | 100% | 100% | False | 2 |
| I2-01 | INFEASIBLE | ['C03', 'C06'] | ['C03', 'C06', 'C07', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 50% | True | 0 |
| I2-02 | INFEASIBLE | ['C03', 'C06'] | ['C03', 'C06', 'C07', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 50% | True | 0 |
| I2-03 | INFEASIBLE | ['C03', 'C06'] | ['C03', 'C06', 'C07', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 50% | True | 0 |
| I2-04 | INFEASIBLE | ['C03', 'C06'] | ['C03', 'C06', 'C07', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 50% | True | 0 |
| I2-05 | INFEASIBLE | ['C03', 'C06'] | ['C03', 'C06', 'C07', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 50% | True | 0 |
| I2-06 | INFEASIBLE | ['C03', 'C06'] | ['C03', 'C06', 'C07', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 50% | True | 0 |
| I3-01 | INFEASIBLE | ['C06', 'C13'] | ['C06', 'C13'] | ['C13_frequency'] | ['C13_frequency'] | 100% | 100% | False | 2 |
| I3-02 | INFEASIBLE | ['C06', 'C13'] | ['C06', 'C13'] | ['C13_frequency'] | ['C13_frequency'] | 100% | 100% | False | 2 |
| I3-03 | INFEASIBLE | ['C06', 'C13'] | ['C06', 'C13'] | ['C13_frequency'] | ['C13_frequency'] | 100% | 100% | False | 2 |
| I3-04 | INFEASIBLE | ['C06', 'C13'] | ['C06', 'C13'] | ['C13_frequency'] | ['C13_frequency'] | 100% | 100% | False | 2 |
| I3-05 | INFEASIBLE | ['C06', 'C13'] | ['C06', 'C13'] | ['C13_frequency'] | ['C13_frequency'] | 100% | 100% | False | 2 |
| I3-06 | INFEASIBLE | ['C06', 'C13'] | ['C03', 'C06', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 67% | True | 0 |
| I4-01 | INFEASIBLE | ['C01', 'C13'] | ['C01', 'C13'] | ['C01_window', 'C13_frequency'] | [] | 100% | 100% | False | 2 |
| I4-02 | INFEASIBLE | ['C01', 'C13'] | ['C01', 'C13'] | ['C01_window', 'C13_frequency'] | [] | 100% | 100% | True | 0 |
| I4-03 | INFEASIBLE | ['C01', 'C13'] | ['C01', 'C13'] | ['C01_window', 'C13_frequency'] | [] | 100% | 100% | True | 0 |
| I4-04 | INFEASIBLE | ['C01', 'C13'] | ['C01', 'C13'] | ['C01_window', 'C13_frequency'] | [] | 100% | 100% | True | 0 |
| I4-05 | INFEASIBLE | ['C01', 'C13'] | ['C01', 'C13'] | ['C01_window', 'C13_frequency'] | [] | 100% | 100% | True | 0 |
| I4-06 | INFEASIBLE | ['C01', 'C13'] | ['C01', 'C13'] | ['C01_window', 'C13_frequency'] | [] | 100% | 100% | True | 0 |
| I5-01 | INFEASIBLE | ['C03', 'C09', 'C13'] | ['C03', 'C06', 'C07', 'C09', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 60% | True | 0 |
| I5-02 | INFEASIBLE | ['C03', 'C09', 'C13'] | ['C03', 'C06', 'C07', 'C09', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 60% | True | 0 |
| I5-03 | INFEASIBLE | ['C03', 'C09', 'C13'] | ['C03', 'C06', 'C07', 'C09', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 60% | True | 0 |
| I5-04 | INFEASIBLE | ['C03', 'C09', 'C13'] | ['C03', 'C06', 'C07', 'C09', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 60% | True | 0 |
| I5-05 | INFEASIBLE | ['C03', 'C09', 'C13'] | ['C03', 'C06', 'C07', 'C09', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 60% | True | 0 |
| I5-06 | INFEASIBLE | ['C03', 'C09', 'C13'] | ['C03', 'C06', 'C07', 'C09', 'C13'] | ['C13_frequency'] | ['C03_weekly', 'C13_frequency'] | 100% | 60% | True | 0 |

**判定率**：30/30 判为 `INFEASIBLE`，`UNKNOWN` **0** 个
**冲突源召回率**：micro **100.0%** · macro **100.0%**（目标 100%）
**冲突源精确率**：micro **74.2%** · macro **80.9%**（目标 ≥60%）

> 标注来源：v6 §12.3 表格的「预期最小冲突集」列，**不是本窗口自己编的**。
> 「归因规则」= `Diagnosis.conflicts` 各项 `rule_ids` 的并集，它同时包含
> `sat_core_ids`（CP-SAT 的极小 core）与 `structural_ids`（结构性不可满足组），
> 外加归因阶段从 `DropReason` 补回来的根因规则（v6 §3.9，M2-A §3.10）。
- **I1**（三名教员全部整周不可用）：6 个变体，状态 ['INFEASIBLE']，归因规则并集 ['C03', 'C04', 'C13']
- **I2**（服务学员机型的飞机全部整周维护）：6 个变体，状态 ['INFEASIBLE']，归因规则并集 ['C03', 'C06', 'C07', 'C13']
- **I3**（承载 C 类课目的空域整周容量降为 0）：6 个变体，状态 ['INFEASIBLE']，归因规则并集 ['C03', 'C06', 'C13']
- **I4**（训练窗压缩至 06:00-06:30）：6 个变体，状态 ['INFEASIBLE']，归因规则并集 ['C01', 'C13']
- **I5**（服务学员机型的跑道全部关闭）：6 个变体，状态 ['INFEASIBLE']，归因规则并集 ['C03', 'C06', 'C07', 'C09', 'C13']

**出口标准逐条核对：**

| 出口标准 | 结果 |
|---|---|
| I1~I5 全部 30 个 100% 判定为 `INFEASIBLE`，无一 `UNKNOWN` | ✅ 30/30，`UNKNOWN` 0 个 |
| 最小冲突集召回率 = 100% | ✅ micro 100% · macro 100% |
| 精确率 ≥60% | ✅ micro 74.2% · macro 80.9% |
| **I4 的冲突集中必须出现约束1（训练窗）** | ✅ 6/6 变体的 `sat_core_ids` 里直接有 `C01_window` |
| **I5 的冲突集中必须出现约束9（跑道密度）** | ✅ 6/6 变体的归因规则含 `C09` |
| I2 / I5 的合格输出是「升级人工」 | ✅ 两族 12 个变体全部 `escalate=True` 且 `useful_proposals == 0` |
| 冲突集要同时看 `sat_core_ids` 与 `structural_ids` | ✅ 见上表两列 |

**三点观察，都值得下个窗口知道：**

1. **`sat_core_ids` 几乎恒为 `['C13_frequency']` 一组。** CP-SAT 的 core 是**极小**的
   —— 只要一组自相矛盾就够了。召回率 100% **完全靠 `structural_ids` 与归因阶段补上**
   （M2-A §3.10 就是为这件事写的）。**删掉结构性判定，召回率会从 100% 掉到 33% 左右**
   （I1/I2/I3/I5 标注的 C03/C04/C06/C09 全在 core 之外）。
2. **I4 是唯一一族 `sat_core_ids` 里直接含根因的**（`C01_window`），因为训练窗压缩是
   通过 gated 上下界进模型的（M2-A §3.12），assumption literal 沾得上。
3. **I2 的精确率 50% 是标注口径造成的，不是冲突集报多了。** 归因给出
   `C03/C06/C07/C13`，而 v6 §12.3 的「预期最小冲突集」列只写了「约束6 × 约束3」。
   多出来的 C07（机队全维护 = 维护时段条款）与 C13（频率要求无法满足）**都是真实
   根因**。这里**没有**去调标注让数字好看 —— 标注一律照抄 v6，精确率就照实报。

### 6.4 边界对（「恰好」的成对证明）

| 对 | 旋钮 | 恰好够 | 恰好差 1 | 「恰好」成立 |
|---|---|---|---|---|
| BD-01 | `instructors_out` | FEASIBLE（14 架次） | INFEASIBLE | ✅ |
| BD-02 | `student_fleet_down` | FEASIBLE（14 架次） | INFEASIBLE | ✅ |
| BD-03 | `fleet_down` | FEASIBLE（14 架次） | INFEASIBLE | ✅ |
| BD-04 | `airspace_IFR` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |
| BD-05 | `airspace_RT1` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |
| BD-06 | `airspace_RT2` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |
| BD-07 | `airspace_SAB` | FEASIBLE（14 架次） | INFEASIBLE | ✅ |
| BD-08 | `runways_closed` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |
| BD-09 | `everyone_off_days` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |
| BD-10 | `students_off_days` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |
| BD-11 | `instructors_off_days` | FEASIBLE（14 架次） | INFEASIBLE | ✅ |
| BD-12 | `student_off_P05` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |
| BD-13 | `student_off_P06` | FEASIBLE（14 架次） | INFEASIBLE | ✅ |
| BD-14 | `student_off_P07` | FEASIBLE（14 架次） | INFEASIBLE | ✅ |
| BD-15 | `student_off_P08` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |
| BD-16 | `all_airspaces_down` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |
| BD-17 | `student_fleet_days` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |
| BD-18 | `window_end_half_hours` | FEASIBLE（14 架次） | INFEASIBLE | ✅ |
| BD-19 | `window_end_ten_min` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |
| BD-20 | `student_fleet_days_front` | OPTIMAL（14 架次） | INFEASIBLE | ✅ |

**20/20 组成立。**

---

## 7. 已知限制

1. **naive checker 的独立性是打了折的**，折扣与理由见 §2。**验收时请按 §2 的口径
   陈述，不要简单说成「第三方盲写实现」。**
2. **C13 的对拍从 S-11 裁定之后只剩回归价值。** 两侧现在都按同一条裁定实现，
   这一条上再出现分歧只可能是实现 bug，不可能是规格理解分歧。其余 13 条不受影响。
3. **200 场景全量跑一次约 65 分钟，进不了 CI。** 它是一条 CLI 批任务
   （`python -m tests.scenarios.run_suite run`），产物落 `reports/`。CI 里跑的是
   属性测试 + 黄金用例 + 连库专项，覆盖同一批语义但规模小得多。
   **下次改动 solver 或 validator 之后，全量必须重跑一遍**，不能只看 CI 绿。
4. **组合扰动族 34/60 判 INFEASIBLE**（§6.2 ②）。要用这一族评估「带扰动的求解
   质量」得先按状态筛。
5. **8 个「恰好够」的边界场景停在 `FEASIBLE`**（§6.2 ③），按 v6 §3.11.1 它们的解
   不保证逐字节可复现，不能拿来做黄金基线。
6. **人工抽检（三重验证的第 3 条）尚未执行。** 清单已按固定种子生成
   （`M2_交叉验收报告.md` §6），需要业务方逐条核对。**在它完成之前，v6 §12.3 的
   「三重独立验证」严格说只完成了两重** —— 这一条不要在验收报告里含糊过去。
7. **黄金用例是合成场景，不是基准周。** 基准周单次求解 20 秒，40 个用例进不了常规
   pytest。基准周的逐字节可复现由 M2-A 的 `test_baseline_is_byte_reproducible` 覆盖。
8. **`arbitrary_scenario()` 的世界最多 3~6 人 / 3 机 / 4 课目。** 它压根碰不到基准周
   那种 2276 候选的规模，**属性测试全绿不代表大规模下两侧也一致**。大规模那一侧由
   200 场景（全部基于基准快照）覆盖，两者互补，缺一不可。

---

## 8. 下一个窗口的前置条件与接口约定

### 给 M3（报告与 Excel）

- **Sheet 4 区块 2 的 14 行直接来自 `ValidationReport.results`**（M2-B §8 已约定）。
  本窗口补一条：**C13 的 `checked_items` 现在把 S-11 复训组也计进去了**
  （每组算 `1 + 窗口数`），区块 2 的数字会比 M2-B 报告里那张表大一点，属正常。
- **区块 4「缺失先修」列**逐字取 `BlockedItem.reason`，措辞已由求解侧固化为
  `「<课目编号> 未完成」`，多门用 `、` 连接。基准周的 7 条实测见
  `tests/integration/test_crosscheck_live.py::BASELINE_BLOCKED_EXPECTED`，
  **那是一份可以直接拿来对的期望值**。
- **区块 6 的「授权改写声明」**取 `report.all_notes()`。S-11 开关为 on 时恒有第一条
  声明；真排出复训架次时还会多一条「S-11 生效实例」。

### 给 W7 / M4-B（编排）

- `validate` 节点 = `run_all_checks` + `verify_format`，两者都不碰 LLM、不读 Skill。
- **判定分歧的处置流程本窗口已经走通一遍**（§3），下次再遇到照同一套来：
  先定位到**具体条款**，再看是哪一侧读错、还是文档自相矛盾，**最后才动代码**。

### 给 M8（加固）与 M9（实验）

- `tests/scenarios/` 那套（catalog / calibrate / runner / report）是**通用的**：
  换一份快照就能生成那份数据的 200 个场景（实体全部从 PG 读，没有写死编号）。
  M9-A 要造更大的数据集时直接复用，不要另起一套。
- `training_progress` 主键不含 `snapshot_id`（M2-B §6.3 记过的那堵墙）本窗口同样撞到：
  **200 场景全量跑与任何连库测试不能并发**，否则 `compile_spec` 的物化互相覆盖。
  现在靠「串行跑」绕开，M8 加固时建议给主键补上 `snapshot_id`。

### ⚠️ 三个会咬人的坑

1. **`sat_core_ids` 单独用会让召回率掉到三分之一**（§6.3 观察 1）。看冲突集必须
   `sat_core_ids ∪ structural_ids ∪ 归因规则` 三者一起看。
2. **改 `check_c13` 之前先看 §3。** 那段 `recurrent_groups` 看起来像可以摊平回逐行
   循环的冗余结构，它不是 —— 摊平就等于把 FTS-3003 重新引回来。
3. **属性测试出反例时不要先改代码。** 反例就是 FTS-3003 的定义。本窗口的 `Z-8`
   正是「先定位、后裁定、最后才改一侧」这个顺序换来的；反过来先改代码抹平，
   得到的是一个绿色的、但两侧都错的系统。
