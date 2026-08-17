"""E2E：从提交到归档的全流程（Playwright × 真前端 × 真 RQ worker）。

25 条断言分布在四个页签 + 人工门禁上，**共用同一次基准周排班**
（见 `conftest.py` 的模块注释）。它们验的是「显示出来了没有」——
「算得对不对」由 `tests/unit/test_frontend_components.py` 在无浏览器的情况下验。

## 这一批与集成测试的分工

| | 集成测试 | 本文件 |
|---|---|---|
| 执行方式 | inline（同进程） | **真 RQ worker（跨进程）** |
| 验什么 | HTTP 契约、锁、幂等、回放数据 | **界面上看得见的东西** |
| HITL 恢复 | 同进程 resume | **杀掉 worker 再起一个**，隔着进程恢复 |
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import RUN_TIMEOUT_MS, restart_worker
from tests.fixtures.api_fixtures import restored_db

pytestmark = pytest.mark.e2e


def _tab(page: Any, name: str) -> None:
    page.get_by_role("tab", name=name).click()
    page.wait_for_timeout(500)


def _body(page: Any) -> str:
    return str(page.inner_text("body"))


def _code_string_literals(path: Path) -> list[str]:
    """模块里**代码位置**的字符串字面量（不含 docstring）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


# ─────────────────────────────────────────────────────────────────────
# 顶栏与骨架（v6 §8.3）
# ─────────────────────────────────────────────────────────────────────
def test_topbar_shows_snapshot_and_versions(page: Any) -> None:
    """顶栏：快照 / 规则版本 / **语义版本** / 跑道模型 / 离线运行标识。"""
    text = _body(page)
    assert "FTS 智能排班" in text
    assert re.search(r"快照\s*snap_[0-9a-f]+", text), text[:400]
    assert "规则 1.3.0" in text
    assert "语义 1.1.0" in text, "语义版本（sem_1.1）必须显示"


def test_topbar_shows_runway_model_and_offline_badge(page: Any) -> None:
    text = _body(page)
    assert "跑道模型 dual_runway" in text
    assert "● 离线运行" in text


def test_four_tabs_exist(page: Any) -> None:
    """v6 §8.3 的四页签，一个不少。"""
    for name in ("排班结果", "约束校验", "运作过程", "解释报告"):
        assert page.get_by_role("tab", name=name).is_visible()


def test_sidebar_has_session_upload_and_form_fallback(page: Any) -> None:
    text = _body(page)
    assert "会话与上传" in text
    assert "数据摄取 / 确认" in text
    assert "表单排班（LLM 不可用时的降级路径）" in text


# ─────────────────────────────────────────────────────────────────────
# 页签一 · 排班结果
# ─────────────────────────────────────────────────────────────────────
def test_week_summary_matches_the_baseline(page: Any) -> None:
    """基准周：14 架次 = 9 带飞 + 5 单飞，7 条阻塞项（CLAUDE.md §4）。"""
    _tab(page, "排班结果")
    text = _body(page)
    for label, value in (("架次", "14"), ("带飞", "9"), ("单飞", "5"), ("阻塞项", "7")):
        assert re.search(rf"{label}\s*\n?\s*{value}", text), f"{label} 没显示成 {value}"


def test_blocked_items_show_a_yellow_warning_banner(page: Any) -> None:
    """BLOCKED 是**黄色提示**不是红色错误 —— 它是规则生效的结果（约束13）。"""
    _tab(page, "排班结果")
    banner = page.get_by_text(re.compile(r"⚠ 7 项因先修未满足未安排"))
    assert banner.first.is_visible()
    warning = page.locator('[data-testid="stAlertContainer"]').filter(has_text="因先修未满足")
    assert warning.count() >= 1


def test_three_sheets_are_previewed(page: Any) -> None:
    _tab(page, "排班结果")
    for name in ("分日飞行计划", "飞行员训练", "飞机排班"):
        assert page.get_by_role("tab", name=name).is_visible()


def test_sheet1_crew_uses_the_role_suffix_format(page: Any) -> None:
    """机组列按 §10.1：姓名 + 角色后缀，全角逗号分隔。"""
    _tab(page, "排班结果")
    page.get_by_role("tab", name="分日飞行计划").click()
    page.wait_for_timeout(400)
    text = _body(page)
    assert re.search(r"[一-龥]{2,3}[教学单训]", text), "机组列没有角色后缀"


def test_sheet4_shows_all_seven_blocks(page: Any) -> None:
    """Sheet 4 **七个区块**（v6 §10.4，v6 相对 v5.2 新增了区块 7）。"""
    _tab(page, "排班结果")
    page.get_by_role("tab", name="合规与解释（Sheet 4）").click()
    page.wait_for_timeout(800)
    text = _body(page)
    for title in (
        "区块 1 · 计划元信息",
        "区块 2 · 约束校验结果",
        "区块 3 · 训练进度与欠账",
        "区块 4 · 阻塞项",
        "区块 5 · 资源利用",
        "区块 6 · 松弛与决策记录",
        "区块 7 · 跑道与空域占用明细",
    ):
        assert title in text, f"缺 {title}"


def test_sheet4_block1_carries_semantics_switches_and_runway_model(page: Any) -> None:
    _tab(page, "排班结果")
    page.get_by_role("tab", name="合规与解释（Sheet 4）").click()
    page.wait_for_timeout(800)
    text = _body(page)
    assert "语义开关" in text and "S-01=" in text, "Z-7：语义开关必须落在区块 1"
    assert "跑道模型" in text and "dual_runway" in text


def test_gantt_chart_is_rendered(page: Any) -> None:
    _tab(page, "排班结果")
    assert "周甘特图" in _body(page)
    chart = page.locator('[data-testid="stVegaLiteChart"], canvas, svg')
    assert chart.count() > 0, "甘特图没画出来"


# ─────────────────────────────────────────────────────────────────────
# 页签二 · 约束校验
# ─────────────────────────────────────────────────────────────────────
def test_validation_lists_fourteen_rules(page: Any) -> None:
    _tab(page, "约束校验")
    text = _body(page)
    assert "规则通过" in text and "14/14" in text
    for rule_id in (f"C{i:02d}" for i in range(1, 15)):
        assert rule_id in text, f"缺 {rule_id}"


def test_each_rule_shows_a_real_checked_count(page: Any) -> None:
    """「已检查 N 项」必须是真实数字，而且各不相同（0 项 = 假通过）。"""
    _tab(page, "约束校验")
    counts = [int(n) for n in re.findall(r"已检查 (\d+) 项", _body(page))]
    assert len(counts) == 14, f"只找到 {len(counts)} 条「已检查 N 项」"
    assert all(n > 0 for n in counts), f"有规则检查了 0 项：{counts}"
    assert len(set(counts)) >= 10, f"检查项数几乎全一样，疑似写死：{counts}"


def test_c06_is_displayed_with_the_v6_name(page: Any) -> None:
    """约束6 的显示名是「资源有效性与容量」（v6 更名）。"""
    _tab(page, "约束校验")
    assert "资源有效性与容量" in _body(page)


def test_c09_states_both_density_scopes(page: Any) -> None:
    """D-2：20 分钟窗口按跑道；**7 分钟间隔全场**。最容易实现错的一条。"""
    _tab(page, "约束校验")
    page.get_by_text(re.compile(r"C09 起降密度限制")).first.click()
    page.wait_for_timeout(400)
    text = _body(page)
    assert "20 分钟窗口按跑道" in text and "7 分钟间隔全场" in text


def test_rule_panel_shows_source_text_and_chroma_reference(page: Any) -> None:
    """判定依据 = 规则原文 + Chroma 溯源（v6 §8.2 约束校验面板那一行）。"""
    _tab(page, "约束校验")
    page.get_by_text(re.compile(r"C06 资源有效性与容量")).first.click()
    page.wait_for_timeout(400)
    text = _body(page)
    assert "规则原文" in text
    assert re.search(r"rule:1\.3\.0:\d{2}", text), "没有 Chroma 溯源标识"


def test_format_gates_show_three_layers(page: Any) -> None:
    _tab(page, "约束校验")
    text = _body(page)
    assert "Schema 层" in text and "业务完整性层" in text and "产物回读层" in text


# ─────────────────────────────────────────────────────────────────────
# 页签三 · 运作过程
# ─────────────────────────────────────────────────────────────────────
def test_replay_completeness_is_shown_as_one_hundred_percent(page: Any) -> None:
    """**回放完整性 = 100%**（M6 出口标准），而且要显示在界面上。"""
    _tab(page, "运作过程")
    text = _body(page)
    assert "回放完整性" in text
    assert "100%" in text, "回放不完整（seq 不连续）"


def test_step_slider_covers_every_event(page: Any) -> None:
    """步进 slider 必须覆盖全部 seq —— 缺一步就不是 100% 回放。"""
    _tab(page, "运作过程")
    text = _body(page)
    total = int(re.search(r"事件数\s*\n?\s*(\d+)", text).group(1))  # type: ignore[union-attr]
    assert total >= 6
    shown = re.search(r"显示第 0 ~ (\d+) 步，共 (\d+) 步", text)
    assert shown is not None, "找不到步进说明"
    assert int(shown.group(2)) == total
    assert int(shown.group(1)) == total - 1, "slider 默认没停在最后一步"
    assert page.locator('[data-testid="stSlider"]').count() == 1


def test_three_node_kinds_are_visually_distinguishable(page: Any) -> None:
    """三类节点在时间线与调用图上要能分出来（v6 §8.2）。"""
    _tab(page, "运作过程")
    text = _body(page)
    assert "Agent（自主决定循环轮数）" in text
    assert "LLM 节点（调模型，轮数固定）" in text
    assert "确定性节点（不经 Harness、不读 Skill）" in text
    assert "⚙" in text and "▸" in text, "时间线缺三类图标"


def test_timeline_expanders_are_ordered_by_seq(page: Any) -> None:
    _tab(page, "运作过程")
    seqs = [int(n) for n in re.findall(r"#(\d+) [⚙▸🤖·]", _body(page))]
    assert seqs == sorted(seqs) and seqs[0] == 0, f"时间线不是按 seq 排的：{seqs}"


def test_call_graph_is_rendered(page: Any) -> None:
    _tab(page, "运作过程")
    assert "调用图（本次实际走过的路径，含回环次数）" in _body(page)
    assert page.locator('[data-testid="stGraphVizChart"]').count() >= 1


def test_solver_panel_shows_status_and_runway_allocation(page: Any) -> None:
    """求解面板：候选/变量/约束/状态/目标值/gap/耗时 + **跑道分配统计**。"""
    _tab(page, "运作过程")
    text = _body(page)
    for label in ("候选数", "变量数", "约束数", "求解状态", "目标值", "gap", "耗时"):
        assert label in text, f"求解面板缺 {label}"
    assert "跑道分配统计" in text
    assert "OPTIMAL" in text
    assert re.search(r"RWY-1 \d+ 架次", text)


# ─────────────────────────────────────────────────────────────────────
# 页签四 · 解释报告
# ─────────────────────────────────────────────────────────────────────
def test_explanation_tab_renders(page: Any) -> None:
    _tab(page, "解释报告")
    text = _body(page)
    assert "解释报告" in text
    assert "本次运行还没有解释" not in text


# ─────────────────────────────────────────────────────────────────────
# 人工门禁（v6 §7.2.4 / §8.3）
# ─────────────────────────────────────────────────────────────────────
def test_gate_offers_approve_and_reject(page: Any) -> None:
    assert page.get_by_role("button", name="✓ 确认并归档").is_visible()
    assert page.get_by_role("button", name="✗ 驳回").is_visible()


def test_relaxation_tiers_t0_to_t3_are_offered(page: Any) -> None:
    text = _body(page)
    for tier in ("T0 · 全硬约束", "T1 ·", "T2 ·", "T3 ·"):
        assert tier in text, f"松弛档位缺 {tier}"


def test_tier_two_wording_follows_d6(page: Any) -> None:
    """**D-6**：T2 是「约束3 整体降级为软目标」，不是旧的「A 类降至每人 1 次」。"""
    text = _body(page)
    assert "T2 · T1 + 约束3「A 类每周必飞」整体降级为软目标" in text
    assert "A 类降至每人 1 次" not in text


# ─────────────────────────────────────────────────────────────────────
# 浏览器存储 & 跨进程恢复
# ─────────────────────────────────────────────────────────────────────
def test_frontend_uses_no_browser_storage(page: Any) -> None:
    """前端不使用任何浏览器存储 API（CLAUDE.md §11 反模式）。

    两头都查：**页面运行时**没往 localStorage / sessionStorage / cookie 写东西，
    **源码里**也没有那几个 API 的字面量。只查其中一头都能被绕过。
    """
    stored = page.evaluate(
        "() => ({local: Object.keys(localStorage).length,"
        " session: Object.keys(sessionStorage).length})"
    )
    assert stored["local"] == 0, f"localStorage 里有东西：{stored}"
    assert stored["session"] == 0, f"sessionStorage 里有东西：{stored}"

    # 源码侧只查**代码**，不查注释与 docstring —— `frontend/state.py` 的模块注释里
    # 正大光明地写着「为什么不用 localStorage」，按词禁会把那段说明一起禁掉，
    # 而那段说明恰恰是这条规矩的载体。用 AST 把 docstring 摘出去再查。
    for path in Path("frontend").rglob("*.py"):
        for literal in _code_string_literals(path):
            for api in ("localStorage", "sessionStorage", "document.cookie"):
                assert api not in literal, f"{path} 的代码里出现了 {api}"
        # Streamlit 里要碰浏览器存储只有一条路：塞一段 JS 进 components.html。
        # 直接把那条路堵上，比枚举 API 名字更靠谱。
        source = path.read_text(encoding="utf-8")
        assert "components.html" not in source, f"{path} 用了 components.html（可注入 JS）"
        assert "<script" not in source, f"{path} 里有 <script>"


def test_hitl_survives_a_worker_restart_and_archives(
    page: Any, stack: dict[str, Any], snapshot_id: str, plans_root: Path
) -> None:
    """**跨进程重启的 HITL 恢复**（v6 §9.2：人工确认可以隔天再来）。

    先把 worker 进程杀掉再起一个新的 —— 新进程只拿 `thread_id` 从
    `PostgresSaver` 恢复，**不重跑求解**。然后按「确认并归档」，
    归档产物落盘、下载按钮出现。
    """
    with restored_db(snapshot_id):
        restart_worker(stack)
        page.get_by_role("button", name="✓ 确认并归档").click()
        page.wait_for_selector("text=已提交确认", timeout=RUN_TIMEOUT_MS)
        # 归档跑完 → 门禁那一屏消失（`gate.awaiting` 为假）
        page.wait_for_selector(
            "button:has-text('确认并归档')", state="hidden", timeout=RUN_TIMEOUT_MS
        )
        # 下载按钮在「排班结果」页签里，而上一条用例把活动页签留在了「解释报告」——
        # Streamlit 的非活动页签仍在 DOM 里但是 hidden 的（实测踩过：
        # `452 × locator resolved to hidden`）
        _tab(page, "排班结果")
        page.wait_for_selector("button:has-text('下载 xlsx')", timeout=RUN_TIMEOUT_MS)

        text = _body(page)
        assert "已提交确认" in text

        archived = list(plans_root.rglob("*.xlsx"))
        assert archived, f"{plans_root} 下没有归档产物"
        assert archived[0].stat().st_size > 5000
