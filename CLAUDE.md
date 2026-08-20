# CLAUDE.md · FTS 飞行训练排班系统

> 本文件是 Claude Code 在本仓库工作的常驻指令。**每个新会话开始时必须完整读完本文件**，再读当次窗口的任务提示词。

---

## 0. 一句话项目定位

全离线内网部署的飞行训练排班系统。**主体是确定性工作流**：排班由 OR-Tools CP-SAT 求解，由一套与求解器实现完全独立的校验器兜底。LLM 只做三件事 —— 把人话翻译成精确求解输入、在冲突时组织人类能懂的权衡、对结果给出可信解释。**LLM 不生成、不修改、不参与任何一条架次记录。**

---

## 1. 权威文档优先级（冲突时从上往下压）

1. `docs/SPEC_DECISIONS.md` —— 业务方逐条裁决记录（本项目的最高规格权威）
2. `docs/FTS_飞行训练排班系统_工程化设计方案_v6.md` —— 工程化设计方案（2026-08-07 起生效）
3. `data/origin/*.pdf` —— 原始业务资料（personnel / aircraft / missions / rules）
4. `data/origin/image 1~4.png` —— **仅版式基准，内容一律不采信**（理由见 SPEC_DECISIONS §C.2 / v6 §1.2.2）

**发现任意两级之间存在冲突 → 立刻停下来问用户，不要自行选一个继续。**

> **v5.2 已作废，任何窗口不得引用。** v6 是全量重写版，已合入 SPEC_DECISIONS 全部裁决、2026-08-07 的六条补充裁定（D-1~D-6）、以及与原始 PDF 逐字比对后的十处数据更正（F1~F10）。**v6 附录 C 是「SPEC_DECISIONS 逐条 → v6 落点」对照表**，找某条裁决落在哪一节时先查它。
>
> **v6 相对 v5.2 的章节号变动**（引用旧号会指错地方）：`§1.2 修复层 → §1.5`；`§3.4~3.9 整体后移两位 → §3.6~3.11`（腾出 §3.4 空域容量、§3.5 频率滑窗）；`§10.5 命名归档 → §10.6`；`§11.1 拆为 §11.1 裸装部署 + §11.2 三态 Provider`，`§11.2~11.4 顺延为 §11.3~11.5`。新增章节：§1.2 原始数据冲突裁定、§1.3 基准周实体全景、§1.4 负载推演与阻塞项、§3.1.1 机组编成规则、§5.5 数据冲突清单、§10.5 版式采信边界、§10.7 版式基准抽取清单、§12.4.1 生成层 judge 与其验证、§12.5.4 egress 拦截、附录 C。
>
> **实测后追加的小节**（M1/M2-A，编号见 v6 本版说明 ⑤ 的 `Z-1`~`Z-6`）：§5.1.1 缺输入即提问、§6.3.1 `cycle_start` 来源、§6.3.2 物化视图语义、**§3.2.1 三处等价重写**、**§3.7.1~§3.7.5 目标函数的五点澄清**、**§3.11.1 可复现性的实现方式与边界**。**§3 的小节号本身没变**，只是多了子节。
>
> **M5 实测后追加**（`Z-17`~`Z-23`）：§6.1 第五个 collection、§6.4 遗忘周期长度与
> `superseded_by` 语义、**§7.2.4 `human_gate` 的两种问法**、§7.3.4 的 `PIN_RUNWAY`
> 负例边界与 undo 语义、§12.4 消融失效形态、§3.11 插桩预算 300s。
> **其中 `Z-19` 改变了修订轮的交互形状**（两次门禁往返），M6 前端按它做。
>
> **M6 实测后追加**（`Z-24`）：**§9.3 错误码由 15 个增至 16 个**（新增 `FTS-4005`
> 状态冲突）、§9.2 补 worker 侧的 `snapshot_id` 级锁、§8.1 回填轮询响应体实测
> 190 字节。**看到「15 个错误码」的旧表述一律按 16 个理解。**
>
> **M9-A 实测后追加**（`Z-30`~`Z-34`）：**§6.5.4 的 `pg:` / `ent:` 双 doc id 形态**
> （算召回指标前必须先过 `canonical_doc_id` 归一，否则路 A 的命中被判成未召回）、
> **§6.4 的「同档来源偏好冲突升级人工、不覆盖」**（这是设计不是缺陷）、
> **§15.4 通用能力回归的判定口径**（确定性判据 + McNemar，门槛「≤3 个点 **且** p≥0.05」，
> **不复用 §12.4.1 的 32B judge** —— §12.4.1 末尾那段「口径未定」自此作废）、
> **§9.2 的一条风险**（一次失败的 SQL 让整个事务作废，而图共用一个会话）、
> **§7.6/§7.7.1 的 LLM 调用预算 10 → 14**（补上从来就漏掉的 Knowledge 检索循环那一行）。
>
> **M8 实测后追加**（`Z-25`~`Z-28`）：§11.5 的**导出脱敏口径**（只脱敏日志、
> 导出不分角色）、§11.4 的 **compose 路径形态**（瘦镜像 + bind-mount，两条路径
> 用**黄金用例指纹**而非基准周比对）、§11.5 的**认证落地形态**（静态 Token +
> 散列 + 轮换，不建用户表）、日志脱敏正则 `P\d{2}` → `P\d+`。
> **错误码仍是 16 个，M8 一个都没加。** 另有 `Z-29`：三条待裁决项经业务方
> 2026-08-19 逐条确认**维持现状**（不自带解释器 / 不加第 17 个码 / 不加限流）。
>
> **看到任何 `§x.y` 一律按 v6 编号理解。** 旧收工报告、早期笔记里若出现 v5.2 编号，先用上面这张表换算，**不要直接照旧号去 v6 里翻**——`§3.5` 在 v5.2 是目标函数、在 v6 是频率滑窗，翻错会写出完全不同的代码。

---

## 2. 运行环境（硬约束，不要改）

| 项 | 值 |
|---|---|
| Python 环境 | conda 虚拟环境 **`schedule`**。所有命令前置 `conda run -n schedule` 或先 `conda activate schedule` |
| GPU | **只用第 4 块卡**：`CUDA_VISIBLE_DEVICES=3`。写进 `.env`、所有训练/推理脚本、Ollama 启动环境 |
| 容器 | **无 Docker**。PostgreSQL 16 / Redis 7 / Ollama 全部裸装，用户态运行，不要求 root |
| 网络 | 服务器**可联外网**（仅用于安装依赖与拉模型）。但**应用代码本身必须写成全离线可运行**，§11.4 的 egress 禁令照常实现与测试 |
| 数据库 | PG16 用 `initdb` 在项目目录下起独立实例（非系统服务），端口默认 5433 避让 |
| Redis | 裸装，端口默认 6380 避让 |
| Ollama | 用户态解压安装，`OLLAMA_HOST=127.0.0.1:11434`，`OLLAMA_MODELS` 指向项目内目录 |

`docker-compose.yml` 仍需按 §11.1 编写并纳入交付包（离线交付场景要用），但**本次搭建不通过它运行**。同时必须提供一份等价的 `deploy/native/` 裸装脚本集，两者行为一致。

---

## 3. 十条铁律

1. **不留半成品。** 严禁 `TODO`、`FIXME`、`pass  # 待实现`、`raise NotImplementedError`、空函数体、「等后续窗口补」。当前窗口范围内的每个模块都必须完整可运行、有测试。范围外的依赖用**真实可用的最小实现或明确定义的接口 + 该接口的完整测试替身**，并在收工报告里列清楚。
2. **`validator/` 禁止 import `solver/`，也禁止「读过」。** 两套代码依据 v6 §3.2 规格表**分别实现**，不共享任何约束表达代码。import-linter 只能查出 import，**查不出「我瞄了一眼 solver 怎么写的然后照着实现」**——所以这条同时是对窗口行为的约束：写 `validator/` 的窗口不许打开 `backend/solver/` 下任何文件，需要解来测试就自己在 `tests/fixtures/` 手工构造，不许去跑求解器拿解。反之亦然。**这个隔离是「100% 合规」这个交付承诺的全部证据基础**：一旦两边互相看过，v6 §12.1 的属性测试与 §12.3 的三重独立验证就同时失去意义。
3. **`solver/`、`nodes/compile_spec.py`、`validator/` 禁止 import `skills_loader/`。** 同样由 import-linter 强制。**第三条禁令**：除 `core/http.py` 外全仓库禁止 import `requests` / `httpx` / `urllib.request`（egress 收口，v6 §11.5 / §12.5.4）。三条一并由 `lint-imports` 把关。
4. **确定性边界。** `compile_spec` / `solve` / `validate` / `resume_guard` / `human_gate` / `commit_plan` 六个节点**不经 Harness、不读 Skill、不注册为任何 LLM 组件的工具**。`probe_solve` 是唯一例外（只读探针，受独立预算池约束）。
5. **不假设实验数据。** 任何实验用例、数据集、标注、指标口径，如果设计方案里没有明确定义，**停下来问用户**，不要自己编一个跑下去。
6. **不报告未实际计算的指标。** 所有数字必须来自真实运行。跑不出来就写「未跑通 + 原因」，不写估计值。禁止在报告里出现「预期 / 约 / 大致」修饰的实测项。
   **正向要求**：v6 里凡是写着「M\<n\> 实测填入」的位置都是**刻意留白**（候选规模 §3.1.3、端到端延迟 §7.6、基线对比 §12.3、训练显存与耗时 §15.3、消融表 §15.4）。跑出真数后**回填 v6 文档**并在收工报告里注明，这不是可选项。
7. **抽取失败绝不静默降级。** 摄取管线宁可抛 `IngestionError` 阻断，也不让 `sionB-1` 这类脏 token 进库。
8. **UNKNOWN ≠ INFEASIBLE。** 三态（OPTIMAL/FEASIBLE、INFEASIBLE、UNKNOWN）在数据模型、错误码、UI 配色上全程分离。混为一谈是本类系统最伤信任的 bug。
9. **可复现性。** 同 `snapshot_id` + 同 `ruleset_version` + 同 `semantics_version` + `seed=42` → 结果逐字节可复现。任何引入不确定性的写法（未固定的字典序、时间戳进哈希、未 seed 的随机）都是 bug。
10. **有疑问就问，不要猜。** 见 §7。

---

## 4. 规格速查（已裁决，直接用，不要再问）

完整版见 `docs/SPEC_DECISIONS.md`（原始裁决）与 **v6 §1.1 语义假设登记表 S-01~S-13**（合并后的最终形态），另有 S-14（`cycle_start` 来源，业务方 2026-08-09/08-10 裁定，落 v6 §6.3.1）。以下是最常用的：

| 项 | 裁定 | v6 落点 |
|---|---|---|
| 先修「X类」(S-01) | 该类**全部**课目完成（A类需 A-1 且 A-2） | §3.1、§3.2 约束13 |
| 约束3 A类每周必飞 (S-02) | A 类**整体**至少 1 次（飞 A-1 或 A-2 任一即可） | §3.2 约束3、§3.5.4 |
| **约束3 适用范围 (S-13)** | **对全部学员生效，不论完成状态**（语义是「保持熟练度」，与约束13 的「推进进度」分离） | §3.2 约束3、§3.5.4 |
| **S-13 的例外 (Z-9)** | **本周每一天都不可用的学员不计入约束3**。判据**只看人在不在，不看排不排得上** —— 还有一天可用就照常要求（那天排不上是资源问题，必须如实判不可行）。⚠️ 这条**不解开约束13**：有未完成课目的学员整周请假仍是 INFEASIBLE，该走 Tier 1 松弛 | §1.1 S-13、§3.2 约束3、§3.5.4 |
| 频率换算 (B.4) | **各课目用自己的 `freq_days` 开滑动窗口**（A类=3天、B~F类=7天、G/H类=14天），跨周由 `last_done_date` 锚点衔接 | §3.5 |
| **跨周首执行截止日 (D-4)** | 统一取通式 **`first_exec_day ≤ max(0, freq_days − gap)`**（SPEC_DECISIONS §B.4 第二分支的 `−1` 是笔误） | §3.5.3 |
| **锚点缺失 (S-12)** | `last_done_date` 为 NULL 时**视为窗口从本周周一起算**（`≤ freq_days − 1`），**不计欠账**。原始 PDF 未提供该字段，用 `gap=999` 会让基准周假性不可行 | §3.5.3 |
| 约束7 周转基准 (S-06) | 上一架次**着陆** → 下一架次**起飞** | §3.2 约束7 |
| 约束8 休息 (S-07) | 仅同日内累计 | §3.2 约束8 |
| 约束9 窗口 (S-04) | 半开 `[t, t+20)` | §3.3 |
| 跑道 (S-05) | **双跑道**：`RWY-1`(JL-8, JL-9: AC10/AC27/AC34/AC49/AC61/AC73/AC84/AC95)、`RWY-2`(JL-8: AC10/AC27/AC34/AC49/AC61/AC73)。**JL-8 的跑道是求解决策变量，JL-9 固定 RWY-1** | §1.1.1、§1.3.5、§3.3 |
| **起降密度分组口径 (D-2)** | **20 分钟窗口按跑道分组；7 分钟间隔全场统一**（`rules.pdf` 约束9 只对前半句限定「同一跑道」） | §1.1.1、§3.3 |
| 带飞 (S-08 + D-1) | `需带飞 = (mission.带飞==是) ∧ (身份==学员)`。**A-1/A-2 的带飞列为「否」→ 学员 A 类单飞**；B~H 类学员带飞；教员与成熟飞行员刘斌一律单飞 | §3.1.1 |
| 教员复训 (S-09 + S-11) | 教员**不排**课目（只占带飞教员岗）；成熟飞行员刘斌 C 类到期后按 7 天滑窗强制复训，**覆盖约束2 字面语义** | §1.2.4、§3.1.2 |
| **S-11 复训粒度 (Z-8)** | **按「类别」不按「课目」**——到期类别里有多门课目时，飞该类**任一门**即满足本次复训（刘斌 C 类飞 C-1 或 C-2 都行）。学员的进度推进仍逐 (人, 课目) 判 | §3.2 约束13、§12.3 S-11 专项 |
| 空域容量 (S-10) | **硬约束**，并入约束6 实现，对外仍称「14 条」。`rules.pdf` 约束6 原文即含此要求 | §3.4 |
| **约束14 `req_max`** | **`ceil(7 / freq_days)`** —— A类 3，B~F 类 1，G/H 类 1 | §3.2 约束14 |
| **松弛 Tier 2 (D-6)** | 重定义为「**约束3 整体降级为软目标**」（S-02 之下原定义已成空操作） | §3.10 |
| 刘斌 C 类到期日 (C.1) | **2026-01-07**（总表），明细表的 02-07 为笔误 | §1.2.1、§5.5 X1 |
| **`cycle_start` 来源 (S-14)** | ① 课目文件的「课程开始日期」列（可选，逐行读，各课目可不同）→ ② 用户回答 `Q_cycle_start` → ③ 都没有就**提问并阻断**（FTS-1004）。**没有默认值，配置项里也没有** | §6.3.1、§5.1.1 |
| 基准周 (C.3) | 2026W02，2026-01-05 ~ 2026-01-11 | §1.2.3 |
| **编号与枚举 (Z-4)** | **编号只固定前缀、不限位数**（`^P\d+$` / `^AC\d+$` / `^mission[A-Z]-\d+$` / `^RWY-\d+$`）；**空域编号不是枚举**；机型由上传数据决定。`role`/`weekday`/`identity`/`level` 保持枚举（它们是规格） | §5.1.1、附录 B |
| **课目「完成」判据 (Z-16)** | **一门课飞完完整周期才算完成**：`cycle_required = (cycle_weeks × 7) // freq_days` —— A 类 28 次、B~F 类 16 次、G/H 类 10 次。`commit_plan_node` 攒满后置 `status='COMPLETED'` **并往 `person_completed_missions` 写事实行**（先修判定读的是那张表，只翻 status 会解锁不了先修）。⚠️ **只升不降**：摄取期读进来的 COMPLETED 是业务方给的事实，不许被计次公式推翻 | §6.3.3 |
| **回显确认的落点 (Z-19)** | 修订翻译**先回门禁展示「我理解为…」，`APPROVE` 才 `solve`** —— 修订轮是**两次门禁往返**。同一门禁按 `pending_revision` 有两种问法，`APPROVE` 在回显屏是「去重解」不是「去归档」。**没有第四种决策** | §7.2.4 `Z-19` |
| **撤销的语义 (Z-21)** | `undo` = 去掉那条约束**再解一次**，**不是把旧方案取回来**。重解结果不必与当初那版逐字节相同（最小扰动锚定**当前**方案），要断言的是**约束集回到了那一版** | §7.3.4 `Z-21` |
| **记忆时效 (Z-18)** | `superseded_by` 是**链接不是墓碑**：有效性只由 `[valid_from, valid_to)` 决定。当作废标记会让历史时点查询一律返回空。情景记忆归档线 = 快照里**最长**的 `cycle_weeks` × 3 | §6.4 `Z-18` |
| **可复现性 (Z-3)** | CP-SAT **不保证**同 seed + 同 worker 数返回同一个等价最优解。靠「多线程求最优值 + **单线程规范化**」两段式解决；保证边界是 `OPTIMAL` 时逐字节可复现、`FEASIBLE` 时不保证 | §3.11.1 |
| **导出脱敏 (Z-25)** | **只脱敏日志，导出文件不按角色分级** —— 所有能调 `/export` 的角色拿到**完全相同**的 xlsx。v6 §11.5 原文只说「按角色控制字段可见性」而没定义哪些字段，业务方 2026-08-18 裁定按此实现。⚠️ 日志脱敏的编号正则是 `P\d+` **不限位数**（`Z-28`，原 `P\d{2}` 对 `P100` 静默失效） | §11.5 |
| **两条部署路径的比对量 (Z-26)** | **比的是黄金用例指纹，不是基准周** —— 基准周会落到 `FEASIBLE`，而那个状态不保证逐字节可复现（§3.11.1），拿它比会得到一个会飘的门禁。40 个黄金用例全部落在 `OPTIMAL`/`INFEASIBLE`，聚合成一个 `golden_fingerprint`。**M8 实测两条路径同为 `4dc4df24…f0c0aca`**。compose 的镜像**不装依赖**，conda 环境 bind-mount 进相同绝对路径 | §11.4 |
| **认证落地 (Z-27)** | **静态 Token → 角色，不建用户表**。条目支持 `sha256$<hex>:user:role`，明文兼容但启动打 WARN。`python -m backend.api.tokens_cli new/hash/rotate`。**`API_TOKENS` 空 = 全部拒绝** | §11.5 |
| **召回 id 双形态 (Z-30)** | 路 A 发 `pg:<表>:<主键>`、语料发 `ent:<类>:<编号>`，**同一实体两种形态**。算 Recall@5 / MRR@10 之前两侧都要过 `backend/datasets/schemas.py::canonical_doc_id` 归一 —— 不归一会把路 A 的命中全判成未召回 | §6.5.4、§12.4 |
| **偏好不会被自动改写 (Z-31)** | `put_preference` 遇「同档来源、值不同」**升级人工**（FTS-2001），只有可信度**严格更高**才覆盖。所以「蒸馏两次得到两个版本」是走不通的，要多版本得走可信度升级（对话推断 → 排班确认记录） | §6.4 |
| **通用能力回归口径 (Z-32)** | **确定性判据 + McNemar 配对精确检验**，门槛「整体绝对下降 ≤3 个点 **且** p ≥ 0.05」，子层降 >8 个点只作警示。**不复用 32B judge**；判定器落 `backend/datasets/ood_judge.py` | §15.4、§12.4.1 |
| **LLM 调用预算 (Z-34)** | **每请求上限 10 → 14**，§7.6 预算表补 **KnowledgeAgent 检索问答**一行（改写 1 + 循环 ≤6 步 + 生成 1 = 8）。M9-A 实测 71/320 条探针顶到旧上限被截断。**选的是补预算不是砍步数** —— 没有质量数据前不削探索深度 | §7.6、§7.7.1 |
| **一条坏 SQL 作废整个事务 (Z-33)** | 模型编表名 → `InFailedSqlTransaction` → 同一会话后续查询全败，而图**共用一个会话**。跑批脚本按「一条一会话」规避，**生产侧未改**；W13 前决定要不要给 `sql_query` 套 SAVEPOINT | §9.2 |
| **三条「维持现状」(Z-29)** | 业务方 2026-08-19 逐条确认，**都不改代码，也不要再问**：① **交付包不自带 Python 解释器**，靠 `conda/PYTHON_VERSION` + install.sh 按版本挑，挑不到明确失败；② **未预期异常不占业务码**，500 用 `{"kind":"internal"}` 的不同形状，**码表仍是 16 个**；③ **不加速率限制**（v6 §11.5 未定义口径，两把锁已挡住数据层并发） | §9.3、§11.4、§11.5 |
| **并发与锁 (Z-24)** | **两把锁**：`(tenant, week)` 排班锁（API 提交期取，拿不到即 `FTS-4005` / HTTP 409，**不排队**）+ **`snapshot_id` 级锁**（worker 执行期取，堵住「不同周、同快照」在 `training_progress` 上的死锁）。**16 个错误码**，第 16 个是 `FTS-4005` | §9.2、§9.3 |

**实体规模（按 `data/origin/*.pdf` 逐字核对，v6 §1.3）**：**8 人**（3 教员 + 1 成熟飞行员 + 4 学员）· **8 机**（JL-8 六架 AC10/27/34/49/61/73；**JL-9 只有两架 AC84/AC95**）· 12 课目 · 6 空域 · 2 跑道。
⚠️ **AC73 是 JL-8，不是 JL-9**；学员只持 JL-8 机型资质与 A/B/C/F 四类资质，故 D/E/G/H 类课目不生成任何学员候选。

> ⚠️ **但这组数字是「基准数据集长什么样」，不是「系统只能处理这么大」。**
> 生产形态是**用户上传自己的人员/飞机/课目/空域文件**，`data/origin/` 只是数据模板与
> 基础测试样本。所以 8 人、`P\d{2}`、`JL-8`/`JL-9`、类别 A~H **一个都不许写成代码
> 常量或校验上限**；它们唯一的合法用途是基准回归护栏（v6 §5.1.1、§1.3 告警框）。
> 用户少传一类数据时**提示补传**，绝不拿基准数据或上一版快照顶替。

**基准周已知扰动**：吴鹏 01-05 不可用；AC73 01-09 全天定检；刘斌 C 类 01-07 到期。

**基准周结果（M2-A 已实测，不再是"预期"）**：**OPTIMAL**，**14 架次 = 9 带飞 + 5 单飞**，
**7 条阻塞项**（何超 B-1/B-2/C-1/C-2/F-1；张勇 C-2；陈伟 C-2），静态预筛后**候选 2276**，
模型 12 568 变量 / 37 235 约束，墙钟 **21.0s**（M2-A 实测时预算为 30s；**该预算 2026-08-13
由业务方提到 60s**，见 v6 §3.11 `Z-13` —— 21.0s 这个墙钟不受影响，证完即返回）。
与 v6 §1.4 的纸面推演逐项吻合。
**后续窗口若跑出与此不同的结果，先怀疑自己的改动，再按 §7 第 4 条停下来问 —— 不许放宽约束。**

---

## 5. Git 工作流

- **单仓库，每个里程碑一个分支，PR 合入 main。** `main` 必须随时可跑。
- 分支命名：`feat/m0-bootstrap`、`feat/m1-ingestion`、`feat/m2a-solver`、`feat/m2b-validator`、`feat/m2c-crosscheck`、`feat/m3-report`、`feat/m4a-harness`、`feat/m4b-orchestration`、`feat/m5-retrieval`、`feat/m6-frontend`、`feat/m8-hardening`、`feat/m9a-datasets`、`feat/m7-finetune`、`feat/m9b-experiments`
- **开工第一件事**：`git fetch origin && git switch main && git pull && git switch -c <分支名>`
- **收工顺序**：静态工具链全绿 → 测试全绿 → `git add -A && git commit` → `git push -u origin <分支>` → `gh pr create` → 里程碑验收通过后打 tag（`m2-done` 等）
- Commit message 用中文，格式 `<模块>: <做了什么>`，正文列出关键决策。
- **禁止** `git push --force` 到 main、`git commit --amend` 已推送的提交、`git reset --hard` 未备份的工作区。

---

## 6. 质量门禁（每个窗口收工前必须全绿）

```bash
conda run -n schedule ruff check . --fix
conda run -n schedule ruff format .
conda run -n schedule mypy backend --strict
conda run -n schedule bandit -r backend -ll
conda run -n schedule lint-imports                # import-linter，强制三条依赖禁令（§3 铁律 2/3）
conda run -n schedule pytest -q --cov=backend --cov-report=term-missing --cov-fail-under=80

# ⚠️ CI 在这六条之后还有两条静态扫描，**本地也必须跑**（M2-A 的 CI 就是栽在第一条上）
bash deploy/scripts/check_no_placeholders.sh      # 铁律 1：无 TODO / 半成品
bash deploy/scripts/check_egress.sh               # v6 §12.5.4 的 E2/E3
```

> **不要用「肉眼看 `rg` 的输出」代替跑脚本。** M2-A 当时按下面 §10 的 `rg` 命令扫了一遍，
> 看到两条命中、判断「只命中检查正则本身、无害」就过了 —— 而 CI 调的是
> `check_no_placeholders.sh`，它**命中即 exit 1**，不认「无害」。
> **门禁的判据是脚本的退出码，不是人的判断。**
>
> **也不要在「CI 看不到的那一侧」跑门禁。** 同一个窗口紧接着又栽了一次：新写的文件
> 还没 `git add`，而当时的脚本只扫入库文件，于是本机全绿、一 commit 就 CI 红。
> 两次是同一个错的两种形态 —— **验证时的视角必须与 CI 的视角一致**。
> 现在两个扫描脚本都用 `git ls-files --cached --others --exclude-standard`
> （入库的 + 未入库但没被 gitignore 的），本机与 CI 看同一批文件；
> **新增文件在提交之前就会被拦下**。
>
> **pre-commit 已装（M2-A）**：`conda run -n schedule pre-commit install`。
> 它在 `git commit` 时跑上面除 pytest 以外的全部检查 + 通用卫生检查。两条注意：
> ① **与 CI 重叠的检查一律用 `language: system` 调 conda 环境**，不要换成远端 hook ——
> 曾经 ruff 用远端 v0.9.6、CI 用环境里的 0.16.1，两者判定不一致来回打架；
> ② `pre-commit run --all-files` 在本机跑不了（系统 git 2.25.1 不支持
> `--deduplicate`），全量扫描用 `pre-commit run --files $(git ls-files)`，
> **提交时的 hook 不受影响**。

一条不过就不许推。**不许通过放宽配置来让它过** —— 配置文件（`pyproject.toml` / `.importlinter` / `setup.cfg`）的任何放宽都要在收工报告里单列一条说明理由，并等用户确认。

CI（GitHub Actions）跑同一套命令，`LLM_PROVIDER=mock`、`EMBED_PROVIDER=hash`，**不依赖 Ollama、不依赖 GPU**。

⚠️ **跑 pytest 之前必须先 `alembic upgrade head`。** 集成测试按 v6 §12.1 直连裸装 PG，
**测试不自带 schema**；库里没表就会以 `relation "data_snapshots" does not exist` 全线失败。
CI 在六条门禁之前有一个独立的迁移步骤，本地则由开工检查单的 `healthcheck.sh` 把关
（它会检查 `alembic_version`）。
**M1 的首个 PR 就是栽在这条上**：本地手工迁移过所以全绿，CI 是全新库所以全红。

⚠️ **集成测试不许断言「环境外状态」**——「库里应当已经有一个 ACTIVE 快照」这类断言
等于假设有人在测试之外先跑过某个命令，本地绿、CI 红。要断言什么就在用例里自己建起来。

---

## 7. 必须停下来问用户的情形

**看到以下任意一条，立刻停止编码，把问题、你倾向的方案、以及各方案的具体后果写清楚，等用户回答。**

1. 设计方案与原始 PDF / `SPEC_DECISIONS.md` 之间出现冲突
2. 某条规格有两种以上说得通的读法，且选择会改变排班结果
3. 需要新的实验用例、数据集、标注、指标口径，而现有文档没有定义
4. 基准周或某个测试场景跑出 INFEASIBLE，而设计方案预期它应当可解（**绝对不许通过放宽约束来让它可解**）
5. 求解器与校验器判定分歧（FTS-3003，CRITICAL）
6. 要装的依赖 / 要拉的模型体积超过 10GB，或磁盘剩余不足 50GB
7. 某个指标实测显著低于目标（差距 > 5 个百分点），需要决定是调实现还是调目标
8. 需要修改 `docs/` 下任何设计文档
9. 需要偏离本文件 §3 的十条铁律

提问格式：**问题 → 各选项 → 每个选项的具体后果（影响哪些架次/指标/模块）→ 你的建议**。不要只抛问题不给分析。

---

## 8. 目录结构

```
fts/
├── CLAUDE.md
├── backend/
│   ├── api/            # FastAPI 路由、依赖、鉴权、错误处理
│   ├── agents/         # 2 个 Agent：knowledge / diagnosis（自主循环）
│   ├── components/     # 4 个 LLM 节点：route / planner / extract / explain
│   ├── routing/        # 两级意图分类：规则匹配 + LLM 兜底
│   ├── nodes/          # 确定性节点：compile_spec/solve/validate/resume_guard/human_gate/commit_plan
│   ├── graph/          # LangGraph 组装、State、Checkpointer、Store
│   ├── solver/         # candidates.py / model.py / objective.py / diagnose.py
│   │                   #   + data.py / solve.py / reschedule.py（M2-A 新增）
│   ├── validator/      # checks.py / schema.py / workbook.py   ← 禁止 import solver、skills_loader
│   ├── ingestion/      # 分类器 / parsers / repair / chunkers / diff / conflicts.py（§5.5 源内冲突检出）
│   ├── memory/         # progress.py / episodic.py / procedural.py / store.py
│   ├── retrieval/      # rewrite.py / bm25.py / vector.py / rrf.py / rerank.py / prereq_cte.py
│   ├── report/         # excel.py / verify.py / naming.py / manifest.py
│   ├── harness/        # 契约校验 / 重试 / ACL / 预算 / 上下文装配 / 录制重放 / prompt版本
│   ├── planner/        # SolveIntent 生成 / 影响面探测 / 修订翻译 / 置信度校准
│   ├── training/       # 数据合成 / LoRA 训练脚本 / 准入评估（离线批任务）
│   ├── skills_loader/  # frontmatter 解析 / 路由 / authoritative 校验
│   ├── llm/            # provider.py / ollama.py / mock.py / replay.py
│   ├── models/         # SQLAlchemy ORM
│   ├── schemas/        # Pydantic 契约（对外冻结）
│   └── core/           # config / logging / security / errors / http.py（唯一允许出网的受限工厂）
│                       #   + ruleset.py（rules/*.yaml 类型化加载，两侧共用，M2-A 新增）
├── frontend/           # Streamlit
├── rules/              # ruleset_v1.3.yaml + semantics.yaml（S-01~S-13 全部做成开关）
├── skills/             # 知识层 markdown（业务方可编辑，authoritative: false）
├── templates/          # Excel 模板 + 版式基准抽取清单
├── tests/              # unit / property / integration / golden / trajectory / guardrail / e2e
├── datasets/           # 评测集 / 轨迹标注 / SFT 合成数据（版本化）
├── data/origin/        # 原始 PDF 与版式图（只读，禁止修改）
├── data/plans/         # 归档的排班产物 YYYY/Www/（xlsx + json + manifest + 校验报告 + solver log）
├── deploy/             # native（主路径）/ compose（交付备选）/ scripts / offline-package
├── traces/             # 录制的运行轨迹（供 replay）
├── reports/            # 实验结果与验收报告
└── docs/
```

---

## 9. 常用命令

```bash
# 环境
conda activate schedule
export CUDA_VISIBLE_DEVICES=3

# 服务（裸装，脚本在 deploy/native/）
bash deploy/native/start_pg.sh      # PG16, 127.0.0.1:5433
bash deploy/native/start_redis.sh   # Redis7, 127.0.0.1:6380
bash deploy/native/start_ollama.sh  # Ollama, 127.0.0.1:11434, 绑 GPU 3
bash deploy/native/healthcheck.sh   # 全栈体检（含 alembic 版本检查）

# 数据库迁移（**跑集成测试前必须先做**，见 §6）
alembic upgrade head                # 建/升到最新 schema
alembic current                     # 看当前版本
alembic downgrade base              # 清空全部表（回滚验证用，会删数据）

# 摄取基准数据（把 data/origin/ 四份 PDF 跑成基准 snapshot）
python -m backend.ingestion.cli --baseline
python -m backend.ingestion.cli --baseline --dry-run     # 只看 Diff 不落库
python -m backend.ingestion.cli <文件...> --approver <人> \
    --cycle-start YYYY-MM-DD --resolve "<冲突id>=<取值>"  # 上传路径

# 应用（三个进程都要起，缺 worker 时任务永远停在 QUEUED）
export API_TOKENS='<token>:<user_id>:<role>,...'   # 空表 = 全部拒绝，不是全部放行
export FRONTEND_API_TOKEN='<前端用的那个 token>'
uvicorn backend.api.main:app --host 0.0.0.0 --port 8000
rq worker --url redis://127.0.0.1:6380 --worker-class rq.SimpleWorker fts
streamlit run frontend/app.py --server.port 8501

# E2E（需要 chromium；CI 上没有会自动跳过）
python -m playwright install chromium
pytest tests/e2e

# 三态 Provider
LLM_PROVIDER=ollama LLM_MODEL=qwen2.5:14b-instruct-q4_K_M   # 真机
LLM_PROVIDER=mock                                            # 单测/CI，零 LLM 调用
LLM_PROVIDER=replay REPLAY_TRACE_DIR=./traces/accept_v1      # 回归，零 LLM 调用
```

---

## 10. 每个窗口的开工 / 收工检查单

> **本节对每个窗口都生效，不论粘贴进来的是哪一段任务提示词。** `docs/CC_PROMPTS.md` 只有被粘贴的那一节会进上下文，本文件则每次自动加载——所以凡是「每个窗口都必须成立」的规则一律写在本文件里，CC_PROMPTS 只放该窗口独有的任务内容。

**开工**
- [ ] 读完 CLAUDE.md + `docs/SPEC_DECISIONS.md` + **v6 设计方案**中本窗口相关章节（章节号见 `docs/CC_PROMPTS.md` 各窗口的【前置】；**不要读 v5.2**）
- [ ] **读完 `reports/` 下上一个里程碑的收工报告**（M2 读 `M1_收工报告.md`，以此类推）。
      收工那栏写着「收工报告是唯一的交接面」，那开工就必须真的去读它 ——
      上一个窗口踩过的坑、留下的接口约定、「我本来想这么做但因为 XX 改成了那样」，
      只在那份文件里。**不读它 = 那些坑你要重踩一遍。**
      ⚠️ **唯一例外：隔离窗口之间。** M2-B（写 `validator/`）**只许读 `M2A_收工报告.md`
      的 §9.1「给 M2-B」那一节** —— 该报告的 §3/§4.2/§5 写满了求解器的编码细节，
      读了等于打开了 `backend/solver/`，铁律 2 的隔离随之作废，而 import-linter 查不出来。
      **写隔离窗口的收工报告时，要专门留一节「给对面那个窗口」，只写口径不写编码。**
- [ ] `git switch -c <分支名>`
- [ ] `bash deploy/native/healthcheck.sh` 确认依赖服务在线（M0 窗口除外）
- [ ] 用 TodoWrite 把本窗口的交付项拆成可勾选的任务列表
- [ ] 把「本窗口需要用户拍板的问题」一次性列出来先问掉，不要边写边问

**收工**
- [ ] 本窗口交付项逐条对照，**没有任何 TODO / NotImplementedError / 空实现**
- [ ] `rg -n "TODO|FIXME|NotImplementedError|待实现|待补充|后续补" backend/ frontend/ tests/` 输出为空
- [ ] §6 质量门禁六条命令全绿
- [ ] 本窗口的出口标准（见任务提示词）逐条实测通过，**贴真实输出**
- [ ] 写 `reports/M<n>_收工报告.md`：做了什么、关键决策与理由、实测数据、已知限制、下一窗口的前置条件。
      **写给下一个窗口的自己看**——踩过的坑、留下的接口约定、以及「我本来想这么做、但因为 XX 改成了那样」都要写。下一个窗口读不到你这次会话的上下文，收工报告是唯一的交接面。
- [ ] 回填 v6 中本窗口跑出真数的「M\<n\> 实测填入」占位（铁律 6）
- [ ] commit + push + 开 PR
- [ ] 在对话里向用户汇报：**出口标准逐条的实测结果**、需要用户决策的遗留问题、PR 链接

---

## 11. 反模式清单（见到就是错）

- ❌ 引用 v5.2 的章节号（§3.4~3.9、§10.5、§11.1~11.4 在 v6 里都换了位置，见 §1 的变动说明）
- ❌ 为了让基准周可解而放宽任何硬约束
- ❌ 把 `last_done_date` 缺失当作「已欠账」（`gap=999`）—— 违反 S-12，会让基准周假性不可行
- ❌ 把约束9 的 7 分钟间隔实现成「按跑道分组」—— 违反 D-2，原文只对 20 分钟窗口限定了「同一跑道」
- ❌ 让学员 A 类架次带教员 —— 违反 D-1，A-1/A-2 的带飞列是「否」
- ❌ 校验器 import solver，或两者共用一个「约束表达」工具模块
- ❌ 把 solver / validator 包装成 Agent 或注册为 LLM 工具
- ❌ 让 LLM 选 Skill（Skill 路由是确定性规则）
- ❌ 用 `image 1~4.png` 里的架次数据做黄金用例期望输出
- ❌ 在实验报告里写没实际跑出来的数
- ❌ judge 没过一致性验证（v6 §12.4.1：一致率 ≥85% 且 Kappa ≥0.70）就硬报 Faithfulness / 上下文利用率；更不许换 judge 模型或放宽门槛来让它过
- ❌ 把 v6 §12.4.1 的「judge vs 人工」Kappa 与 §12.2 的「双人标注」Kappa 混为一谈（前者实际计算故必报，后者未做故不报）
- ❌ 写 `validator/` 的窗口去读 `backend/solver/`（或反之）—— import-linter 查不出来，但它毁掉的是整个交付承诺的证据基础
- ❌ 单元测试依赖真实 Ollama
- ❌ 把 `UNKNOWN` 当 `INFEASIBLE` 处理
- ❌ 摄取失败时回退到「尽力而为」的部分入库
- ❌ **给缺失的必需输入设静默默认值**（默认日期、默认规模、默认机型…）—— 缺什么就问什么，`FTS-1004`
- ❌ **把 v6 §1.3 的基准规模/编号/机型当成系统上限**（8 人、`P\d{2}`、`JL-8`/`JL-9`、类别 A~H）——
      §1.3 描述的是基准数据集，生产形态是用户上传自己的数据，见 §5.1.1
- ❌ **用户少传一类数据时拿上一版快照或基准数据顶替** —— 必须提示补传（`resolution="upload"`）
- ❌ **删掉 `solver/objective.py` 的「单线程规范化」阶段** —— 它看起来像多余的一次求解，
      删了铁律 9 会**静默失效**（CP-SAT 多线程不保证返回同一个最优解，v6 §3.11.1）
- ❌ **删掉 `solver/` 里那三族「冗余」蕴含下界** —— 缺一族基准周就证不到 OPTIMAL（v6 §3.7.5）
- ❌ **把 v6 §3.2/§3.1.3 的编码写法当成语义规格去校验** —— §3.2.1 列了三处等价重写，
      校验器按**语义**独立实现，不比对编码形态
- ❌ **把 S-11 复训实现成「到期类别里每门课目各自 7 天滑窗」** —— 粒度是**类别**（Z-8），
      飞该类任一门即满足；写成逐门会与求解器判定分歧（FTS-3003，M2-C 实测过一次）
- ❌ **把回显确认做成「先重解再展示」**（Z-19）—— 顺序反了等于「翻译错了也已经排了一版」；
      也**不许把回显写进 `explanation`**，那个字段归 `explain` 节点所有、每轮末尾会被覆盖
- ❌ **断言「undo 之后方案与当初那版逐字节相同」**（Z-21）—— 最小扰动项锚定的是**当前**方案，
      重解出来的是「满足那套约束、且离当前最近」的解，两者都是 OPTIMAL
- ❌ **把 `superseded_by` 当作废标记**（Z-18）—— 有效性只由区间决定；当墓碑用会让
      「刘斌 01-06 能不能飞仪表课目」这类历史时点查询一律返回空
- ❌ **把「飞过一次」当成课目完成**（Z-16）—— 完成的判据是**飞完完整周期**。
      按飞一次置位会让何超飞一次 A-2 就把 B 类课目全解锁，先修判定当场失守
- ❌ **只改 `training_progress.status` 不写 `person_completed_missions`** ——
      先修判定读的是后者（v6 §6.1），只翻 status 会出现「显示已完成、先修却解锁不了」
- ❌ **把 S-13 的例外写成「该学员没有可行候选就豁免约束3」** —— 判据是**人整周不可用**（Z-9）。
      写成前者会让约束3 在飞机全在修 / 空域关闭 / 跑道关闭时**静默失效**，
      排班员看到「可行」却不知道有人整周没飞 A 类
- ❌ **指望 S-13 的例外能让「学员整周请假」重新可解** —— 它只解开约束3；约束13 的
      频率滑窗照样满足不了，仍判 INFEASIBLE，正确出路是 Tier 1 松弛 + 欠账显式披露（Z-9）
- ❌ **属性测试跑出反例时先改代码让它绿** —— 反例就是 FTS-3003 的定义，先定位到
      **具体条款**再按 §7 第 5 条报告。M2-C 的 Z-8 正是这么抓出来的：两侧代码都没错，
      错的是 v6 §3.2 与 §12.3 互相矛盾
- ❌ 用 `pypdf` 做 PDF 处理（用 pdfplumber）
- ❌ 在 artifacts / 前端里用 localStorage
- ❌ **把锁冲突复用成 `FTS-3004`**（Z-24）—— 3004 是「数据变了要重解」，
      锁冲突是「数据没变、有人在排」，用户的下一步一个是重跑、一个是等
- ❌ **只加 `(tenant, week)` 锁就以为并发安全了**（Z-24）—— 它盖不住「不同周、
      同快照」，而 `materialize_progress` 正是在那里死锁的（M5 有实测证据）
- ❌ **前端用 `st.dataframe` 显示需要被读到/复制的内容** —— 它是 canvas 网格，
      内容不进 DOM：屏幕上看得见，选不中、复制不了、E2E 也读不到（M6 实测踩过）
- ❌ 提交 `.env`、模型权重、`data/` 下的大文件到 Git（写好 `.gitignore`）
