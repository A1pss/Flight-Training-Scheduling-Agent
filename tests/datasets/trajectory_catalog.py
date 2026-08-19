"""`trajectory_100` 的构造（v6 §12.6）。

本文件当前交付的是**15 条送审样例**，重点是让业务方看清「**可接受的替代路径**」
是怎么定义的。全量 100 条待确认后铺开。

## 分层：两处受控自治必须过半

§12.6.2 写得很直白 —— 排班与重排的期望路径是**固定序列**（主体为静态工作流），
轨迹评估在那两类上只验「没跑偏」；真正考察自主决策质量的是 **Knowledge 检索循环**
与 **Diagnosis 探测循环**，它们应占标注集一半以上。全量 100 条的计划分布：

| flow | 条数 | 性质 |
|---|---|---|
| `query`（Knowledge 循环） | 30 | 受控自治 |
| `diagnosis`（Diagnosis 循环） | 25 | 受控自治 |
| `schedule` | 15 | 固定序列 |
| `reschedule` | 10 | 固定序列 |
| `revision` | 10 | 固定序列（两次门禁往返） |
| `ingest` | 10 | 图外的两段式 + 人工确认 |

自治两类合计 **55 条 > 50**。

## 「可接受的替代路径」的三条准入规则

标注里每条 `acceptable_paths` 都能归到下面三条之一。**不在这三条里的差异一律判错** ——
「可接受」如果没有边界，路径正确率这个指标就没有意义了。

| # | 规则 | 例子 |
|---|---|---|
| **A** | **同层并列调用的顺序差异** | 三路召回谁先谁后；两个 `resolve_person` 的先后 |
| **B** | **信息已足够时省略可选步骤** | 候选只有 3 条时跳过 `rerank`；范围显然时跳过 `estimate_scope` |
| **C** | **自治循环的迭代次数差异** | `probe_solve` 探 1~3 轮都行，只要不超预算池且结论相同 |

## 与之对称的三条**否决**规则（`forbidden_paths`）

| # | 规则 | 为什么 |
|---|---|---|
| **D** | **跳过确定性节点** | `compile_spec` / `validate` / `human_gate` 一步都不能省 —— 它们是「100% 合规」的证据链本身 |
| **E** | **用弱工具替代强工具** | 用 `sql_query` 手写递归代替 `prereq_cte`：偶尔答对，但先修展开（S-01 的类→门）迟早写错 |
| **F** | **不调工具直接回答** | §12.6.1 的「缺失调用率 ≤3%」，是这组指标里最重要的一条 —— 它的失效是静默的 |

> **判定方式**（§12.6.2）：工具与参数用结构化精确比对，路径用最长公共子序列相似度。
> 所以 `acceptable_paths` 给的是**完整序列**而不是规则描述 —— 判定器不需要理解规则，
> 只需要比对。规则写在这里是给人看的。
"""

from __future__ import annotations

from typing import Any

W03 = "2026W03"
W02 = "2026W02"

#: 排班链路的固定尾巴（v6 §7.5：`explain → resume_guard → human_gate → commit_plan`）
TAIL: tuple[str, ...] = ("explain", "resume_guard", "human_gate", "commit_plan", "END")


def _step(
    order: int,
    component: str,
    tool: str,
    params: dict[str, Any],
    *,
    optional: bool = False,
    alternatives: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "order": order,
        "component": component,
        "tool": tool,
        "params": params,
        "optional": optional,
        "alternatives": list(alternatives),
    }


def _item(
    item_id: str,
    flow: str,
    utterance: str,
    setup: str,
    expected: list[str],
    rationale: str,
    *,
    acceptable: tuple[list[str], ...] = (),
    forbidden: tuple[list[str], ...] = (),
    steps: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "flow": flow,
        "utterance": utterance,
        "setup": setup,
        "expected_path": expected,
        "acceptable_paths": [list(p) for p in acceptable],
        "forbidden_paths": [list(p) for p in forbidden],
        "steps": list(steps),
        "rationale": rationale,
    }


def knowledge_samples() -> list[dict[str, Any]]:
    """5 条 Knowledge 检索循环（全量 30 条）。"""
    return [
        _item(
            "TRJ-KNW-001",
            "query",
            "何超能不能排 missionB-1？",
            "基准周快照已就绪，无会话历史。",
            ["route", "knowledge", "tool:prereq_cte", "END"],
            "先修判定只有递归 CTE 算得出来。★ **规则 E 的样板**：`sql_query` 也能"
            "查到人和课目，但「A 类整体达标」这个展开（S-01：类 → 该类全部课目）"
            "要手写递归，偶尔答对、迟早写错 —— 所以用 `sql_query` 代替 `prereq_cte` "
            "列进 `forbidden_paths`。而在 `prereq_cte` 之后**再**查一次人员事实核对，"
            "属于冗余调用（另有指标统计），路径仍判对。",
            acceptable=(["route", "knowledge", "tool:prereq_cte", "tool:sql_query", "END"],),
            forbidden=(
                ["route", "knowledge", "tool:sql_query", "END"],
                ["route", "knowledge", "END"],
            ),
            steps=(
                _step(
                    1, "knowledge", "prereq_cte", {"person_id": "P08", "mission_id": "missionB-1"}
                ),
            ),
        ),
        _item(
            "TRJ-KNW-002",
            "query",
            "刘斌的仪表等级什么时候到期？",
            "基准周快照已就绪。",
            ["route", "knowledge", "tool:sql_query", "END"],
            "单表事实，一次精确查询就够。★ 这里 `memory.search` **不是**等价替代："
            "M5 §9.1 记着「`memory.search` 的语义记忆那一路目前走 BM25」，"
            "拿它查到期日是在用模糊匹配问一个精确问题 —— 落 `forbidden_paths`。"
            "但在查完之后补一次 `memory.search` 确认有没有更新的版本，是合理的谨慎"
            "（规则 A 的变体：并列的补充调用）。",
            acceptable=(["route", "knowledge", "tool:sql_query", "tool:memory.search", "END"],),
            forbidden=(
                ["route", "knowledge", "tool:memory.search", "END"],
                ["route", "knowledge", "END"],
            ),
            steps=(
                _step(
                    1,
                    "knowledge",
                    "sql_query",
                    {
                        "sql": "SELECT expiry_date FROM qualifications WHERE person_id = :pid",
                        "params": {"pid": "P04"},
                        "limit": 10,
                    },
                ),
            ),
        ),
        _item(
            "TRJ-KNW-003",
            "query",
            "上上周为什么把 missionF-1 推迟了？",
            "库里有 20 周的情景记忆时间线（见 memory_320 的 episodic_timeline）。",
            ["route", "knowledge", "tool:memory.search", "tool:bm25_search", "END"],
            "情景回忆：先查记忆、再用关键词补一路。★ **规则 A 与 B 各出一条替代**："
            "两路召回的先后不影响结果（A）；记忆里已经命中当周的冲突解决摘要时，"
            "第二路可以不要（B）。★ 反过来，只用 `vector_search` 而不碰记忆是错的 ——"
            "情景记忆的权威内容在 PG，向量库里只有摘要（§6.2）。",
            acceptable=(
                ["route", "knowledge", "tool:bm25_search", "tool:memory.search", "END"],
                ["route", "knowledge", "tool:memory.search", "END"],
            ),
            forbidden=(
                ["route", "knowledge", "tool:vector_search", "END"],
                ["route", "knowledge", "END"],
            ),
            steps=(
                _step(
                    1,
                    "knowledge",
                    "memory.search",
                    {"query": "missionF-1 推迟", "kinds": ["episodic"], "top_k": 5},
                ),
                _step(
                    2,
                    "knowledge",
                    "bm25_search",
                    {"query": "missionF-1 推迟 原因", "top_k": 5},
                    optional=True,
                ),
            ),
        ),
        _item(
            "TRJ-KNW-004",
            "query",
            "AC73 是什么机型？这周还能用吗？",
            "基准周快照已就绪；AC73 在 2026-01-09 全天定检。",
            ["route", "knowledge", "tool:sql_query", "tool:sql_query", "END"],
            "一句话里两个问题（机型 + 可用性）。★ **规则 B 的典型**：分两次查、"
            "还是一次 JOIN 查完，都对 —— 判定器按工具与参数比对，一次查完时"
            "第二步缺席不算缺失调用，因为它标了 `optional`。",
            acceptable=(["route", "knowledge", "tool:sql_query", "END"],),
            forbidden=(["route", "knowledge", "END"],),
            steps=(
                _step(
                    1,
                    "knowledge",
                    "sql_query",
                    {
                        "sql": "SELECT aircraft_type FROM aircraft WHERE aircraft_id = :aid",
                        "params": {"aid": "AC73"},
                        "limit": 1,
                    },
                ),
                _step(
                    2,
                    "knowledge",
                    "sql_query",
                    {
                        "sql": "SELECT * FROM maintenance WHERE aircraft_id = :aid",
                        "params": {"aid": "AC73"},
                        "limit": 10,
                    },
                    optional=True,
                ),
            ),
        ),
        _item(
            "TRJ-KNW-005",
            "query",
            "给我讲讲何超现在的整体情况",
            "基准周快照已就绪。问题开放，需要多轮自主检索。",
            [
                "route",
                "knowledge",
                "tool:sql_query",
                "tool:prereq_cte",
                "tool:vector_search",
                "tool:rerank",
                "END",
            ],
            "★ **规则 C 的样板**：开放式问题，模型自己决定查几轮。步数上限 6"
            "（`KNOWLEDGE_MAX_STEPS`），3~5 步都是合理的深度 —— 少一轮少一点细节，"
            "不是错。**但下限是硬的**：一次工具都不调就开始讲，属于缺失调用（规则 F），"
            "而这一族恰恰是最容易凭参数记忆编出流畅答案的地方。"
            "另：候选本来就只有几条时跳过 `rerank` 属于规则 B。",
            acceptable=(
                [
                    "route",
                    "knowledge",
                    "tool:sql_query",
                    "tool:prereq_cte",
                    "tool:vector_search",
                    "END",
                ],
                ["route", "knowledge", "tool:sql_query", "tool:prereq_cte", "END"],
                [
                    "route",
                    "knowledge",
                    "tool:prereq_cte",
                    "tool:sql_query",
                    "tool:vector_search",
                    "tool:rerank",
                    "END",
                ],
            ),
            forbidden=(["route", "knowledge", "END"],),
            steps=(
                _step(
                    1,
                    "knowledge",
                    "sql_query",
                    {
                        "sql": "SELECT * FROM persons WHERE person_id = :pid",
                        "params": {"pid": "P08"},
                        "limit": 1,
                    },
                ),
                _step(
                    2, "knowledge", "prereq_cte", {"person_id": "P08", "mission_id": "missionB-1"}
                ),
                _step(
                    3,
                    "knowledge",
                    "vector_search",
                    {"query": "何超 训练进度", "top_k": 5, "collection": "entity_summaries"},
                    optional=True,
                ),
                _step(
                    4,
                    "knowledge",
                    "rerank",
                    {"query": "何超 训练进度", "candidates": [], "top_k": 5},
                    optional=True,
                ),
            ),
        ),
    ]


def diagnosis_samples() -> list[dict[str, Any]]:
    """4 条 Diagnosis 探测循环（全量 25 条）。"""
    return [
        _item(
            "TRJ-DIA-001",
            "diagnosis",
            "给所有人排下周的班",
            "三名教员整周不可用（§12.3 的 I1 构造）。求解返回 INFEASIBLE。",
            [
                "route",
                "planner",
                "compile_spec",
                "solve",
                "diagnosis",
                "tool:min_conflict_set",
                "tool:blame_chain",
                "tool:probe_solve",
                "tool:rank_relaxations",
                "human_gate",
                "END",
            ],
            "I1：带飞需求 9 而一个带飞教员岗都没有，归因落在 C03/C04/C13。"
            "★ **规则 C**：`probe_solve` 探 1~3 轮都可接受（预算池 5 次 / 120s），"
            "探得多一点只是更谨慎。★ **规则 D 的反例**：跳过 `probe_solve` 直接 "
            "`rank_relaxations` 判错 —— v6 §3.9.1 要求**每条松弛提案必经探针实证验证**，"
            "没验过的提案根本不许呈现。",
            acceptable=(
                [
                    "route",
                    "planner",
                    "compile_spec",
                    "solve",
                    "diagnosis",
                    "tool:min_conflict_set",
                    "tool:probe_solve",
                    "tool:probe_solve",
                    "tool:rank_relaxations",
                    "human_gate",
                    "END",
                ],
                [
                    "route",
                    "planner",
                    "compile_spec",
                    "solve",
                    "diagnosis",
                    "tool:min_conflict_set",
                    "tool:blame_chain",
                    "tool:probe_solve",
                    "tool:probe_solve",
                    "tool:probe_solve",
                    "tool:rank_relaxations",
                    "human_gate",
                    "END",
                ],
            ),
            forbidden=(
                [
                    "route",
                    "planner",
                    "compile_spec",
                    "solve",
                    "diagnosis",
                    "tool:min_conflict_set",
                    "tool:rank_relaxations",
                    "human_gate",
                    "END",
                ],
                [
                    "route",
                    "planner",
                    "compile_spec",
                    "solve",
                    "validate",
                    "explain",
                    "resume_guard",
                    "human_gate",
                    "commit_plan",
                    "END",
                ],
            ),
            steps=(
                _step(
                    1, "diagnosis", "min_conflict_set", {"iso_week": W03, "scope_persons": ["ALL"]}
                ),
                _step(
                    2,
                    "diagnosis",
                    "blame_chain",
                    {"person_id": "P08", "mission_id": "missionB-1"},
                    optional=True,
                ),
                _step(
                    3,
                    "diagnosis",
                    "probe_solve",
                    {"iso_week": W03, "relaxations": ["TIER_1"], "time_limit_s": 30},
                ),
                _step(
                    4, "diagnosis", "rank_relaxations", {"proposals": [], "prefer": "least_arrears"}
                ),
            ),
        ),
        _item(
            "TRJ-DIA-002",
            "diagnosis",
            "本周给所有人排班",
            "六架 JL-8 全部整周维护（§12.3 的 I2 构造）。求解返回 INFEASIBLE，"
            "且**没有任何 R2 方案可解**。",
            [
                "route",
                "planner",
                "compile_spec",
                "solve",
                "diagnosis",
                "tool:min_conflict_set",
                "tool:probe_solve",
                "tool:probe_solve",
                "human_gate",
                "END",
            ],
            "I2：学员只持 JL-8 资质，机队全停 = A 类每周必飞封死。★ 正确终局是"
            "**升级人工**，不是给一个「0 架次 + 全量欠账」的空方案 —— M2-A 实测发现的"
            "那条「一个架次都不排不算解决方案」就落在这里。所以路径里**没有** "
            "`rank_relaxations`（无提案可排），也没有 `commit_plan`。",
            acceptable=(
                [
                    "route",
                    "planner",
                    "compile_spec",
                    "solve",
                    "diagnosis",
                    "tool:min_conflict_set",
                    "tool:blame_chain",
                    "tool:probe_solve",
                    "human_gate",
                    "END",
                ],
            ),
            forbidden=(
                [
                    "route",
                    "planner",
                    "compile_spec",
                    "solve",
                    "diagnosis",
                    "tool:min_conflict_set",
                    "tool:probe_solve",
                    "tool:rank_relaxations",
                    "human_gate",
                    "commit_plan",
                    "END",
                ],
            ),
            steps=(
                _step(
                    1, "diagnosis", "min_conflict_set", {"iso_week": W02, "scope_persons": ["ALL"]}
                ),
                _step(
                    2,
                    "diagnosis",
                    "probe_solve",
                    {"iso_week": W02, "relaxations": ["TIER_1"], "time_limit_s": 30},
                ),
                _step(
                    3,
                    "diagnosis",
                    "probe_solve",
                    {"iso_week": W02, "relaxations": ["TIER_2"], "time_limit_s": 30},
                ),
            ),
        ),
        _item(
            "TRJ-DIA-003",
            "diagnosis",
            "下周排班",
            "服务学员机型的跑道全部关闭（§12.3 的 I5 构造）。INFEASIBLE，"
            "约束9 确实进了最小冲突集。",
            [
                "route",
                "planner",
                "compile_spec",
                "solve",
                "diagnosis",
                "tool:min_conflict_set",
                "tool:probe_solve",
                "human_gate",
                "END",
            ],
            "I5：跑道全关 → 起降密度组把候选压成 0。★ **规则 B**：冲突集已经把"
            "约束9 明明白白列出来了，再调 `blame_chain` 展开归因链只是锦上添花，"
            "省掉不算错。★ 但 `probe_solve` 不能省（规则 D 的同一条精神）—— 不经探针验证的"
            "松弛提案不许呈现，哪怕冲突集看起来已经很清楚。",
            acceptable=(
                [
                    "route",
                    "planner",
                    "compile_spec",
                    "solve",
                    "diagnosis",
                    "tool:min_conflict_set",
                    "tool:blame_chain",
                    "tool:probe_solve",
                    "human_gate",
                    "END",
                ],
            ),
            forbidden=(
                [
                    "route",
                    "planner",
                    "compile_spec",
                    "solve",
                    "diagnosis",
                    "tool:min_conflict_set",
                    "human_gate",
                    "END",
                ],
            ),
            steps=(
                _step(
                    1, "diagnosis", "min_conflict_set", {"iso_week": W03, "scope_persons": ["ALL"]}
                ),
                _step(
                    2,
                    "diagnosis",
                    "probe_solve",
                    {"iso_week": W03, "relaxations": ["TIER_1"], "time_limit_s": 30},
                ),
            ),
        ),
        _item(
            "TRJ-DIA-004",
            "diagnosis",
            "本周排班",
            "IFR Route 整周容量降为 0（I3）。INFEASIBLE。**探针预算池被前几轮探测"
            "耗尽**（5 次上限）。",
            [
                "route",
                "planner",
                "compile_spec",
                "solve",
                "diagnosis",
                "tool:min_conflict_set",
                "tool:probe_solve",
                "tool:probe_solve",
                "tool:probe_solve",
                "tool:probe_solve",
                "tool:probe_solve",
                "human_gate",
                "END",
            ],
            "★ 预算边界的专项：探针池是 5 次 / 单次 30s / 累计 120s，**与 Harness 的 "
            "LLM 预算互不挤占**（§3.9.2）。池子空了就停，「已验证的提案照常呈现」——"
            "这是正常终止，不是失败。★ 第 6 次 `probe_solve` 判错：不是因为多探一次"
            "有害，而是因为**预算闸必须真的拦得住**（§12.5.1 的预算熔断正确率 100%）。",
            acceptable=(
                [
                    "route",
                    "planner",
                    "compile_spec",
                    "solve",
                    "diagnosis",
                    "tool:min_conflict_set",
                    "tool:probe_solve",
                    "tool:probe_solve",
                    "tool:probe_solve",
                    "tool:rank_relaxations",
                    "human_gate",
                    "END",
                ],
            ),
            forbidden=(
                [
                    "route",
                    "planner",
                    "compile_spec",
                    "solve",
                    "diagnosis",
                    "tool:min_conflict_set",
                    "tool:probe_solve",
                    "tool:probe_solve",
                    "tool:probe_solve",
                    "tool:probe_solve",
                    "tool:probe_solve",
                    "tool:probe_solve",
                    "human_gate",
                    "END",
                ],
            ),
            steps=(
                _step(
                    1, "diagnosis", "min_conflict_set", {"iso_week": W02, "scope_persons": ["ALL"]}
                ),
                _step(
                    2,
                    "diagnosis",
                    "probe_solve",
                    {"iso_week": W02, "relaxations": ["TIER_1"], "time_limit_s": 30},
                ),
            ),
        ),
    ]


def workflow_samples() -> list[dict[str, Any]]:
    """6 条固定序列流程：排班 2 · 重排 1 · 修订 2 · 摄取 1。"""
    return [
        _item(
            "TRJ-SCH-001",
            "schedule",
            "给所有人排下周的班",
            "基准周快照已就绪，无既有计划。",
            [
                "route",
                "planner",
                "tool:resolve_week",
                "tool:estimate_scope",
                "tool:propose_solve_intent",
                "compile_spec",
                "solve",
                "validate",
                *TAIL,
            ],
            "★ **固定序列的样板**：`compile_spec → solve → validate → explain → "
            "resume_guard → human_gate → commit_plan` 是**静态边**，不是模型选的。"
            "轨迹评估在这一族上验的是「没跑偏」。★ 规则 B：全员范围时 `estimate_scope` "
            "可省（范围显然）。★ **规则 D 的两条反例**都列进了 `forbidden_paths`："
            "跳过 `compile_spec` 直接求解、跳过 `validate` 直接解释 —— "
            "前者让语义开关不生效，后者让「100% 合规」失去证据。",
            acceptable=(
                [
                    "route",
                    "planner",
                    "tool:resolve_week",
                    "tool:propose_solve_intent",
                    "compile_spec",
                    "solve",
                    "validate",
                    *TAIL,
                ],
                [
                    "route",
                    "planner",
                    "tool:estimate_scope",
                    "tool:resolve_week",
                    "tool:propose_solve_intent",
                    "compile_spec",
                    "solve",
                    "validate",
                    *TAIL,
                ],
            ),
            forbidden=(
                [
                    "route",
                    "planner",
                    "tool:resolve_week",
                    "tool:propose_solve_intent",
                    "solve",
                    "validate",
                    *TAIL,
                ],
                [
                    "route",
                    "planner",
                    "tool:resolve_week",
                    "tool:propose_solve_intent",
                    "compile_spec",
                    "solve",
                    *TAIL,
                ],
            ),
            steps=(
                _step(
                    1,
                    "planner",
                    "resolve_week",
                    {"surface": "下周", "reference_date": "2026-01-05"},
                ),
                _step(
                    2,
                    "planner",
                    "estimate_scope",
                    {"iso_week": W03, "scope_persons": ["ALL"], "scope_missions": ["ALL"]},
                    optional=True,
                ),
                _step(
                    3,
                    "planner",
                    "propose_solve_intent",
                    {
                        "iso_week": W03,
                        "intent": {"scope_persons": "ALL"},
                        "rationale": "全员排班，无既有计划，取中性冻结档",
                    },
                ),
            ),
        ),
        _item(
            "TRJ-SCH-002",
            "schedule",
            "生成何超与罗磊本周的训练时间表",
            "基准周快照已就绪，无既有计划。",
            [
                "route",
                "planner",
                "tool:resolve_person",
                "tool:resolve_person",
                "tool:propose_solve_intent",
                "compile_spec",
                "solve",
                "validate",
                *TAIL,
            ],
            "指定对象排班。★ **规则 A**：两次 `resolve_person` 的先后无所谓 —— "
            "判定器按工具与参数比对，参数集合相同即可。★ 只消解一个人是**错**的"
            "（漏掉的那位会安静地不进排班范围），所以它进 `forbidden_paths` 而不是"
            "「可接受的省略」—— 规则 B 只覆盖**信息已足够**的省略，这里信息不够。",
            acceptable=(
                [
                    "route",
                    "planner",
                    "tool:resolve_person",
                    "tool:resolve_person",
                    "tool:estimate_scope",
                    "tool:propose_solve_intent",
                    "compile_spec",
                    "solve",
                    "validate",
                    *TAIL,
                ],
            ),
            forbidden=(
                [
                    "route",
                    "planner",
                    "tool:resolve_person",
                    "tool:propose_solve_intent",
                    "compile_spec",
                    "solve",
                    "validate",
                    *TAIL,
                ],
            ),
            steps=(
                _step(1, "planner", "resolve_person", {"surface": "何超"}),
                _step(2, "planner", "resolve_person", {"surface": "罗磊"}),
                _step(
                    3,
                    "planner",
                    "propose_solve_intent",
                    {
                        "iso_week": W02,
                        "intent": {"scope_persons": ["P08", "P05"]},
                        "rationale": "用户点名两位学员",
                    },
                ),
            ),
        ),
        _item(
            "TRJ-RSC-001",
            "reschedule",
            "高超一周都参加不了训练，AC84 本周维修，重新排班",
            "本周已有一版**已批准**的计划。",
            [
                "route",
                "planner",
                "tool:resolve_person",
                "tool:resolve_aircraft",
                "tool:assess_disruption",
                "tool:propose_solve_intent",
                "compile_spec",
                "solve",
                "validate",
                *TAIL,
            ],
            "重排。★ `assess_disruption` **不可省** —— 它是影响面探测与自我降档的"
            "输入（§7.3.3 ①），省掉就等于拿一个未经评估的冻结档去重排，"
            "扰动可能远超用户预期。所以它不在规则 B 的范围里。"
            "★ 规则 A：两个 `resolve_*` 的先后可以对调。",
            acceptable=(
                [
                    "route",
                    "planner",
                    "tool:resolve_aircraft",
                    "tool:resolve_person",
                    "tool:assess_disruption",
                    "tool:propose_solve_intent",
                    "compile_spec",
                    "solve",
                    "validate",
                    *TAIL,
                ],
            ),
            forbidden=(
                [
                    "route",
                    "planner",
                    "tool:resolve_person",
                    "tool:resolve_aircraft",
                    "tool:propose_solve_intent",
                    "compile_spec",
                    "solve",
                    "validate",
                    *TAIL,
                ],
            ),
            steps=(
                _step(1, "planner", "resolve_person", {"surface": "高超"}),
                _step(2, "planner", "resolve_aircraft", {"surface": "AC84"}),
                _step(
                    3,
                    "planner",
                    "assess_disruption",
                    {
                        "iso_week": W02,
                        "baseline_plan_id": "plan_current",
                        "changed_persons": ["P02"],
                        "changed_aircraft": ["AC84"],
                    },
                ),
                _step(
                    4,
                    "planner",
                    "propose_solve_intent",
                    {
                        "iso_week": W02,
                        "intent": {"scope_persons": "ALL"},
                        "rationale": "两处扰动，影响面中等，取 BALANCED",
                    },
                ),
            ),
        ),
        _item(
            "TRJ-REV-001",
            "revision",
            "把张勇的 missionC-2 挪到周四以后",
            "已经排出一版方案，用户在人工门禁上选了 `REVISE` 并说了这句话。",
            [
                "human_gate",
                "planner",
                "tool:translate_revision",
                "human_gate",
                "solve",
                "validate",
                *TAIL,
            ],
            "★ **`Z-19` 的两次门禁往返**，本集里最容易标错的一条。翻译完**先回门禁"
            "展示「我理解为…」**，用户 `APPROVE` 之后才 `solve` 重解 —— 那一屏的 "
            "`APPROVE` 是「去重解」不是「去归档」。"
            "★ `forbidden_paths` 里的那条（翻译完直接 `solve`）正是 v6 反模式清单点名的"
            "「先重解再展示」：顺序反了等于「翻译错了也已经排了一版」。",
            acceptable=(
                [
                    "human_gate",
                    "planner",
                    "tool:translate_revision",
                    "tool:check_authority",
                    "human_gate",
                    "solve",
                    "validate",
                    *TAIL,
                ],
            ),
            forbidden=(
                ["human_gate", "planner", "tool:translate_revision", "solve", "validate", *TAIL],
            ),
            steps=(
                _step(
                    1,
                    "planner",
                    "translate_revision",
                    {
                        "utterance": "把张勇的 missionC-2 挪到周四以后",
                        "round_no": 1,
                        "iso_week": W02,
                    },
                ),
                _step(
                    2, "planner", "check_authority", {"tier": 0, "role": "scheduler"}, optional=True
                ),
            ),
        ),
        _item(
            "TRJ-REV-002",
            "revision",
            "刚才那条不算了，撤销",
            "上一轮已注入一条 `SHIFT_WINDOW` 修订并重解出了新方案。",
            [
                "human_gate",
                "planner",
                "tool:translate_revision",
                "human_gate",
                "solve",
                "validate",
                *TAIL,
            ],
            "★ **`Z-21` 的撤销语义**：`undo` = 去掉那条约束**再解一次**，"
            "**不是把旧方案取回来**。所以路径与一次普通修订完全相同 —— 仍要过回显门禁、"
            "仍要重解、仍要过校验。★ `forbidden_paths` 里那条（直接回到 `human_gate` "
            "呈现旧方案）是最直觉、也最错的做法：重解结果不必与当初那版逐字节相同"
            "（最小扰动锚定**当前**方案），要断言的是约束集回到了那一版。",
            acceptable=(
                [
                    "human_gate",
                    "planner",
                    "tool:translate_revision",
                    "human_gate",
                    "solve",
                    "validate",
                    "explain",
                    "resume_guard",
                    "human_gate",
                    "END",
                ],
            ),
            forbidden=(["human_gate", "planner", "human_gate", "commit_plan", "END"],),
            steps=(
                _step(
                    1,
                    "planner",
                    "translate_revision",
                    {"utterance": "刚才那条不算了，撤销", "round_no": 2, "iso_week": W02},
                ),
            ),
        ),
        _item(
            "TRJ-ING-001",
            "ingest",
            "把这四份 PDF 导进来",
            "用户上传 personnel / aircraft / missions / rules 四份 PDF，"
            "走 `POST /api/v1/ingest`（**不在对话图内**）。",
            [
                "ingest.prepare",
                "tool:classify_doc",
                "tool:parse_personnel",
                "tool:parse_aircraft",
                "tool:parse_missions",
                "tool:parse_rules",
                "tool:diff_snapshot",
                "ingest.gate",
                "ingest.commit",
            ],
            "摄取的两段式：`prepare`（安全闸 → 分类 → 抽取 → 校验 → Diff，**只读**）"
            "→ **人工确认** → `commit`（落库）。★ 规则 A：四次 `parse_*` 的先后无所谓。"
            "★ **`forbidden_paths` 里那条跳过 `ingest.gate` 的**是本条的要害：v6 §5.1 "
            "的人工确认是**硬性门禁**，代码里把 `GateDecision` 做成必传参数就是为了"
            "让「先落库再说」写不出来。",
            acceptable=(
                [
                    "ingest.prepare",
                    "tool:classify_doc",
                    "tool:parse_rules",
                    "tool:parse_personnel",
                    "tool:parse_aircraft",
                    "tool:parse_missions",
                    "tool:diff_snapshot",
                    "ingest.gate",
                    "ingest.commit",
                ],
            ),
            forbidden=(
                [
                    "ingest.prepare",
                    "tool:classify_doc",
                    "tool:parse_personnel",
                    "tool:parse_aircraft",
                    "tool:parse_missions",
                    "tool:parse_rules",
                    "tool:diff_snapshot",
                    "ingest.commit",
                ],
            ),
            steps=(
                _step(
                    1,
                    "extract",
                    "classify_doc",
                    {"filename": "personnel.pdf", "text_head": "人员信息表…"},
                ),
                _step(
                    2,
                    "extract",
                    "parse_personnel",
                    {"document_id": "doc_personnel", "page_range": "1-3"},
                ),
                _step(
                    3,
                    "extract",
                    "parse_aircraft",
                    {"document_id": "doc_aircraft", "page_range": "1-2"},
                ),
                _step(
                    4,
                    "extract",
                    "parse_missions",
                    {"document_id": "doc_missions", "page_range": "1-2"},
                ),
                _step(
                    5, "extract", "parse_rules", {"document_id": "doc_rules", "page_range": "1-4"}
                ),
                _step(
                    6,
                    "extract",
                    "diff_snapshot",
                    {"base_snapshot_id": "snap_9724982865ee", "new_snapshot_id": "snap_pending"},
                ),
            ),
        ),
    ]


def build_sample() -> list[dict[str, Any]]:
    return [*knowledge_samples(), *diagnosis_samples(), *workflow_samples()]
