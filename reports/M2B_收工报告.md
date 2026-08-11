# M2-B 收工报告 · 独立校验器

**窗口** M2-B · **日期** 2026-08-11 · **分支** `feat/m2b-validator`
**依据** `docs/SPEC_DECISIONS.md`、`docs/M0_规格锁定.md`、v6 §1.1~§1.3 / §3.2 / §3.5 / §4 全章 / 附录 B / 附录 C、`rules/ruleset_v1.3.yaml`

---

## 0. 一句话结论

`backend/validator/` 四个模块（`checks.py` / `schema.py` / `workbook.py` / `context.py`）全部落地，
14 条 check 逐条实现、无一遗漏；**三道闸门都用「篡改后必须通不过」的用例验过**；
手工构造的 14 架次合规样本 14 条全过，`image 4.png` 的四类已知违规逐条拓出；
§6 六条门禁 + 两条静态扫描全绿，校验器包覆盖率 **93%**（全局 92.82%）。

**隔离声明**：本窗口自始至终**没有打开过 `backend/solver/` 下任何文件**，
也没有跑过求解器拿解 —— 测试用的解全部是本窗口按 v6 §3.2 手工排出来的（见 §5.1）。
唯一读过的 M2-A 材料是 `reports/M2A_收工报告.md` 的 **§9.1「给 M2-B」那一节**
（口径对齐要求，无编码细节）。

---

## 1. 交付物

| 文件 | 行数 | 内容 |
|---|---|---|
| `backend/validator/checks.py` | 1170 | 14 个纯函数 `check_c01`~`check_c14` + `run_all_checks` |
| `backend/validator/context.py` | 492 | 校验器**自己**的只读事实视图 + PG 装配（`load_context` / `context_from_rows`） |
| `backend/validator/schema.py` | 330 | 闸门2：Schema 层 + 外键 + **三表交叉一致性** |
| `backend/validator/workbook.py` | 800 | 闸门3：xlsx 回读契约 + 反解 + `deep_diff` |
| `backend/validator/__init__.py` | 54 | 三道闸门的公开入口 |
| `tests/fixtures/validator_facts.py` | 600 | 手工事实（v6 §1.3）+ 手工合规样本 + image 4 违规样本 |
| `tests/fixtures/workbook_builder.py` | 260 | 按回读契约写出 xlsx（供闸门3 测试用；**不是 M3 的正式写出模块**） |
| `tests/unit/test_validator_checks.py` | 900 | 69 个用例：每条规则 1 合规 + ≥2 违规 |
| `tests/unit/test_validator_schema.py` | 280 | 26 个用例 |
| `tests/unit/test_validator_workbook.py` | 300 | 21 个用例（其中 13 个是「篡改后必须失败」） |
| `tests/unit/test_validator_context.py` | 155 | 13 个用例 |
| `tests/guardrail/test_validator_isolation.py` | 100 | 校验器侧的隔离护栏（镜像 `test_solver_isolation.py`） |
| `tests/integration/test_validator_context_live.py` | 151 | 直连裸装 PG 的装配路径 |

**改动的既有文件（两处，都需要业务方过目，见 §7）**：
`backend/schemas/validation.py`（`CheckResult` 新增可选 `notes` 字段）、
`requirements.txt`（新增 `types-openpyxl`）。

---

## 2. v6 §3.2 对照表 · 校验器侧逐条实现位置与判定依据

| 规则 | 实现位置 | 判定依据（**语义**，不是求解器的编码形态） |
|---|---|---|
| **1** 时间一致性 | `checks.py::check_c01` | 逐条重算 `land − takeoff == mission.duration_minutes`；比对训练窗（取 `ruleset.window_start/end` = 06:00-18:00）；`cross_day_allowed=false` 时断言 `land > takeoff`；**星期列必须与日期自洽** |
| **2** 人员可用性 | `check_c02` | 遍历 架次×机组：查 `person_unavailability`；查 `person_qualifications.expiry_date`（`expiry_inclusive` → **到期日当日仍可**）。**S-11**：`identity ∈ semantics.s11_identities` 时不报违规，改为要求该架次 `is_recurrent=True` 且角色为「复训」，并在 `CheckResult.notes` 写入授权改写声明 |
| **3** 角色配置 | `check_c03` | ① 双人架次教员数==1 且学员数==1；单人架次角色 ∈ {单飞, 复训}；② 角色与身份自洽（成熟飞行员不得占教员岗）；③ 期望人数按 §3.1.1 判定式 `需带飞 = (mission.dual_required) ∧ (identity==学员)` **独立重算**；④ 每周必飞：**类别由课目表 `weekly_required` 推出**（基准数据 = A 类），A-1+A-2 **合并计数**（S-02），**遍历全部学员不论完成状态**（S-13） |
| **4** 资质匹配/岗位互斥 | `check_c04` | 查 `person_qualifications` 是否含课目所属类别；教员岗必须是教员身份；**教员不得作为受训人出现**（S-09）；同一人全部架次做**区间两两相交**检测（半开 `[takeoff, landing)`） |
| **5** 机型/机组编成 | `check_c05` | 机组每人持 `aircraft.aircraft_type` 资质；`len(crew) ≤ aircraft.seats`；按 §3.1.1 判定式重算期望人数再比对 |
| **6** 资源有效性与容量 | `check_c06` | ① 机号 ∈ 在册；机型 ∈ `mission_aircraft_types`；课目 ∈ `aircraft_mission_capability`；架次的 `airspace_id` 必须等于课目绑定空域；② **扫描线**：按 (空域, 日) 分组，起飞 +1 / 着陆 −1，排序键 `(时刻, delta)` 使 **−1 先于 +1**（同刻「先减后加」），前缀最大值 ≤ `airspaces.capacity`（**容量从表读，不写死**） |
| **7** 飞机冲突+周转 | `check_c07` | 按机分组、按 (日, 起飞) 排序，逐对 `takeoff[b] − landing[a] ≥ aircraft.turnaround_minutes`（**逐机一列，不是机型常量**，S-06 从着陆算到起飞）；维护窗与架次半开区间相交检测 |
| **8** 人员冲突+休息 | `check_c08` | 按 (人, 日) 分组排序：相邻 `gap ≥ min_gap_min(10)`；当日第 `rest_after_n(2)` 架次之后的每一个，与前一架次的 `gap ≥ rest_min(30)`。**跨日不累计**（S-07） |
| **9** 起降密度 | `check_c09` | ⓪ `runway_id` 在册且 `aircraft_type ∈ runway.aircraft_types`；① **按 (日, 跑道)** 分组，半开 `[t, t+20)` 计数 ≤2（S-04）；② **按日全场**排序，相邻起飞差 ≥7（**D-2：不分跑道**）。两段**分开的循环** |
| **10** 每日时长上限 | `check_c10` | 按 (人, 日) 求和 `landing − takeoff`，与 `ruleset.daily_minute_cap(identity)`（学员 240 / 其余 480）比对 |
| **11** 每周架次上限 | `check_c11` | 按人计数，与 `weekly_sortie_cap(identity)`（学员 10 / 其余 12）比对 |
| **12** 每日架次上限 | `check_c12` | (人, 日) ≤3；(机, 日) ≤6 |
| **13** 任务完成度 | `check_c13` | 从 PG 读 `training_progress`：① 先修未满足 → 断言该 (人, 课目) **出现次数 = 0**；② 已完成课目跳过（S-03，**S-11 复训除外**）；③ 管辖对象 = 学员 ∪ 复训中的成熟飞行员；④ 窗口 `F = freq_days`（复训取 `recurrent_window_days=7`，起算日 = `recurrent_since`）；⑤ 截止日 **D-4 通式** `deadline = max(origin, (last_done − week_monday).days + F)`，**S-12** 锚点缺失时 `deadline = origin + F − 1` 且不计欠账；⑥ `deadline > 6` 或窗口整体落在本周之外 → **本周不构成约束**，显式跳过 |
| **14** 任务唯一性 | `check_c14` | ① 除架次号外逐字段相同 → 完全重复；② 按 (人, 课目) 计**受训人**出现次数 ≤ `req_max_for(freq_days) = ceil(7/freq_days)`（A 类 3、其余 1，独立重算） |

格式校验三层（v6 §4.3）：**Schema 层** `schema.py::validate_plan_schema`（编号 pattern 直接 import
`backend.schemas.plan` 的常量，不另抄）· **业务完整性层** `check_referential_integrity` +
`check_cross_table_consistency` · **产物层** `workbook.py::verify_workbook`。

---

## 3. 关键决策与理由（**踩过的坑都在这里**）

### 3.1 校验器自带一份 `ValidationContext`，自己读 PG

v6 §4.2 的签名是 `check_c07(plan, ctx: DataContext)`，但求解侧的数据装配在
`backend/solver/data.py` / `nodes/compile_spec.py`（后者会 import 前者）。校验器**引用不得**，
既因为 import-linter 的禁令一，更因为一旦共用，「求解器把某一行数据读错了」这类错误就同时
逃过了两道闸门。于是 `validator/context.py` 从 `backend.models` 的事实表**自己装配**一份，
两边各读各的。共用的只有三样：`schemas/plan`（数据形状）、`core/ruleset`（YAML → 对象，
不表达约束）、以及 PG 里的同一批事实。

### 3.2 ⚠️ 空域并发的同刻口径：**先减后加**（M2-A §9.1 的对齐项 1）

事件排序键取 `(时刻, delta)`，`-1 < +1` 保证同一分钟内**着陆先于起飞**被处理。
于是「06:35 着陆 + 06:35 起飞」并发计为 1，与求解侧的半开区间 `[start, start+dur)` 一致。
写成 `(时刻, +1 先)` 会在 IFR/RT1/RT2/RNG 这四个容量=1 的空域上直接产出 FTS-3003。
**这条有专门的双向用例**（`test_c06_same_minute_landing_then_takeoff_is_not_concurrent`：
同刻通过、提前一分钟就必须报违规）。

> v6 §4.2 的参考实现写的是 `sorted([(t, +1, s) …] + [(t, −1, s) …])`。照抄会有两个问题：
> ① 元组第三位是 `Sortie`（Pydantic 模型），同刻同 delta 时会拿它做比较 → `TypeError`；
> ② 语义上确实是 −1 先，但那是靠元组比较的巧合，不是显式意图。实现改成显式的
> `key=lambda e: (时刻, delta, seq)`，`seq` 是稳定序号。

### 3.3 ⚠️ 7 分钟间隔写成**两段独立的循环**，而且刻意不合并

D-2 的口径（20 分钟按跑道、7 分钟全场）在代码里最容易被"顺手优化"成一个按跑道的循环。
`check_c09` 里两段循环各自带注释说明「不要合并」，并有一个专测用例
`test_c09_seven_minute_separation_is_airport_wide_not_per_runway`：两次起飞相隔 3 分钟、
**分属两条不同跑道**，必须命中且**只**命中这一条。配套的反向用例
（第三次起飞挪到另一条跑道后 20 分钟窗口就不再超）确保两段没有被写成同一段。

### 3.4 `passed = 没有 HARD 违规`，SOFT 只在「已松弛 + 已披露」时出现

`CheckResult.passed` 若定义成「没有任何违规」，那么一份 Tier 1 合法松弛过的方案会被闸门1
拦下 —— 而 v6 §3.10 明确松弛只发生在 R1/R2 且欠账 100% 显式披露。实现口径：

- 松弛档位放宽了该条（查 `ruleset.ladder_step(tier).relaxes`）**且**缺口已在 `plan.debts`
  里如实披露 → `severity="SOFT"`，报告里看得见、不拦方案；
- **未披露的欠账仍是 HARD**（`test_c03_undisclosed_shortfall_stays_hard_even_when_relaxed`）；
- R0 的九条（1/2/4/5/6/7/8/9/14）恒为 HARD，松弛阶梯从不放宽它们。

### 3.5 约束14 只数**受训人**

约束14 原文是「同一人员与同一课目子任务的组合……不得重复安排」。若把教员岗也计入，
一名教员一周内带三个学员飞 missionC-1 就会超 `req_max=1` —— 与约束3 的编成要求直接打架。
故计数只算角色 ∈ {学员, 单飞, 复训} 的那个人。同理，C13 的「本周排了几次」也只从受训人视角统计。

### 3.6 约束3 的「每周必飞」类别**从数据推**，不写死「A 类」

`ctx.weekly_required_classes()` 取自 `missions.weekly_required`（基准数据里只有 A-1/A-2 带
「（每周必飞）」标记），A-1/A-2 合并计数由 `missions_of_class("A")` 得到。
`test_c03_weekly_class_is_data_driven_not_hardcoded_a` 把 F-1 也标成每周必飞，断言校验器
跟着数据走 —— 这是 CLAUDE.md §11「类别 A~H 不许写成常量」的可执行形态。

### 3.7 C13 的两个边界，写错任何一个基准周都会假性不合规

1. **`deadline > 6` 必须显式跳过。** G/H 类 `freq_days=14`、锚点缺失时 `deadline = 13`，
   那是「本周不必飞」，不是「必须在第 13 天前飞」。
2. **S-11 复训窗口自 `recurrent_since` 起算。** 刘斌 01-08 起算、窗口 [01-08, 01-14] 跨出
   W02 → 本周不强制（v6 §1.2.4）。实现上 `origin_day = (recurrent_since − week_start).days`，
   周内窗口只取 `range(max(0, origin), 7 − F + 1)`，于是 `origin=3, F=7` 时窗口集合为空。
   把 `recurrent_since` 提前到 01-01 后窗口右端落在第 2 天，校验器就要求本周必须排 ——
   两个方向都有用例。

### 3.8 违规样本必须能绕过 Pydantic 契约层

闸门1 与闸门2 是**两道独立的闸门**，校验器不能依赖 Pydantic 兜底。但 Pydantic 2 对嵌套的
模型实例**会重跑** `mode="after"` 校验器 —— 于是 `Sortie.model_construct()` 造出来的违规
架次，一装进 `SchedulePlan(...)` 就在闸门2 被拦掉，根本到不了 `checks.py`。
`make_plan(validate=False)` 因此对外壳也走 `model_construct`。**这不是绕过校验，这是让
闸门1 能被单独测到。**

### 3.9 `checked_items` 逐条如实计数，并有「不许写死常数」的回归

`test_every_check_reports_real_checked_items` 做两件事：断言 14 条没有一条是 0；
再把架次砍半，断言逐架次计数的那几条**跟着变小**。写死常数能骗过第一个断言，骗不过第二个。
（C08 一开始就栽在第一个断言上：合规样本里没人一天飞两次，"相邻对"为 0 —— 现在把
(人,日) 分组数也计入，语义是「逐组查过了」。）

### 3.10 闸门3 的回读契约由本窗口定义，M3 照它写

M3 的 Excel 写出还没有，但「回读 → 深度相等」这一层必须现在就能测通。做法是把**反解所需的
版式约定**固化成 `workbook.py` 的模块级常量（工作表名与顺序、三张表的表头、Sheet 4 七个区块的
标题与列名、机组拼接格式、角色后缀），M3 直接 import 它们来写。测试用的
`tests/fixtures/workbook_builder.py` 是这份契约的一个完整实现（不含字体底纹列宽）。

**一处对 v6 §10.4 的扩充需要业务方过目**：区块1 增加一行「**语义开关**」，
序列化为 `S-01=all_missions_completed；S-02=class_level；…`。
理由与当初新增区块7 完全同构：`semantics_switches` 参与 `content_sha256`（v6 附录 B 脚注），
不落表就反解不回来，§4.3 的「深度相等」断言就不可能成立。Sheet 4 本身「无版式基准可依，
由 §10 定义」（§10.5），故这是扩充而非偏离版式。**未改 `docs/`**（CLAUDE.md §7 第 8 条）。

### 3.11 Sheet 1~3 只有姓名，`person_id` 靠人员表反查

版式基准里机组列写的是 `孙军教，陈伟学`，没有编号。`verify_workbook(path, plan, ctx=...)`
用 `ctx` 的人员表把姓名映射回编号；**重名时不猜，直接报成回读错误**
（`test_duplicate_person_names_are_reported_not_guessed`）。不传 `ctx` 时退化为用 `plan`
自带的机组姓名建映射 —— 那只够做格式比对，不能替代人员表。

---

## 4. 出口标准逐条实测

### 4.1 逐条对照

| # | 出口标准 | 结果 |
|---|---|---|
| 1 | 14 个 check 全部实现，无遗漏、无 TODO | ✅ `ALL_CHECKS` 长度 14，`report.missing_rules() == []`；`check_no_placeholders.sh` 退出码 0 |
| 2 | `rg -n "ortools\|from backend.solver\|import solver" backend/validator/` 输出为空 | ✅ 见 §4.2 |
| 3 | `lint-imports` 通过 | ✅ 3 kept / 0 broken |
| 4 | 每条规则合规样本通过、违规样本被精确定位 | ✅ 69 个用例全绿，见 §4.3 |
| 5 | **C09 跨跑道 7 分钟被判违规** | ✅ 见 §4.4 |
| 6 | **C03 的「A 类带教员」被判违规** | ✅ 见 §4.5 |
| 7 | **C02 的 S-11 用例不报违规且标注授权改写** | ✅ 见 §4.6 |
| 8 | image 4 的四类违规全部拓出 | ✅ 见 §4.7 |
| 9 | 三表交叉一致性有专门测试 | ✅ 5 个用例（字段漂移 / 缺行 / 分组键错位 / 机组人数不符 / 自洽） |
| 10 | 校验器单测覆盖率 ≥90% | ✅ **93%**（checks 93 / context 100 / schema 93 / workbook 91） |
| 11 | §6 六条门禁全绿 | ✅ 见 §4.8 |

### 4.2 隔离验证（铁律 2）

```
$ rg -n "ortools|from backend.solver|import solver" backend/validator/
（无输出，退出码 1）

$ conda run -n schedule lint-imports
禁令一 validator 不得 import solver KEPT
禁令二 求解链路不得 import skills_loader KEPT
禁令三 egress 收口于 core.http KEPT
Contracts: 3 kept, 0 broken.
```

`tests/guardrail/test_validator_isolation.py` 另外断言：导入 `backend.validator` 的四个模块
之后，`sys.modules` 里**没有** `ortools` 与 `backend.solver`（间接依赖也算）。

### 4.3 合规样本 14 条全过（`checked_items` 原样粘贴）

```
all_passed: True   total_checked_items: 446
C01 时间一致性           passed=True checked=14 v=0
C02 人员可用性           passed=True checked=23 v=0
C03 角色配置             passed=True checked=18 v=0
C04 资质匹配与岗位互斥   passed=True checked=59 v=0
C05 机型与机组编成       passed=True checked=37 v=0
C06 资源有效性与容量     passed=True checked=42 v=0
C07 飞机排期冲突与周转   passed=True checked=12 v=0
C08 人员冲突与休息       passed=True checked=23 v=0
C09 起降密度限制         passed=True checked=35 v=0
C10 每日飞行时长上限     passed=True checked=23 v=0
C11 每周架次上限         passed=True checked=6  v=0
C12 每日架次上限         passed=True checked=37 v=0
C13 任务完成度           passed=True checked=90 v=0
C14 任务唯一性           passed=True checked=27 v=0
NOTES: ['S-11：成熟飞行员到期资质转复训（自到期次日起按 7 天滑窗强制安排），系对 rules.pdf 约束2
        字面语义的**业务方授权改写**（2026-08-06 裁定），非校验器漏判。']
```

### 4.4 ★ C09「跨跑道 7 分钟」（D-2 口径的唯一守门人）

两次起飞相隔 3 分钟、分属 RWY-1 与 RWY-2：

```
passed=False checked_items=5
  rule_id=C09 severity=HARD subjects=['2026-01-11', 'S000035', 'S000036']
  detail=2026-01-11 相邻起飞间隔 3 分钟 < 7 分钟（**全场口径**，S000035@RWY-1 06:00 → S000036@RWY-2 06:03）
  fix_hint=将 S000036 起飞推迟至 06:07
```

用例还断言 `len(result.violations) == 1` —— 若 7 分钟被误实现成按跑道分组，这条就是 0 条违规。

### 4.5 ★ C03/C05「A 类带教员」（D-1 反向验证）

把何超的 missionA-2 单飞硬塞一个教员：

```
C03 ['S000005', 'P08'] S000005（missionA-2 带飞=否，受训人身份=学员）按 §3.1.1 判定式应为 1 人机组，实际 2 人：['孙军', '何超']
C05 ['S000005', 'P08'] S000005 机组编成应为 1 人（missionA-2 带飞=否，受训人身份 学员），实际 2 人
```

### 4.6 ★ C02 的 S-11（刘斌 2026-01-08 飞 missionC-1）

```
passed=True  violations=0  checked_items=24
NOTE: S-11：成熟飞行员到期资质转复训（自到期次日起按 7 天滑窗强制安排），系对 rules.pdf 约束2
      字面语义的**业务方授权改写**（2026-08-06 裁定），非校验器漏判。
NOTE: S-11 生效实例：刘斌(P04) C 类于 2026-01-07 到期，S000015(2026-01-08) 按复训安排。
```

配套断言：① 只要 S-11 开关为 on，**即使本周没排复训架次**，第一条声明也必须在场
（v6 §10.4 区块6 强制项）；② 到期后**不标** `is_recurrent` 的架次仍是违规
（S-11 是「转复训」，不是「随便飞」）；③ 学员的到期资质按约束2 **字面**执行，到期日当日放行、
次日起拦下。

### 4.7 image 4 已知违规样例（v6 §1.2.2 的四类逐条拓出）

```
[C06] 资源有效性与容量  checked=21 violations=4
    - AC84（JL-9）机型不适配 missionA-1（要求 JL-8）          ← §1.2.2 第①类
    - AC84 的适配课目列表不含 missionA-1
    - AC49（JL-8）机型不适配 missionD-1（要求 JL-9）
    - AC49 的适配课目列表不含 missionD-1
[C07] 飞机排期冲突与周转 checked=5 violations=1
    - AC84（JL-9）2026-01-05 相邻架次间隔 10 分钟 < 周转要求 40 分钟
      （S000101 着陆 06:29 → S000102 起飞 06:39）              ← §1.2.2 第②类
[C09] 起降密度限制      checked=19 violations=1
    - 2026-01-06 相邻起飞间隔 0 分钟 < 7 分钟（**全场口径**，
      S000104@RWY-1 06:00 → S000105@RWY-1 06:00）              ← §1.2.2 第③类
[C04] 资质匹配与岗位互斥 checked=16 violations=2
    - 岗位互斥：刘斌(P04) 身份为「成熟飞行员」，占据了 S000103 的教员岗   ← §1.2.2 第④类
    - 岗位互斥：刘斌(P04) 身份为「成熟飞行员」，占据了 S000107 的教员岗
[C03] 角色配置          checked=11 violations=9（含上面第④类的两条 + 7 条编成违规）
[C01] 时间一致性        checked=7  violations=1
    - S000101 时长 29 分钟 ≠ missionA-1 标准时长 30 分钟（图上 06:00→06:29）
[C05] 机型与机组编成    checked=18 violations=5
[C13] 任务完成度        checked=90 violations=12（两天的裁剪样本，频率窗口自然不满足）
```

**fixture 的裁剪说明**（写在 `validator_facts.py::IMAGE4_SORTIES` 的注释里）：

- 只取周一、周二两组，**课目号能映射到实体表**的 7 条；
- 周二 06:00 的 AC34 那行是 `missionD-2` —— 课目表里没有这门课，整行删除。
  连带后果：v6 §1.2.2 说的「周二三机同时 06:00 起飞」裁剪后只剩**两架**，
  **但 C09 照样命中**（全场口径下两架同刻起飞的间隔是 0 分钟）；
- 图里的空域名（`Range Route 1`、`Large Area C`）在空域表里不存在。保留下来的行一律取
  **课目绑定的空域** —— 空域本就由课目唯一决定，图上那一列是错标；
- 图上没有跑道列（Sheet 1~3 不含跑道，v6 §10.4）。全部放在 RWY-1，是「信息不足时取最保守读法」
  （JL-9 本来也只能用 RWY-1）。

### 4.8 §6 六条门禁 + 两条静态扫描

```
$ ruff check .                → All checks passed!
$ ruff format .               → 11 files reformatted, 131 files left unchanged
$ mypy backend --strict       → Success: no issues found in 84 source files
$ bandit -r backend -ll       → No issues identified.
$ lint-imports                → Contracts: 3 kept, 0 broken.
$ pytest -q --cov=backend --cov-fail-under=80
                              → 805 项：804 passed / 1 skipped，exit 0
                                （skip 的是 `test_exit_criterion_ripgrep_is_empty`：
                                  rg 不在 conda 环境的 PATH 上，同一断言由
                                  逐行扫描版本覆盖）
                                TOTAL 覆盖率 92.82%（阈值 80%）
$ bash deploy/scripts/check_no_placeholders.sh
                              → ✅ 无 TODO / FIXME / NotImplementedError / 待实现 / 待补充 / 后续补
$ bash deploy/scripts/check_egress.sh
                              → ✅ E2 通过 / ✅ E3 通过
```

校验器包单独统计（`--cov=backend/validator`）：

```
backend/validator/checks.py       482  24  268  26   93%
backend/validator/context.py      186   0   16   0  100%
backend/validator/schema.py       143   7   80   9   93%
backend/validator/workbook.py     428  28  162  24   91%
TOTAL                            1239  59  526  59   93%
```

### 4.9 v6 的「M\<n\> 实测填入」留白

`rg -n "实测填入" docs/` 显示 v6 剩余的两处留白分别属于 M9（§12.3 纯 LLM 基线）与已由 M2-A
填过的 §3.1.3。**M2-B 没有需要回填的占位**，故本窗口未改 `docs/`。

---

## 5. 测试用「解」的来源（隔离的证据）

### 5.1 合规样本是**手工排**出来的

`tests/fixtures/validator_facts.py::COMPLIANT_SORTIES` 是本窗口按 v6 §3.2 的 14 条逐条推出来的
14 个架次（9 带飞 + 5 单飞），排布依据是 v6 §1.4.3 的**纸面推演**（那一节是设计文档的内容，
不是求解器的输出）：

- 何超 missionA-2 排在第 2、5 天 → 覆盖全部 5 个 3 天滑窗，且首次执行 ≤ 第 2 天（`F−1`，S-12）
- 罗磊/张勇/陈伟 各 1 次 A 类**单飞**（约束3 + S-02 + S-13，D-1 之下不带教员）
- 9 个「未完成且先修满足」的带飞组合各 1 次（约束13，`freq_days=7`）
- 每日只排 2 个架次、起飞间隔 ≥7 分钟、每机每日 ≤1 次 → 周转与密度都留足余量
- 全周不用 AC73（避开 01-09 定检）、不排吴鹏（避开 01-05 不可用）

**这份解与求解器实际排出来的那 14 个架次几乎肯定不同**（时刻、机号、跑道都是我随手选的
合规值）。这正是它作为校验器测试样本的价值：它只依赖规格，不依赖求解器的任何选择。

### 5.2 仍然测不出来的那一半

「打开 `backend/solver/` 瞄一眼再照着写」不留下 import、也不留下字符串，
`test_validator_isolation.py` 查不出来。这一半靠窗口纪律 —— 如实写在这里，不假装它被测到了。
本窗口的实际做法：`backend/solver/` 全程未打开；`tests/fixtures/solver_facts.py` /
`solver_asserts.py` 同样未打开（它们可能含求解器的编码细节）；M2-A 收工报告只读了 §9.1。

---

## 6. 已知限制

1. **闸门3 的写出侧还没有。** `workbook.py` 定义并实现了回读契约，写出由 M3 交付。
   `tests/fixtures/workbook_builder.py` 是按契约写的**测试用**实现（无字体/底纹/列宽/合并单元格），
   M3 不要直接拿它当产品代码用，但**必须让 `verify_workbook` 对它的产物绿灯**。
2. **Sheet 3 的机组列只有姓名**（版式基准如此），角色靠「双人 = (教员, 学员)、单人看区块7 的
   复训标记」还原。若将来出现「双人且都不是教员」的合法编成，这条还原规则要跟着改。
3. **`training_progress` 的主键不含 `snapshot_id`**（v6 §6.3 的 DDL 如此）。集成测试往库里写
   同一批 (人, 课目, 周期) 时会与 `--baseline` 的既有行撞主键，只能用一个远早于基准周的
   `cycle_start` 绕开。这不是本窗口引入的，但**M2-C / M3 若要在同一个库里并存多个快照的进度，
   会撞上同一堵墙**，建议在 M8 加固窗口一并处理。

   > ⚠️ **由此引出一条踩坑记录：不要并发跑两个 pytest 会话。** 本窗口一度在后台跑全量门禁的
   > 同时又开了一次单包覆盖率统计，两个会话打同一个裸装 PG，结果是
   > `test_solver_baseline_live::test_baseline_week_is_optimal` 报 **FEASIBLE**（机器争抢下
   > 30s 预算内没证到最优）、两个摄取集成测试报 `UniqueViolation`。
   > **那是假红**：串行重跑 805 项全绿。看到基准周变 FEASIBLE 时，**先确认是不是自己在并发跑**，
   > 再按 CLAUDE.md §7 第 4 条走停下来报告的流程。
4. **C13 只校验「本周窗口」**。跨周锚点是按 `last_done_date` 单点接续的；若某周的排班结果没有
   回写 `last_done_date`，下一周的校验会退化成 S-12 分支（从周一起算、不计欠账）。
   回写动作属于 `commit_plan` 节点（W7），本窗口只消费。
5. **属性测试（Hypothesis）没做。** v6 §12.1 的「随机生成计划 → 注入单点违规 → 断言命中正确
   rule_id」在本窗口是**确定性**用例的形态（每条规则 1 合规 + ≥2 违规，三种 v6 点名的新增违规
   形态各有专测）。真正的 `@given` 版本需要 `arbitrary_schedule_plan()` 生成器，而它同时是
   §12.1 另一条属性测试（`test_solver_output_always_passes_validator`）的输入 —— 那条必须两侧
   都在场才能跑，属于 **M2-C 交叉验证窗口**。这里如实标注为未做，不含混。

---

## 7. 需要业务方拍板的两处改动

| # | 改动 | 理由 | 影响面 |
|---|---|---|---|
| 1 | `backend/schemas/validation.py::CheckResult` 新增 `notes: list[str] = []` | 出口标准要求「报告中标注 S-11 为**授权改写**」，而 `CheckResult` 的字段全是违规/计数，没有放「不是违规但必须出现在报告里的声明」的位置。**可选字段 + 默认值，对既有调用方向后兼容**；v6 §10.4 区块6 的「授权改写声明」行直接取 `report.all_notes()` | 契约扩充（新增可选字段），不改变任何既有语义 |
| 2 | `requirements.txt` 新增 `types-openpyxl==3.1.5.20260807` | `workbook.py` 用 openpyxl，`mypy --strict` 报 `Library stubs not installed`。**选择装 stub 而不是把 `openpyxl.*` 加进 `pyproject.toml` 的 `ignore_missing_imports` 白名单** —— 后者是放宽配置（CLAUDE.md §6 要求单列说明），前者与既有的 `types-PyYAML` 同一口径，且能真正查出 openpyxl API 的用法错误 | 仅类型检查期依赖，运行时无影响；CI 走 `pip install -r requirements.txt` 自动生效 |

**`pyproject.toml` / `.importlinter` / `setup.cfg` 一处都没动。**

另有一处**版式契约的扩充**（不是配置放宽，但同样请过目）：Sheet 4 区块1 增加一行「语义开关」，
理由见 §3.10。它不改 `docs/`，只写在 `workbook.py` 的模块文档里，M3 落地时会用到。

---

## 8. 下一个窗口的前置条件与接口约定

### 给 M2-C（交叉验证）

```python
from backend.validator import load_context, run_all_checks, verify_format, verify_workbook
```

- `load_context(session, *, snapshot_id, week_start) → ValidationContext`。
  **`week_start` 必须是周一**，否则 `RequiredInputMissingError`（与求解侧同一约定）。
- `run_all_checks(plan, ctx) → ValidationReport`。**不短路**：14 条全跑完。
  `report.all_passed` = 逐条 `passed` 全真；`report.missing_rules()` 非空即说明没跑全，
  **这种情况下不能宣称 100% 合规**。
- 判定分歧（求解器出解、校验器判违规）就是 **FTS-3003，CRITICAL**，按 CLAUDE.md §7 第 5 条
  停下来报告，**不许**通过调整校验器口径去消除分歧。三处最可能分歧的口径已在 §3.2 / §3.3 /
  §4.6 写明，先对这三条。
- `ValidationReport` 里 `severity="SOFT"` 的违规**不算分歧**（那是已披露的松弛欠账，
  见 §3.4）；只有 HARD 违规才触发 FTS-3003。
- v6 §12.1 的两条属性测试（`test_solver_output_always_passes_validator` /
  `test_validator_catches_injected_violations`）需要一个 `arbitrary_schedule_plan()` 生成器，
  本窗口未做（§6 第 5 条），由 M2-C 补。注入违规的三种必测形态已在
  `tests/unit/test_validator_checks.py` 有确定性版本，直接抄口径即可。

### 给 M3（报告与 Excel）

- **直接 import `backend.validator.workbook` 的常量**来写 xlsx：`SHEET_ORDER`、
  `SHEET1_HEADERS` / `SHEET2_HEADERS` / `SHEET3_HEADERS`、`BLOCK_TITLES`、
  `BLOCK2/3/4/7_HEADERS`、`REQUIRED_META_LABELS`、`ROLE_SUFFIX`、`RECURRENT_MARK`、
  `SWITCH_SEP` / `SWITCH_KV`。两边共用一份常量，就不会漂。
- **三个字段只能靠 Sheet 4 承载**：`sortie_id` / `runway_id` / `is_recurrent` 在区块7；
  `semantics_switches` 在区块1 的「语义开关」行（§3.10）。少任何一个，`verify_workbook`
  的深度相等断言必然不成立。
- **时间列一律写 `HH:MM` 文本**。写成 `datetime.time` 或数字，闸门3 直接判失败
  （`test_detects_excel_serial_time_cells`）。
- 区块6 的「授权改写声明」行是强制项（S-11 开关为 on 时），取 `report.all_notes()`。
- 区块2 的 14 行直接来自 `ValidationReport.results`：`rule_id` / `rule_title` /
  `passed` / `checked_items` / `len(violations)`。**`checked_items` 要如实展示** ——
  它的作用就是让「检查了 0 项」这种假通过一眼可见。
- `tests/fixtures/workbook_builder.py` 可以当作版式契约的可执行说明来读，但它没有样式。

### 给 W7（编排）

- `validate` 节点的形态：`run_all_checks` + `verify_format`，两者都不碰 LLM、不读 Skill，
  符合铁律 4 的确定性边界。
- 驳回回环需要的信息全在 `Violation.fix_hint` 里（面向排班员的可执行建议，不是给求解器的）。

---

## 9. 一句话给下一个自己

**这份校验器的价值全部建立在「它不知道求解器怎么写」这件事上。**
下次要改 `checks.py` 时，如果动机是「让它和求解器对上」，先停下来问一句：
到底是校验器读错了规格，还是求解器读错了规格 —— 那正是 FTS-3003 要问的问题。
