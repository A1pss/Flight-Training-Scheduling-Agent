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

from tests.datasets.tool_params import params_for

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
            steps=(_step(1, "knowledge", "prereq_cte", params_for("prereq_cte")),),
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
                    params_for(
                        "sql_query",
                        sql="SELECT expiry_date FROM qualifications WHERE person_id = :pid",
                        params={"pid": "P04"},
                    ),
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
                    params_for("memory.search", query="missionF-1 推迟", kinds=["episodic"]),
                ),
                _step(
                    2,
                    "knowledge",
                    "bm25_search",
                    params_for("bm25_search"),
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
                    params_for(
                        "sql_query",
                        sql="SELECT aircraft_type FROM aircraft WHERE aircraft_id = :aid",
                        params={"aid": "AC73"},
                        limit=1,
                    ),
                ),
                _step(
                    2,
                    "knowledge",
                    "sql_query",
                    params_for(
                        "sql_query",
                        sql="SELECT * FROM maintenance WHERE aircraft_id = :aid",
                        params={"aid": "AC73"},
                    ),
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
                    params_for(
                        "sql_query",
                        sql="SELECT * FROM persons WHERE person_id = :pid",
                        params={"pid": "P08"},
                        limit=1,
                    ),
                ),
                _step(2, "knowledge", "prereq_cte", params_for("prereq_cte")),
                _step(
                    3,
                    "knowledge",
                    "vector_search",
                    params_for("vector_search"),
                    optional=True,
                ),
                _step(
                    4,
                    "knowledge",
                    "rerank",
                    params_for("rerank"),
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
                    1, "diagnosis", "min_conflict_set", params_for("min_conflict_set", iso_week=W03)
                ),
                _step(
                    2,
                    "diagnosis",
                    "blame_chain",
                    params_for("prereq_cte"),
                    optional=True,
                ),
                _step(
                    3,
                    "diagnosis",
                    "probe_solve",
                    params_for("probe_solve", iso_week=W03),
                ),
                _step(4, "diagnosis", "rank_relaxations", params_for("rank_relaxations")),
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
                    1, "diagnosis", "min_conflict_set", params_for("min_conflict_set", iso_week=W02)
                ),
                _step(
                    2,
                    "diagnosis",
                    "probe_solve",
                    params_for("probe_solve", iso_week=W02),
                ),
                _step(
                    3,
                    "diagnosis",
                    "probe_solve",
                    params_for("probe_solve", iso_week=W02, relaxations=["TIER_2"]),
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
                    1, "diagnosis", "min_conflict_set", params_for("min_conflict_set", iso_week=W03)
                ),
                _step(
                    2,
                    "diagnosis",
                    "probe_solve",
                    params_for("probe_solve", iso_week=W03),
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
                    1, "diagnosis", "min_conflict_set", params_for("min_conflict_set", iso_week=W02)
                ),
                _step(
                    2,
                    "diagnosis",
                    "probe_solve",
                    params_for("probe_solve", iso_week=W02),
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
                    params_for("resolve_week"),
                ),
                _step(
                    2,
                    "planner",
                    "estimate_scope",
                    params_for("estimate_scope", iso_week=W03),
                    optional=True,
                ),
                _step(
                    3,
                    "planner",
                    "propose_solve_intent",
                    params_for(
                        "propose_solve_intent",
                        iso_week=W03,
                        rationale="全员排班，无既有计划，取中性冻结档",
                    ),
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
                _step(1, "planner", "resolve_person", params_for("resolve_person")),
                _step(2, "planner", "resolve_person", params_for("resolve_person", surface="罗磊")),
                _step(
                    3,
                    "planner",
                    "propose_solve_intent",
                    params_for("propose_solve_intent", iso_week=W02, rationale="用户点名两位学员"),
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
                _step(1, "planner", "resolve_person", params_for("resolve_person", surface="高超")),
                _step(
                    2, "planner", "resolve_aircraft", params_for("resolve_aircraft", surface="AC84")
                ),
                _step(
                    3,
                    "planner",
                    "assess_disruption",
                    params_for("assess_disruption"),
                ),
                _step(
                    4,
                    "planner",
                    "propose_solve_intent",
                    params_for(
                        "propose_solve_intent",
                        iso_week=W02,
                        rationale="两处扰动，影响面中等，取 BALANCED",
                    ),
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
                    params_for("translate_revision"),
                ),
                _step(
                    2,
                    "planner",
                    "check_authority",
                    params_for("check_authority", requested_tier=0),
                    optional=True,
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
                    params_for("translate_revision", utterance="刚才那条不算了，撤销", round_no=2),
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
                    params_for("classify_doc"),
                ),
                _step(
                    2,
                    "extract",
                    "parse_personnel",
                    params_for("parse_personnel"),
                ),
                _step(
                    3,
                    "extract",
                    "parse_aircraft",
                    params_for("parse_aircraft"),
                ),
                _step(
                    4,
                    "extract",
                    "parse_missions",
                    params_for("parse_missions"),
                ),
                _step(5, "extract", "parse_rules", params_for("parse_rules")),
                _step(
                    6,
                    "extract",
                    "diff_snapshot",
                    params_for("diff_snapshot"),
                ),
            ),
        ),
    ]


def build_sample() -> list[dict[str, Any]]:
    return [*knowledge_samples(), *diagnosis_samples(), *workflow_samples()]


# ══════════════════════════════════════════════════════════════════════
# 全量 100：query 30 · diagnosis 25 · schedule 15 · reschedule 10 ·
#           revision 10 · ingest 10
# ══════════════════════════════════════════════════════════════════════

KNW = ["route", "knowledge"]
NO_TOOL = ["route", "knowledge", "END"]

#: 先修判定族：结构与 TRJ-KNW-001 相同，换 (人, 课目)。
#: **规则 E 的禁令逐条复制** —— 一族里只在一条上写禁令，等于只测了那一条。
PREREQ_QUERIES: tuple[tuple[str, str, str, str], ...] = (
    ("P08", "missionC-1", "何超", "缺 missionA-2，A 类未整体达标"),
    ("P08", "missionF-1", "何超", "同上，F 类先修也是 A 类整体"),
    ("P06", "missionC-2", "张勇", "★ C-2 的先修是**逐门的** missionC-1，不是「C 类整体」"),
    ("P07", "missionC-2", "陈伟", "同上，陈伟也没飞过 C-1"),
    ("P05", "missionG-1", "罗磊", "★ 双重排除：G-1 走 JL-9（学员无机型资质）且先修含 F 类"),
    ("P04", "missionE-2", "刘斌", "正例：刘斌全 12 门完成，E-2 的逐门先修 E-1 已达标"),
)

#: 单表事实族：一次 `sql_query` 就够。
FACT_QUERIES: tuple[tuple[str, str, dict[str, Any], str], ...] = (
    ("AC84 是什么机型？", "aircraft", {"aid": "AC84"}, "机型事实，JL-9"),
    ("missionE-2 要飞多久？", "missions", {"mid": "missionE-2"}, "课目时长 69 分钟"),
    ("IFR Route 的容量是多少？", "airspaces", {"sid": "IFR"}, "空域容量 1，硬约束（S-10）"),
    ("孙军是什么身份？", "persons", {"pid": "P01"}, "身份事实，教员"),
    ("AC73 这周有维护吗？", "maintenance", {"aid": "AC73"}, "维护窗事实，2026-01-09 全天定检"),
    (
        "罗磊完成了哪些课目？",
        "progress",
        {"pid": "P05"},
        "★ 读 `person_completed_missions` 事实表而不是 `training_progress.status`（Z-16）",
    ),
)

#: 情景回忆族：记忆优先，关键词补一路。
EPISODE_QUERIES: tuple[tuple[str, str], ...] = (
    ("上次 AC73 定检是哪一周？", "★ 「上次」= 最近一次；时间排序错了会召回到更早那条"),
    ("第 12 周批准的方案用了哪一档松弛？", "批准记录召回"),
    ("上一版计划里何超排了几个架次？", "历史报告召回，走 historical_reports"),
    ("上周有哪些阻塞项？", "阻塞项披露是 §12.3 的必测项，历史查询同样要取得到"),
    ("用户上次驳回是因为什么？", "驳回理由召回，§15.2 ⑥ 难负例挖掘的输入"),
)

#: 开放式多轮族：模型自己决定查几轮（规则 C）。
OPEN_QUERIES: tuple[tuple[str, str, str], ...] = (
    ("张勇的训练情况怎么样？", "P06", "开放式：事实 + 先修 + 语义补充"),
    ("这周的机队够用吗？", "P05", "★ 资源侧的开放式问题，要同时看机队与课目需求"),
    ("刘斌接下来该飞什么？", "P04", "★ 涉及 S-11 复训判定，只查进度是答不全的"),
    ("学员们的进度差在哪？", "P08", "跨人比较，最容易在没查全的情况下下结论"),
)

#: 一句两问族：分两次查还是一次 JOIN 查完都对（规则 B）。
DOUBLE_QUERIES: tuple[tuple[str, str, str], ...] = (
    ("AC49 是什么机型？周转要多久？", "AC49", "机型 + 周转，两者由同一张表给出"),
    ("何超是学员吗？他能飞 JL-9 吗？", "P08", "身份 + 机型资质"),
    ("missionC-2 的先修是什么？要飞多久？", "missionC-2", "先修 + 时长"),
    ("SAB 的容量是多少？绑了哪些课目？", "SAB", "容量 + 反查绑定课目"),
)


def knowledge_full() -> list[dict[str, Any]]:
    """25 条补足到 30（样例 5 条在前）。"""
    rows: list[dict[str, Any]] = []
    for i, (pid, mission, name, why) in enumerate(PREREQ_QUERIES, start=6):
        rows.append(
            _item(
                f"TRJ-KNW-{i:03d}",
                "query",
                f"{name}现在能排 {mission} 吗？",
                "基准周快照已就绪，无会话历史。",
                [*KNW, "tool:prereq_cte", "END"],
                f"先修判定（{name}/{pid} × {mission}）：{why}。★ 规则 E 在这一族逐条生效："
                f"`sql_query` 手写递归代替 `prereq_cte` 一律判错 —— 一族里只在一条上写禁令，"
                f"等于只测了那一条。",
                acceptable=([*KNW, "tool:prereq_cte", "tool:sql_query", "END"],),
                forbidden=([*KNW, "tool:sql_query", "END"], NO_TOOL),
                steps=(
                    _step(
                        1,
                        "knowledge",
                        "prereq_cte",
                        params_for("prereq_cte", person_id=pid, mission_id=mission),
                    ),
                ),
            )
        )
    for i, (query, table, params, why) in enumerate(FACT_QUERIES, start=12):
        rows.append(
            _item(
                f"TRJ-KNW-{i:03d}",
                "query",
                query,
                "基准周快照已就绪。",
                [*KNW, "tool:sql_query", "END"],
                f"单表事实：{why}。一次精确查询就够；★ 补一次 `memory.search` 确认时效"
                f"属于合理的谨慎（规则 A 的变体），但**只**用 `memory.search` 判错 ——"
                f"它那一路走 BM25，是拿模糊匹配问一个精确问题。",
                acceptable=([*KNW, "tool:sql_query", "tool:memory.search", "END"],),
                forbidden=([*KNW, "tool:memory.search", "END"], NO_TOOL),
                steps=(
                    _step(
                        1,
                        "knowledge",
                        "sql_query",
                        params_for(
                            "sql_query", sql=f"SELECT * FROM {table} WHERE …", params=params
                        ),
                    ),
                ),
            )
        )
    for i, (query, why) in enumerate(EPISODE_QUERIES, start=18):
        rows.append(
            _item(
                f"TRJ-KNW-{i:03d}",
                "query",
                query,
                "库里有 20 周的情景记忆时间线（memory_320 的 episodic_timeline）。",
                [*KNW, "tool:memory.search", "tool:bm25_search", "END"],
                f"情景回忆：{why}。★ 规则 A（两路顺序可换）与规则 B（记忆已命中时"
                f"第二路可省）各出一条替代；只用 `vector_search` 判错 —— "
                f"情景记忆的权威内容在 PG，向量库里只有摘要（§6.2）。",
                acceptable=(
                    [*KNW, "tool:bm25_search", "tool:memory.search", "END"],
                    [*KNW, "tool:memory.search", "END"],
                ),
                forbidden=([*KNW, "tool:vector_search", "END"], NO_TOOL),
                steps=(
                    _step(
                        1,
                        "knowledge",
                        "memory.search",
                        params_for("memory.search", query=query, kinds=["episodic"]),
                    ),
                    _step(
                        2,
                        "knowledge",
                        "bm25_search",
                        params_for("bm25_search", query=query),
                        optional=True,
                    ),
                ),
            )
        )
    for i, (query, pid, why) in enumerate(OPEN_QUERIES, start=23):
        rows.append(
            _item(
                f"TRJ-KNW-{i:03d}",
                "query",
                query,
                "基准周快照已就绪。问题开放，需要多轮自主检索。",
                [
                    *KNW,
                    "tool:sql_query",
                    "tool:prereq_cte",
                    "tool:vector_search",
                    "tool:rerank",
                    "END",
                ],
                f"开放式多轮：{why}。★ 规则 C：步数上限 6，3~5 步都是合理深度；"
                f"候选本就只有几条时跳过 `rerank` 属于规则 B。★ 下限是硬的 —— "
                f"一次工具都不调就开讲属于缺失调用（规则 F），而这一族恰恰最容易"
                f"凭参数记忆编出流畅答案。",
                acceptable=(
                    [*KNW, "tool:sql_query", "tool:prereq_cte", "tool:vector_search", "END"],
                    [*KNW, "tool:sql_query", "tool:prereq_cte", "END"],
                    [
                        *KNW,
                        "tool:prereq_cte",
                        "tool:sql_query",
                        "tool:vector_search",
                        "tool:rerank",
                        "END",
                    ],
                ),
                forbidden=(NO_TOOL,),
                steps=(
                    _step(
                        1,
                        "knowledge",
                        "sql_query",
                        params_for(
                            "sql_query",
                            sql="SELECT * FROM persons WHERE person_id = :pid",
                            params={"pid": pid},
                            limit=1,
                        ),
                    ),
                    _step(
                        2,
                        "knowledge",
                        "prereq_cte",
                        params_for("prereq_cte", person_id=pid, mission_id="missionB-1"),
                    ),
                    _step(
                        3,
                        "knowledge",
                        "vector_search",
                        params_for("vector_search", query=query),
                        optional=True,
                    ),
                    _step(
                        4,
                        "knowledge",
                        "rerank",
                        params_for("rerank", query=query),
                        optional=True,
                    ),
                ),
            )
        )
    for i, (query, entity, why) in enumerate(DOUBLE_QUERIES, start=27):
        rows.append(
            _item(
                f"TRJ-KNW-{i:03d}",
                "query",
                query,
                "基准周快照已就绪。",
                [*KNW, "tool:sql_query", "tool:sql_query", "END"],
                f"一句两问（{entity}）：{why}。★ 规则 B 的典型：分两次查、还是一次 JOIN "
                f"查完，都对 —— 第二步标了 `optional`，缺席不算缺失调用。",
                acceptable=([*KNW, "tool:sql_query", "END"],),
                forbidden=(NO_TOOL,),
                steps=(
                    _step(
                        1,
                        "knowledge",
                        "sql_query",
                        params_for(
                            "sql_query",
                            sql="SELECT … WHERE id = :id",
                            params={"id": entity},
                            limit=5,
                        ),
                    ),
                    _step(
                        2,
                        "knowledge",
                        "sql_query",
                        params_for(
                            "sql_query",
                            sql="SELECT … WHERE id = :id",
                            params={"id": entity},
                            limit=5,
                        ),
                        optional=True,
                    ),
                ),
            )
        )
    return rows


DIA_HEAD = ["route", "planner", "compile_spec", "solve", "diagnosis"]
DIA_TAIL = ["human_gate", "END"]
#: 「求解成功」的那条路 —— 诊断族里它一律是禁令：INFEASIBLE 时不该走到 commit_plan
SOLVED_PATH = ["route", "planner", "compile_spec", "solve", "validate", *TAIL]

#: 21 条诊断样本：五个不可行族 × 变体 + 预算/降级边界。
#: `probes` 是期望的 `probe_solve` 次数，`rank` 表示最终有没有可呈现的提案。
DIAGNOSIS_CASES: tuple[tuple[str, str, str, int, bool, str], ...] = (
    (
        "I1",
        "三名教员整周不可用",
        W03,
        1,
        True,
        "带飞需求 9 而带飞教员岗为 0，归因 C03/C04/C13；Tier 1 顺延带飞课目可解",
    ),
    (
        "I1",
        "三名教员整周不可用 + 训练窗压到 08:00-16:00",
        W03,
        2,
        True,
        "两处收紧叠加，探针要多探一轮才能确认 Tier 1 够不够",
    ),
    (
        "I1",
        "两名教员整周不可用、第三名只上半天",
        W02,
        2,
        True,
        "★ 边界样本：M2-A 实测「两名教员不可用」其实**可行**（单教员周上限 12 > 需求 9），"
        "所以这条要靠第三名的半天限制才真正逼到不可行",
    ),
    (
        "I2",
        "六架 JL-8 全部整周维护",
        W02,
        2,
        False,
        "学员只持 JL-8 资质，机队全停即 A 类每周必飞封死 → 无 R2 方案，升级人工",
    ),
    (
        "I2",
        "六架 JL-8 全维护 + JL-9 两架也停场",
        W03,
        1,
        False,
        "全机队停场，冲突集更短；探一轮就能确认无解",
    ),
    (
        "I2",
        "五架 JL-8 维护、AC10 每天只开两小时",
        W02,
        3,
        False,
        "★ 不是「全停」而是「几乎全停」，探针要多探几轮才能排除 Tier 1/Tier 2",
    ),
    ("I3", "IFR Route 整周容量降为 0", W02, 2, True, "C 类本周顺延、欠账记入下周；归因 C06/C13"),
    ("I3", "IFR 与 RT2 同时关闭", W03, 2, True, "两个容量为 1 的空域同时关，B-1 与 C 类一起顺延"),
    (
        "I3",
        "六个空域容量全部降为 1",
        W02,
        3,
        True,
        "★ 不是关闭而是**压容量**，SAA/SAB 从 2 降到 1；这类扰动最容易被误判为可行",
    ),
    ("I3", "SAB 容量降为 0", W03, 1, True, "SAB 绑 A-2/F-1/G-1，关掉它直接压 A 类每周必飞"),
    (
        "I4",
        "训练窗压缩至 06:00-06:30",
        W02,
        1,
        True,
        "30 分钟窗装不下任何 B/C/F 类课目（35~69 分钟），最小冲突集含 C01_window",
    ),
    ("I4", "训练窗压缩至 06:00-07:00", W03, 2, True, "60 分钟窗只装得下短课目，边界比上一条松一档"),
    ("I4", "每天只开 06:00-06:20，且 AC73 定检", W02, 2, True, "时间窗 + 机队双重收紧"),
    (
        "I4",
        "训练窗按天递减（周一 8 小时到周日 30 分钟）",
        W03,
        3,
        True,
        "★ 逐日不同的窗口，探针要判断顺延到哪一天才有意义",
    ),
    (
        "I5",
        "服务学员机型的跑道全部关闭",
        W03,
        1,
        True,
        "起降密度组把候选压成 0，约束9 确实进冲突集（这正是 I5 的设计目的）",
    ),
    (
        "I5",
        "RWY-1 关闭（JL-9 无处起降）",
        W02,
        2,
        True,
        "★ 只关一条：JL-9 全停但 JL-8 仍可走 RWY-2 —— 影响面比 I5 主构造小得多",
    ),
    (
        "I5",
        "两条跑道每天各只开一小时",
        W03,
        3,
        True,
        "跑道不是关闭而是限时，20 分钟窗口的密度上限成为瓶颈",
    ),
    (
        "I5",
        "RWY-2 关闭 + 起降间隔改为 20 分钟",
        W02,
        2,
        True,
        "★ 用户把约束9 的 7 分钟间隔改严（允许的方向），叠加跑道关闭后不可行",
    ),
    (
        "I1",
        "三名教员不可用，且探针预算在前序请求里已用掉 4 次",
        W02,
        1,
        True,
        "★ 预算残量场景：池子只剩 1 次，探完即停，「已验证的提案照常呈现」",
    ),
    (
        "I2",
        "机队全停，探针预算已耗尽",
        W03,
        0,
        False,
        "★ 预算为 0：**一次探针都不调**在这里是对的 —— 池子空了继续调才是错的。"
        "这条与规则 F（不调工具直接答）不冲突：不调的原因是闸拦住了，不是模型偷懒",
    ),
    (
        "I3",
        "IFR 容量为 0，且 Harness 不可用（LLM 挂了）",
        W02,
        -1,
        True,
        "★ 降级路径：`harness=None` 时诊断照常给出完整结果（确定性四步），"
        "只是没有自主探测那一层，`autonomous=False` 如实标着 —— **诊断能力不依赖 LLM**",
    ),
)


def diagnosis_full() -> list[dict[str, Any]]:
    """21 条补足到 25（样例 4 条在前）。"""
    rows: list[dict[str, Any]] = []
    for i, (family, setup, week, probes, rank, why) in enumerate(DIAGNOSIS_CASES, start=5):
        # probes < 0：Harness 不可用，**一次工具调用都没有** —— 确定性四步照常跑完
        path = [*DIA_HEAD] if probes < 0 else [*DIA_HEAD, "tool:min_conflict_set"]
        path += ["tool:probe_solve"] * max(probes, 0)
        if rank and probes > 0:
            path.append("tool:rank_relaxations")
        path += DIA_TAIL

        acceptable: list[list[str]] = []
        if probes > 0:
            with_blame = [*DIA_HEAD, "tool:min_conflict_set", "tool:blame_chain"]
            with_blame += ["tool:probe_solve"] * probes
            if rank:
                with_blame.append("tool:rank_relaxations")
            acceptable.append([*with_blame, *DIA_TAIL])
            more = [*DIA_HEAD, "tool:min_conflict_set"]
            more += ["tool:probe_solve"] * (probes + 1)
            if rank:
                more.append("tool:rank_relaxations")
            acceptable.append([*more, *DIA_TAIL])

        if probes == 0:
            # 预算已空：`min_conflict_set` 不吃探针预算，仍可调；多调一次归因也无妨
            acceptable.append([*DIA_HEAD, "tool:min_conflict_set", "tool:blame_chain", *DIA_TAIL])

        forbidden: list[list[str]] = [SOLVED_PATH]
        if probes > 0:
            skipped = [*DIA_HEAD, "tool:min_conflict_set"]
            if rank:
                skipped.append("tool:rank_relaxations")
            forbidden.append([*skipped, *DIA_TAIL])
        else:
            forbidden.append([*DIA_HEAD, "tool:min_conflict_set", "tool:probe_solve", *DIA_TAIL])

        steps_head = probes >= 0

        steps = (
            [
                _step(
                    1,
                    "diagnosis",
                    "min_conflict_set",
                    params_for("min_conflict_set", iso_week=week),
                )
            ]
            if steps_head
            else []
        )
        if probes > 0:
            steps.append(
                _step(
                    2,
                    "diagnosis",
                    "probe_solve",
                    params_for("probe_solve", iso_week=week),
                )
            )
        if rank and probes > 0:
            steps.append(_step(3, "diagnosis", "rank_relaxations", params_for("rank_relaxations")))

        outcome = "有可呈现的提案" if rank else "**无 R2 方案 → 升级人工**"
        rows.append(
            _item(
                f"TRJ-DIA-{i:03d}",
                "diagnosis",
                "给所有人排班" if week == W02 else "给所有人排下周的班",
                f"{family} 族：{setup}。求解返回 INFEASIBLE。",
                path,
                f"{family}：{why}。终局是{outcome}。★ 规则 C：多探一轮可接受（预算池 5 次 / "
                f"120s）；★ 规则 B：冲突集已经足够清楚时 `blame_chain` 可省。"
                + (
                    "★ 禁令：跳过 `probe_solve` 直接排提案 —— v6 §3.9.1 要求"
                    "**每条松弛提案必经探针实证验证**，没验过的提案根本不许呈现。"
                    if probes > 0
                    else "★ 禁令：池子已空还继续调 `probe_solve` —— 预算闸必须真的拦得住"
                    "（§12.5.1 预算熔断正确率 100%）。"
                )
                + (
                    "★ 降级路径：`harness=None` 时**一次工具调用都没有**，"
                    "但确定性四步（冲突集 → 归因 → 起草提案 → 探针实证验证）照常跑完 —— "
                    "诊断能力不依赖 LLM，Agent 加的只是探测顺序与深度的自主性。"
                    if probes < 0
                    else ""
                )
                + "★ 另一条恒定禁令：INFEASIBLE 却走到 `commit_plan`，那是把不可行"
                "当可行交付了（铁律 8）。",
                acceptable=tuple(acceptable),
                forbidden=tuple(forbidden),
                steps=tuple(steps),
            )
        )
    return rows


SCH_HEAD = ["route", "planner"]


def _planner_params(tool: str, week: str) -> dict[str, Any]:
    """planner 侧工具的合法参数：带周次的传周次，消解类传原话。"""
    if tool in {"resolve_person", "resolve_aircraft", "resolve_week"}:
        return params_for(tool)
    if tool == "translate_revision":
        return params_for(tool, iso_week=week)
    return params_for(tool, iso_week=week)


#: 13 条排班：范围与槽位形态不同，路径尾巴恒定。
#: `tools` 是 planner 期望调用的工具序列，`loop` 表示是否有 validate → solve 回环。
SCHEDULE_CASES: tuple[tuple[str, str, tuple[str, ...], str, bool, str], ...] = (
    (
        "下周给孙军排班",
        W03,
        ("resolve_person", "propose_solve_intent"),
        "单人",
        False,
        "单人范围：`estimate_scope` 可省（规则 B）",
    ),
    (
        "本周给学员们排班",
        W02,
        ("estimate_scope", "propose_solve_intent"),
        "群体",
        False,
        "群体展开靠快照的 identity 字段；范围不显然，`estimate_scope` **不可省**",
    ),
    (
        "下周把 missionC-1 排给张勇",
        W03,
        ("resolve_person", "propose_solve_intent"),
        "人+课目",
        False,
        "课目编号逐字出现，不需要消解工具",
    ),
    (
        "本周只用 AC10 和 AC27 给陈伟排班",
        W02,
        ("resolve_person", "resolve_aircraft", "resolve_aircraft", "propose_solve_intent"),
        "人+双机",
        False,
        "★ 两次 `resolve_aircraft` 的先后可换（规则 A）；只消解一架判错",
    ),
    (
        "给所有人排 2026-W04 的班",
        "2026W04",
        ("propose_solve_intent",),
        "全员+ISO周",
        False,
        "★ 周次以 ISO 形态逐字给出 → `resolve_week` 可省（规则 B）",
    ),
    (
        "刘斌本周的复训安排一下",
        W02,
        ("resolve_person", "propose_solve_intent"),
        "复训",
        False,
        "★ 复训飞哪一门由求解侧按**类别**判（Z-8），planner 不该替它选一门 C-1 或 C-2",
    ),
    (
        "下周给所有人排班，周三不要安排飞行",
        W03,
        ("resolve_week", "translate_revision", "propose_solve_intent"),
        "带修饰",
        False,
        "★ 句内自带约束修饰 → 首轮就要 `translate_revision`，它不只属于修订轮",
    ),
    (
        "本周排班，每天最多 3 个架次",
        W02,
        ("translate_revision", "propose_solve_intent"),
        "密度修饰",
        False,
        "REDUCE_DENSITY 修饰，同上",
    ),
    (
        "下周给何超和罗磊排班",
        W03,
        ("resolve_person", "resolve_person", "propose_solve_intent"),
        "双人",
        False,
        "两次消解，顺序可换（规则 A）",
    ),
    (
        "本周给全体教员排班",
        W02,
        ("estimate_scope", "propose_solve_intent"),
        "教员群体",
        False,
        "★ 教员不排课目（S-09），只占带飞岗 —— 这条的求解结果多半是空排班，"
        "但那是求解侧的事，轨迹只验路径",
    ),
    (
        "下周排班，AC84 别用",
        W03,
        ("resolve_aircraft", "translate_revision", "propose_solve_intent"),
        "否定修饰",
        False,
        "「别用 X」落 FORBID 而不是 PIN_RESOURCE",
    ),
    (
        "本周给所有人排班",
        W02,
        ("resolve_week", "propose_solve_intent"),
        "校验驳回",
        True,
        "★ **validate → solve 回环**：校验器发现一条违规，方案打回重解。"
        "§12.6.1 的「无效回环率 = 0」指的是**非规格 bug 的回环**；这一条是规格内的回环，"
        "属于正常路径。★ 但回环**两次以上**判错 —— 那说明求解与校验对同一条规格的理解不一致，"
        "该走 FTS-3003 而不是继续绕",
    ),
    (
        "下周给所有人排班",
        W03,
        ("resolve_week", "propose_solve_intent"),
        "门禁驳回",
        False,
        "★ 用户在 `human_gate` 选了 `REJECT` → 直接 END，**不落 `commit_plan`**。"
        "把 REJECT 也归档是这条链路上最贵的一种错",
    ),
)


def schedule_full() -> list[dict[str, Any]]:
    """13 条补足到 15（样例 2 条在前）。"""
    rows: list[dict[str, Any]] = []
    for i, (utterance, week, tools, shape, loop, why) in enumerate(SCHEDULE_CASES, start=3):
        reject = "门禁驳回" in shape
        tail = ["explain", "resume_guard", "human_gate", "END"] if reject else list(TAIL)
        middle = ["compile_spec", "solve", "validate"]
        if loop:
            middle = ["compile_spec", "solve", "validate", "solve", "validate"]
        path = [*SCH_HEAD, *(f"tool:{t}" for t in tools), *middle, *tail]

        acceptable: list[list[str]] = []
        if "estimate_scope" not in tools and len(tools) > 1:
            acceptable.append(
                [
                    *SCH_HEAD,
                    *(f"tool:{t}" for t in tools[:-1]),
                    "tool:estimate_scope",
                    f"tool:{tools[-1]}",
                    *middle,
                    *tail,
                ]
            )
        # 两个同名工具（如两次 resolve_person）交换等于没换 —— 那不是替代路径
        if len(tools) > 2 and tools[0] != tools[1]:
            swapped = [tools[1], tools[0], *tools[2:]]
            acceptable.append([*SCH_HEAD, *(f"tool:{t}" for t in swapped), *middle, *tail])
        if not acceptable:
            acceptable.append(
                [*SCH_HEAD, "tool:estimate_scope", *(f"tool:{t}" for t in tools), *middle, *tail]
            )

        forbidden = [
            [*SCH_HEAD, *(f"tool:{t}" for t in tools), "solve", "validate", *tail],
            [*SCH_HEAD, *(f"tool:{t}" for t in tools), "compile_spec", "solve", *tail],
        ]
        if loop:
            forbidden.append(
                [
                    *SCH_HEAD,
                    *(f"tool:{t}" for t in tools),
                    "compile_spec",
                    "solve",
                    "validate",
                    "solve",
                    "validate",
                    "solve",
                    "validate",
                    *tail,
                ]
            )
        if reject:
            forbidden.append(
                [
                    *SCH_HEAD,
                    *(f"tool:{t}" for t in tools),
                    *middle,
                    "explain",
                    "resume_guard",
                    "human_gate",
                    "commit_plan",
                    "END",
                ]
            )

        steps = tuple(
            _step(
                order,
                "planner",
                tool,
                _planner_params(tool, week),
            )
            for order, tool in enumerate(tools, start=1)
        )
        rows.append(
            _item(
                f"TRJ-SCH-{i:03d}",
                "schedule",
                utterance,
                "基准周快照已就绪，无既有计划。"
                + ("用户随后在人工门禁上选择 `REJECT`。" if reject else "")
                + ("首轮校验驳回一次。" if loop else ""),
                path,
                f"排班（{shape}）。{why}。★ 固定序列：`compile_spec → solve → validate → "
                f"explain → resume_guard → human_gate` 是静态边，不是模型选的；"
                f"跳过 `compile_spec`（语义开关不生效）或跳过 `validate`"
                f"（「100% 合规」失去证据）都判错。",
                acceptable=tuple(acceptable),
                forbidden=tuple(forbidden),
                steps=steps,
            )
        )
    return rows


#: 9 条重排：扰动种类不同，`assess_disruption` 恒不可省。
RESCHEDULE_CASES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "吴鹏 1 月 5 日请假，本周的计划重排一下",
        W02,
        ("resolve_person",),
        "单人单日不可用：判据只看人在不在（Z-9），单日与整周是两种粒度",
    ),
    (
        "本周五 AC73 定检，把受影响的架次调开",
        W02,
        ("resolve_aircraft",),
        "★ 「把受影响的架次调开」是目的陈述而不是第二条约束，不该额外造一次工具调用",
    ),
    ("RWY-2 下周三关闭，重新排一版", W03, (), "跑道扰动：跑道编号逐字出现，无需消解工具"),
    (
        "IFR Route 本周关闭，受影响的架次重排",
        W02,
        (),
        "空域扰动：容量为 1 的空域关掉即等于该路课目本周全停",
    ),
    ("下周训练窗改成 07:00 到 17:00，重排", W03, (), "训练窗收紧：两端同时压"),
    (
        "孙军下周休假，AC27 同期送修",
        W03,
        ("resolve_person", "resolve_aircraft"),
        "★ 人 + 机组合扰动，两次消解顺序可换（规则 A）",
    ),
    (
        "本周吴鹏和高超都请假，IFR 也关了",
        W02,
        ("resolve_person", "resolve_person"),
        "★ 两名教员同时缺勤 + 空域关闭；三名教员去掉两名，带飞容量逼近下限",
    ),
    (
        "下周 AC61、AC73 都送修，训练窗还压到 08:00-16:00",
        W03,
        ("resolve_aircraft", "resolve_aircraft"),
        "两机 + 训练窗：JL-8 机队从 6 架降到 4 架",
    ),
    (
        "本周刘斌出差，SAB 容量减半，重新排",
        W02,
        ("resolve_person",),
        "★ 刘斌缺勤会顶掉 S-11 复训；SAB 绑 A-2/F-1/G-1，容量减半直接压 A 类每周必飞",
    ),
)


def reschedule_full() -> list[dict[str, Any]]:
    """9 条补足到 10（样例 1 条在前）。"""
    rows: list[dict[str, Any]] = []
    for i, (utterance, week, resolvers, why) in enumerate(RESCHEDULE_CASES, start=2):
        tools = (*resolvers, "assess_disruption", "propose_solve_intent")
        path = [
            *SCH_HEAD,
            *(f"tool:{t}" for t in tools),
            "compile_spec",
            "solve",
            "validate",
            *TAIL,
        ]
        acceptable: list[list[str]] = []
        if len(resolvers) == 2 and resolvers[0] != resolvers[1]:
            swapped = (resolvers[1], resolvers[0], "assess_disruption", "propose_solve_intent")
            acceptable.append(
                [
                    *SCH_HEAD,
                    *(f"tool:{t}" for t in swapped),
                    "compile_spec",
                    "solve",
                    "validate",
                    *TAIL,
                ]
            )
        acceptable.append(
            [
                *SCH_HEAD,
                *(f"tool:{t}" for t in tools),
                "tool:estimate_scope",
                "compile_spec",
                "solve",
                "validate",
                *TAIL,
            ]
        )
        without_assess = tuple(t for t in tools if t != "assess_disruption")
        forbidden = (
            [
                *SCH_HEAD,
                *(f"tool:{t}" for t in without_assess),
                "compile_spec",
                "solve",
                "validate",
                *TAIL,
            ],
            [*SCH_HEAD, *(f"tool:{t}" for t in tools), "compile_spec", "solve", *TAIL],
        )
        steps = tuple(
            _step(
                order,
                "planner",
                tool,
                _planner_params(tool, week),
            )
            for order, tool in enumerate(tools, start=1)
        )
        rows.append(
            _item(
                f"TRJ-RSC-{i:03d}",
                "reschedule",
                utterance,
                f"{'本周' if week == W02 else '下周'}已有一版**已批准**的计划。",
                path,
                f"重排：{why}。★ `assess_disruption` **不可省** —— 它是影响面探测与"
                f"自我降档的输入（§7.3.3 ①），省掉等于拿一个未经评估的冻结档去重排，"
                f"扰动可能远超用户预期。所以它不在规则 B 的范围里，"
                f"缺它的那条路径进了 `forbidden_paths`。",
                acceptable=tuple(acceptable),
                forbidden=forbidden,
                steps=steps,
            )
        )
    return rows


#: 8 条修订：六种 `RevisionKind` 各一 + 授权不足 + 门禁 REJECT。
#: **全部都是两次门禁往返**（`Z-19`），一条都不例外。
REVISION_CASES: tuple[tuple[str, str, str, str], ...] = (
    ("本周何超别再排 missionC-2 了", "FORBID", "normal", "针对 (人, 课目) 的禁令"),
    ("把陈伟的第一个架次钉在周一 08:00", "PIN_TIME", "normal", "时间钉住到具体时刻"),
    ("下周罗磊的架次都用 AC61", "PIN_RESOURCE", "normal", "资源钉住，同时出现人与机两个 target"),
    ("本周张勇一天最多飞一次", "REDUCE_DENSITY", "normal", "个人层面的密度限制"),
    (
        "下周张勇的架次都排 RWY-2",
        "PIN_RUNWAY",
        "normal",
        "★ RWY-2 只服务 JL-8，与学员机型相容；`PIN_RUNWAY` 是 v6 新增的第六种",
    ),
    (
        "把刘斌的复训挪到周末",
        "SHIFT_WINDOW",
        "normal",
        "★ 复训的时间窗平移；S-11 的复训窗口本身还要满足 7 天滑窗",
    ),
    (
        "这次直接放宽约束11 的周上限",
        "FORBID",
        "authority",
        "★ **授权不足**：放宽 R1 档需要 director 及以上，scheduler 提这个要求时"
        "`check_authority` 会把它挡下来并写进 `open_questions` —— "
        "路径在回显门禁那一步转向追问，**不进 solve**",
    ),
    (
        "算了，这版不要了",
        "FORBID",
        "reject",
        "★ 用户在回显门禁上选 `REJECT`：**不重解、不归档**，直接 END。"
        "把 REJECT 当成 REVISE 继续重解，是这条链路上最容易犯的错",
    ),
)


def revision_full() -> list[dict[str, Any]]:
    """8 条补足到 10（样例 2 条在前）。"""
    rows: list[dict[str, Any]] = []
    for i, (utterance, kind, mode, why) in enumerate(REVISION_CASES, start=3):
        if mode == "authority":
            path = [
                "human_gate",
                "planner",
                "tool:translate_revision",
                "tool:check_authority",
                "tool:ask_user",
                "human_gate",
                "END",
            ]
            acceptable = (
                [
                    "human_gate",
                    "planner",
                    "tool:check_authority",
                    "tool:translate_revision",
                    "tool:ask_user",
                    "human_gate",
                    "END",
                ],
            )
            forbidden = (
                [
                    "human_gate",
                    "planner",
                    "tool:translate_revision",
                    "human_gate",
                    "solve",
                    "validate",
                    *TAIL,
                ],
            )
            steps = (
                _step(
                    1,
                    "planner",
                    "translate_revision",
                    params_for("translate_revision", utterance=utterance),
                ),
                _step(
                    2, "planner", "check_authority", params_for("check_authority", requested_tier=1)
                ),
                _step(
                    3,
                    "planner",
                    "ask_user",
                    params_for(
                        "ask_user",
                        question="放宽 R1 档需要训练主任授权，是否请主任确认？",
                        options=["请主任确认", "改用 R0 档"],
                    ),
                ),
            )
        elif mode == "reject":
            path = ["human_gate", "END"]
            acceptable = (["human_gate", "planner", "human_gate", "END"],)
            forbidden = (
                [
                    "human_gate",
                    "planner",
                    "tool:translate_revision",
                    "human_gate",
                    "solve",
                    "validate",
                    *TAIL,
                ],
                ["human_gate", "commit_plan", "END"],
            )
            steps = ()
        else:
            path = [
                "human_gate",
                "planner",
                "tool:translate_revision",
                "human_gate",
                "solve",
                "validate",
                *TAIL,
            ]
            acceptable = (
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
            )
            forbidden = (
                ["human_gate", "planner", "tool:translate_revision", "solve", "validate", *TAIL],
            )
            steps = (
                _step(
                    1,
                    "planner",
                    "translate_revision",
                    params_for("translate_revision", utterance=utterance),
                ),
                _step(
                    2,
                    "planner",
                    "check_authority",
                    params_for("check_authority", requested_tier=0),
                    optional=True,
                ),
            )
        rows.append(
            _item(
                f"TRJ-REV-{i:03d}",
                "revision",
                utterance,
                "已经排出一版方案，用户在人工门禁上选了 `REVISE` 并说了这句话。",
                path,
                f"修订翻译 → `{kind}`。{why}。★ **`Z-19` 的两次门禁往返**：翻译完先回门禁"
                f"展示「我理解为…」，`APPROVE` 之后才 `solve` 重解 —— 那一屏的 `APPROVE` "
                f"是「去重解」不是「去归档」。翻译完直接 `solve` 是 v6 反模式清单点名的"
                f"「先重解再展示」，顺序反了等于「翻译错了也已经排了一版」。",
                acceptable=acceptable,
                forbidden=forbidden,
                steps=steps,
            )
        )
    return rows


#: 9 条摄取。`stage` 决定路径在哪一段终止：
#: `full` 走完三段；`blocked` 停在 prepare（抽取失败绝不静默降级，铁律 7）；
#: `question` 停在 prepare 之后的提问（缺输入即提问，FTS-1004）；
#: `rejected` 走到门禁但用户拒绝，**不落库**。
INGEST_CASES: tuple[tuple[str, str, tuple[str, ...], str, str], ...] = (
    (
        "只上传一份人员表",
        "full",
        ("classify_doc", "parse_personnel", "diff_snapshot"),
        "单文件上传：分类 → 抽取 → Diff → 人工确认 → 落库",
    ),
    (
        "上传新的飞机表",
        "full",
        ("classify_doc", "parse_aircraft", "diff_snapshot"),
        "同上，换一类文档",
    ),
    (
        "上传新的课目表",
        "full",
        ("classify_doc", "parse_missions", "diff_snapshot"),
        "★ 课目表带「课程开始日期」列 —— `cycle_start` 的第一来源（S-14）",
    ),
    (
        "上传新的规则文件",
        "full",
        ("classify_doc", "parse_rules", "propose_rule_dsl", "diff_snapshot"),
        "★ 规则文件多一步 `propose_rule_dsl`：条文 → DSL 草案。**草案不自动生效**，"
        "要经人工确认门禁（规则是 R0，改它必须有人签字）",
    ),
    (
        "上传人员表和飞机表两份",
        "full",
        ("classify_doc", "classify_doc", "parse_personnel", "parse_aircraft", "diff_snapshot"),
        "★ 两次分类的先后无所谓（规则 A）",
    ),
    (
        "上传一份课目表，但没有课程开始日期列",
        "question",
        ("classify_doc", "parse_missions"),
        "★ **缺输入即提问**（S-14 / §5.1.1）：`cycle_start` 三条来源全空 → FTS-1004 阻断并追问。"
        "**没有默认值，配置项里也没有** —— 静默给一个默认日期是本项目明令的反模式",
    ),
    (
        "上传一份内容损坏的人员表",
        "blocked",
        ("classify_doc",),
        "★ **抽取失败绝不静默降级**（铁律 7）：宁可抛 `IngestionError` 阻断，"
        "也不让 `sionB-1` 这类脏 token 进库。路径**停在 prepare**，不进门禁、不落库",
    ),
    (
        "上传的人员表里刘斌的到期日与总表冲突",
        "blocked",
        ("classify_doc", "parse_personnel"),
        "★ 源内冲突检出（§5.5 的 X1）：BLOCKING 冲突要走人工裁决，"
        "不能自己挑一个继续 —— 这正是 `SPEC_DECISIONS §C.1` 那条 01-07 / 02-07 的来历",
    ),
    (
        "上传四份 PDF，但确认时发现 Diff 不对",
        "rejected",
        (
            "classify_doc",
            "parse_personnel",
            "parse_aircraft",
            "parse_missions",
            "parse_rules",
            "diff_snapshot",
        ),
        "★ 门禁 `REJECT`：走到 `ingest.gate` 但用户拒绝 → **不落库**。"
        "`commit` 拿不到 `GateDecision` 就跑不起来，这个切分就是为了让「先落库再说」写不出来",
    ),
)


def ingest_full() -> list[dict[str, Any]]:
    """9 条补足到 10（样例 1 条在前）。"""
    rows: list[dict[str, Any]] = []
    for i, (utterance, stage, tools, why) in enumerate(INGEST_CASES, start=2):
        tail = {
            "full": ["ingest.gate", "ingest.commit"],
            "rejected": ["ingest.gate"],
            "blocked": [],
            "question": [],
        }[stage]
        path = ["ingest.prepare", *(f"tool:{t}" for t in tools), *tail]

        acceptable: list[list[str]] = []
        classify_count = sum(1 for t in tools if t == "classify_doc")
        parsers = [t for t in tools if t.startswith("parse_")]
        if len(parsers) > 1:
            reordered = [
                *[t for t in tools if not t.startswith("parse_")][:classify_count],
                *reversed(parsers),
                *[t for t in tools if t == "diff_snapshot"],
            ]
            acceptable.append(["ingest.prepare", *(f"tool:{t}" for t in reordered), *tail])
        if stage == "full":
            acceptable.append(
                ["ingest.prepare", *(f"tool:{t}" for t in tools), "tool:propose_change", *tail]
            )
        if not acceptable:
            acceptable.append(
                [
                    "ingest.prepare",
                    "tool:classify_doc",
                    *(f"tool:{t}" for t in tools if t != "classify_doc"),
                    *tail,
                ]
                if tools[0] != "classify_doc"
                else ["ingest.prepare", *(f"tool:{t}" for t in tools), "tool:propose_change", *tail]
            )

        forbidden: list[list[str]] = [
            ["ingest.prepare", *(f"tool:{t}" for t in tools), "ingest.commit"],
        ]
        if stage in ("blocked", "question"):
            forbidden.append(
                ["ingest.prepare", *(f"tool:{t}" for t in tools), "ingest.gate", "ingest.commit"]
            )
        if stage == "rejected":
            forbidden.append(
                ["ingest.prepare", *(f"tool:{t}" for t in tools), "ingest.gate", "ingest.commit"]
            )

        steps = tuple(
            _step(
                order,
                "extract",
                tool,
                params_for(tool)
                if tool in {"classify_doc", "diff_snapshot", "propose_rule_dsl"}
                else params_for(tool, document_id="doc_uploaded"),
            )
            for order, tool in enumerate(tools, start=1)
        )
        rows.append(
            _item(
                f"TRJ-ING-{i:03d}",
                "ingest",
                utterance,
                f"走 `POST /api/v1/ingest`（**不在对话图内**）。终局：{stage}。",
                path,
                f"摄取：{why}。★ 两段式的分界是**人工确认**：`prepare` 只读、`commit` 落库。"
                f"跳过 `ingest.gate` 直接 `commit` 在每一条上都是禁令 —— v6 §5.1 的人工确认"
                f"是硬性门禁，代码里把 `GateDecision` 做成必传参数就是为了让它绕不过去。",
                acceptable=tuple(acceptable),
                forbidden=tuple(forbidden),
                steps=steps,
            )
        )
    return rows


def build_full() -> list[dict[str, Any]]:
    """全量 100 条。"""
    return [
        *knowledge_samples(),
        *knowledge_full(),
        *diagnosis_samples(),
        *diagnosis_full(),
        *workflow_samples()[:2],
        *schedule_full(),
        *workflow_samples()[2:3],
        *reschedule_full(),
        *workflow_samples()[3:5],
        *revision_full(),
        *workflow_samples()[5:],
        *ingest_full(),
    ]
