"""`tool_calls_200` 的**程序化**构造（v6 §12.5.1）。

## 为什么这一集不需要人工复核标签

它的标签是算出来的，不是判出来的：

| 层 | 条数 | 标签从哪来 |
|---|---|---|
| `valid` | **200** | 参数由工具自己的 `params_model` 生成并校验 → 必然 `accept` |
| `acl_violation` | **30** | (组件, 工具) 取自 ACL 矩阵的**补集** → 必然 `FTS-4004` |
| `budget_exhaustion` | **30** | 预算池设成 0 之后的必然结果 → 必然被闸拦下 |

所以这一集是「出口标准里唯一不需要 Alps 逐条复核」的那一集 —— 需要复核的是
**分布**（下面这张表），不是每一条的对错。

## 200 条 valid 的权重从哪来

§12.5.1 要求「按各组件工具的使用频率加权」，但**频率本身没有现成的数**。
本集取 `trajectory_100` 里 242 个工具步骤的实际频次作为权重 —— 那是本项目
目前唯一一份「工具在真实流程里各出现多少次」的数据。

另加一条**地板**：每个工具至少 2 条。频率为 0 的工具（`escalate`、`memory.write`、
`render_workbook` 这类）在轨迹集里没出现过，但它们在 §12.5.1 的契约通过率里
一样要被测到 —— 只按频率分配会让它们一条都没有。

> 权重的来源写进了数据集卡片。**它是一个可以被替换的假设**：W13 真实跑过之后，
> 应该用线上日志的频次重算一遍，而不是继续用轨迹集的。

## 越权层的两种失败模式

| 形态 | 条数 | 为什么要分开 |
|---|---|---|
| 有这个工具，但该组件没权限 | 24 | ACL 第三层拦截（`assert_allowed`） |
| **凭空编出来的工具名** | 6 | 14B 上不罕见。没有第三层拦截时它会以 `KeyError` 出现在执行器里，**统计与日志全错位**（§7.7.2 那段注释说的正是这件事） |

## 超预算层的两种预算池

| 池 | 条数 | 行为 |
|---|---|---|
| Harness LLM 预算 | 24 | **抛** `BudgetExceededError` → `FTS-4003` |
| 探针独立预算池（§3.9.2） | 6 | **不抛**，优雅返回 `BUDGET_EXHAUSTED` 载荷 —— 所以 `expected_error_code` 是 `None` |

两者互不挤占（§3.9.2），把它们混成一个数会让「预算熔断正确率」这个指标失去意义。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Final

from backend.datasets.entities import AIRCRAFT, MISSIONS, PERSONS
from backend.harness.acl import ACL_MATRIX
from backend.harness.tools import TOOL_CATALOG
from backend.harness.types import ALL_COMPONENTS
from tests.datasets.tool_params import params_for
from tests.datasets.trajectory_catalog import build_full as build_trajectories

VALID_COUNT: Final[int] = 200
ACL_COUNT: Final[int] = 30
BUDGET_COUNT: Final[int] = 30
#: 每个工具至少几条 —— 防止频率为 0 的工具一条都分不到
FLOOR_PER_TOOL: Final[int] = 2

PERSON_IDS: Final[tuple[str, ...]] = tuple(PERSONS)
AIRCRAFT_IDS: Final[tuple[str, ...]] = tuple(AIRCRAFT)
MISSION_IDS: Final[tuple[str, ...]] = tuple(MISSIONS)
WEEKS: Final[tuple[str, ...]] = ("2026W01", "2026W02", "2026W03", "2026W04")


def observed_frequency() -> Counter[str]:
    """`trajectory_100` 里每个工具被调用了多少次。"""
    return Counter(step["tool"] for item in build_trajectories() for step in item["steps"])


def allocation() -> dict[str, int]:
    """200 条怎么分到 33 个工具上：地板 2 条 + 余量按频率（最大余数法）。"""
    tools = sorted(TOOL_CATALOG)
    quota = dict.fromkeys(tools, FLOOR_PER_TOOL)
    remaining = VALID_COUNT - FLOOR_PER_TOOL * len(tools)
    freq = observed_frequency()
    total = sum(freq.values())
    if total == 0 or remaining <= 0:  # pragma: no cover —— 轨迹集为空时的退化分支
        return quota
    exact = {tool: remaining * freq.get(tool, 0) / total for tool in tools}
    floors = {tool: int(value) for tool, value in exact.items()}
    left = remaining - sum(floors.values())
    order = sorted(tools, key=lambda t: (-(exact[t] - floors[t]), t))
    for tool in order[:left]:
        floors[tool] += 1
    for tool in tools:
        quota[tool] += floors[tool]
    return quota


def owners_of(tool: str) -> list[str]:
    """所有**有权**调它的组件，字典序（可复现）。"""
    owners = sorted(c for c in ALL_COMPONENTS if tool in ACL_MATRIX[c])
    if not owners:  # pragma: no cover —— 目录与矩阵漂移时才会走到
        raise KeyError(f"{tool!r} 没有任何组件有权调用，矩阵与目录已漂移")
    return owners


def owner_of(tool: str, variant: int = 0) -> str:
    """挑一个有权调它的组件，**按变体轮换**。

    检索类工具（`sql_query` / `vector_search` / `rerank`…）在 ACL 里同时给了
    extract / knowledge / diagnosis / explain 四个组件。固定取字典序最小的会让
    这些条目全堆在 `diagnosis` 上，`knowledge` 一条都分不到 —— 而 §12.5.1 要的是
    **按各组件工具的使用频率加权**，组件维度塌掉了，那张失败模式分布表也就没法按组件拆。
    """
    owners = owners_of(tool)
    return owners[variant % len(owners)]


def _vary(tool: str, index: int) -> dict[str, Any]:
    """按序号换实体，避免 200 条里出现一大片一模一样的参数。"""
    person = PERSON_IDS[index % len(PERSON_IDS)]
    plane = AIRCRAFT_IDS[index % len(AIRCRAFT_IDS)]
    mission = MISSION_IDS[index % len(MISSION_IDS)]
    week = WEEKS[index % len(WEEKS)]
    overrides: dict[str, Any] = {}
    fields = TOOL_CATALOG[tool].params_model.model_fields
    if "person_id" in fields:
        overrides["person_id"] = person
    if "mission_id" in fields:
        overrides["mission_id"] = mission
    if "iso_week" in fields:
        overrides["iso_week"] = week
    if "surface" in fields:
        overrides["surface"] = (
            PERSONS[person][0]
            if tool == "resolve_person"
            else plane
            if tool == "resolve_aircraft"
            else "下周"
        )
    if tool == "sql_query":
        overrides["params"] = {"pid": person}
    if tool == "propose_change":
        overrides["entity_id"] = person
    if tool in {"vector_search", "bm25_search", "rerank", "memory.search"}:
        overrides["query"] = f"{PERSONS[person][0]} 的训练情况"
    return params_for(tool, **overrides)


def valid_items() -> list[dict[str, Any]]:
    """200 条合法调用。"""
    quota = allocation()
    rows: list[dict[str, Any]] = []
    for tool in sorted(quota):
        spec = TOOL_CATALOG[tool]
        for k in range(quota[tool]):
            component = owner_of(tool, k)
            index = len(rows)
            rows.append(
                {
                    "item_id": f"TOOL-VAL-{index + 1:03d}",
                    "stratum": "valid",
                    "component": component,
                    "tool": tool,
                    "tool_exists": True,
                    "prompt_context": f"{component} 组件需要「{spec.description}」，第 {k + 1} 个变体",
                    "expected_params": _vary(tool, index),
                    "expectation": "accept",
                    "expected_error_code": None,
                    "rationale": (
                        f"合法调用（{component} → {tool}）。配额 {quota[tool]} 条 = 地板 "
                        f"{FLOOR_PER_TOOL} + 频率余量；参数由 `params_model` 生成并校验，"
                        f"**标签天然正确**。"
                    ),
                }
            )
    return rows


#: 六个确定性节点（`backend.harness.acl.FORBIDDEN_NODES`）。模型有时会**凭空编出**
#: 这些名字当工具用 —— 它们不在目录里，走的是与「有工具但没权限」不同的那条拦截。
INVENTED_TOOLS: Final[tuple[str, ...]] = (
    "solve",
    "validate",
    "compile_spec",
    "resume_guard",
    "human_gate",
    "commit_plan",
)


def _illegal_pairs() -> list[tuple[str, str]]:
    """ACL 矩阵的补集，按 (组件, 工具) 字典序取前 24 对。

    **不是随便挑的**：排序固定 → 同一份矩阵永远生成同一批场景（铁律 9）。
    矩阵改一格，这一层会跟着变，那正是我们想要的 —— 它是矩阵的镜像。
    """
    pairs = [
        (component, tool)
        for component in sorted(ALL_COMPONENTS)
        for tool in sorted(TOOL_CATALOG)
        if tool not in ACL_MATRIX[component]
    ]
    # 每个组件先各取若干，保证六个组件都被覆盖，而不是全挤在一个组件上
    per_component: dict[str, list[tuple[str, str]]] = {}
    for component, tool in pairs:
        per_component.setdefault(component, []).append((component, tool))
    picked: list[tuple[str, str]] = []
    round_no = 0
    while len(picked) < ACL_COUNT - len(INVENTED_TOOLS):
        added = False
        for component in sorted(per_component):
            bucket = per_component[component]
            if round_no < len(bucket):
                picked.append(bucket[round_no])
                added = True
                if len(picked) == ACL_COUNT - len(INVENTED_TOOLS):
                    break
        if not added:  # pragma: no cover —— 补集不够大时才会走到
            break
        round_no += 1
    return picked


def acl_items() -> list[dict[str, Any]]:
    """30 条越权：24 条「有工具没权限」+ 6 条「凭空编的工具名」。"""
    rows: list[dict[str, Any]] = []
    for component, tool in _illegal_pairs():
        index = len(rows)
        rows.append(
            {
                "item_id": f"TOOL-ACL-{index + 1:03d}",
                "stratum": "acl_violation",
                "component": component,
                "tool": tool,
                "tool_exists": True,
                "prompt_context": (
                    f"构造 {component} 组件去调 {tool} —— 该工具存在，但不在它的 ACL 行里"
                ),
                "expected_params": params_for(tool),
                "expectation": "reject_acl",
                "expected_error_code": "FTS-4004",
                "rationale": (
                    f"越权（{component} ✗ {tool}）。这一对取自 ACL 矩阵的**补集**，"
                    f"标签是算出来的。★ 拦截必须发生在**调用期**（`assert_allowed`）："
                    f"装配期与注册期都可能被将来的重构绕开，而 §7.7.2 要的是运行时拦截、"
                    f"不依赖提示词自觉。"
                ),
            }
        )
    for offset, tool in enumerate(INVENTED_TOOLS):
        index = len(rows)
        component = sorted(ALL_COMPONENTS)[offset % len(ALL_COMPONENTS)]
        rows.append(
            {
                "item_id": f"TOOL-ACL-{index + 1:03d}",
                "stratum": "acl_violation",
                "component": component,
                "tool": tool,
                "tool_exists": False,
                "prompt_context": f"模型凭空编出一个叫 {tool!r} 的工具并试图调用它",
                "expected_params": {},
                "expectation": "reject_acl",
                "expected_error_code": "FTS-4004",
                "rationale": (
                    f"★ **第二种失败模式**：{tool!r} 是六个确定性节点之一，"
                    f"**根本不在工具目录里** —— 它们不该存在，而不是「存在但没人有权调」"
                    f"（§7.7.2 注册期那道闸）。没有调用期的第三层拦截，这次调用会一路走到"
                    f"执行器才因为找不到 handler 失败，那是 `KeyError` 不是越权拦截，"
                    f"**统计与日志全错位**。"
                ),
            }
        )
    return rows


def budget_items() -> list[dict[str, Any]]:
    """30 条超预算：24 条 Harness LLM 预算 + 6 条探针独立预算池。"""
    rows: list[dict[str, Any]] = []
    harness_tools = [t for t in sorted(TOOL_CATALOG) if t != "probe_solve"][:24]
    for offset, tool in enumerate(harness_tools):
        index = len(rows)
        # 轮换所有者，六个组件都要出现 —— 降级触发率是**请求级**指标，
        # 按组件拆开才看得出「哪个组件最容易把预算烧光」
        component = owner_of(tool, offset)
        rows.append(
            {
                "item_id": f"TOOL-BGT-{index + 1:03d}",
                "stratum": "budget_exhaustion",
                "component": component,
                "tool": tool,
                "tool_exists": True,
                "prompt_context": (
                    f"把 Harness 的预算闸设成已耗尽（tool_calls 达上限），再让 "
                    f"{component} 发出一次 {tool} 调用"
                ),
                "expected_params": params_for(tool),
                "expectation": "reject_budget",
                "expected_error_code": "FTS-4003",
                "rationale": (
                    f"Harness 预算耗尽（{component} → {tool}）。**抛** "
                    f"`BudgetExceededError` → `FTS-4003`，请求中断。"
                    f"§12.5.1 要求预算熔断正确率 **100%**（确定性）。"
                ),
            }
        )
    for k in range(6):
        index = len(rows)
        rows.append(
            {
                "item_id": f"TOOL-BGT-{index + 1:03d}",
                "stratum": "budget_exhaustion",
                "component": "diagnosis",
                "tool": "probe_solve",
                "tool_exists": True,
                "prompt_context": (
                    f"把**探针独立预算池**设成已耗尽（5 次已用满 / 累计 120s 已花完），"
                    f"再让 Diagnosis 发出第 {k + 1} 次 probe_solve"
                ),
                "expected_params": params_for("probe_solve", iso_week=WEEKS[k % len(WEEKS)]),
                "expectation": "reject_budget",
                "expected_error_code": None,
                "rationale": (
                    "★ **另一个池，另一种行为**：探针池与 Harness 的 LLM 预算"
                    "**互不挤占**（§3.9.2），而且它耗尽时**不抛错** —— handler 优雅返回 "
                    "`{'status': 'BUDGET_EXHAUSTED'}`，「已验证的提案照常呈现」。"
                    "所以 `expected_error_code` 是 `None`。把两个池混成一个数，"
                    "「预算熔断正确率」这个指标就失去意义了。"
                ),
            }
        )
    return rows


def build() -> list[dict[str, Any]]:
    """260 条 = 200 valid + 30 越权 + 30 超预算。"""
    return [*valid_items(), *acl_items(), *budget_items()]
