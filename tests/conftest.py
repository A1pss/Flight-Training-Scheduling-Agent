"""pytest 公共 fixture。

**单元测试不依赖真实 Ollama**（CLAUDE.md §11 反模式）：默认把
`LLM_PROVIDER` 钉在 `mock`，任何试图走真机的测试必须显式标 `@pytest.mark.ollama`。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, time
from pathlib import Path

import pytest

from backend.core.config import PROJECT_ROOT, Settings, get_settings
from backend.schemas import CrewMember, Sortie

#: 插桩环境下的求解墙钟（业务方 2026-08-14 裁定，见 `reports/M4B_收工报告.md` §3.12）。
#:
#: **产品默认仍是 60s**（`Settings.SOLVER_TIME_LIMIT_S`，`Z-13`）。这里只抬高
#: **测试环境**的上限，理由是实测出来的：同一个基准周，无 coverage 插桩时
#: 18.8~26.0s 就证到 `OPTIMAL`（2276 候选 / 12568 变量，与 M2-A 逐字相同）；
#: 全量 `pytest --cov` 下同一个证明跑不完 60s，落到 `FEASIBLE`。
#:
#: **插桩是测量工具的开销，不是求解器变慢** —— 给插桩环境多给时间，证的还是
#: 同一个最优解，证完立即返回。这里**不放宽任何硬约束**，也不动产品默认值。
#: 写在 conftest 而不是靠 `export`，是为了让本地与 CI 自动一致（CLAUDE.md §6：
#: 验证时的视角必须与 CI 的视角一致）。
TEST_SOLVER_TIME_LIMIT_S = "180"


@pytest.fixture(autouse=True)
def _force_mock_provider(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """全局默认 mock provider；**只给集成用例**抬高插桩期求解墙钟。

    ⚠️ **墙钟只对 `@pytest.mark.integration` 生效，不是全局 setenv。**
    全局设了会连 `test_core_config_logging.py::test_solver_budget_defaults`
    一起改掉——那条守的正是「产品默认 60s」，而 `Settings(_env_file=None)`
    仍然会读 `os.environ`。**守默认值的测试必须看得见真正的默认值**，
    否则这个门禁就白设了（实测踩过：`assert 180.0 == 60.0`）。
    """
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("APP_ENV", "ci")
    if request.node.get_closest_marker("integration") is not None:
        monkeypatch.setenv("SOLVER_TIME_LIMIT_S", TEST_SOLVER_TIME_LIMIT_S)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """一份不读 `.env` 的干净配置，避免本机 `.env` 污染断言。"""
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def instructor() -> CrewMember:
    return CrewMember(person_id="P01", name="孙军", role="教员")


@pytest.fixture
def student() -> CrewMember:
    return CrewMember(person_id="P08", name="何超", role="学员")


@pytest.fixture
def solo_student() -> CrewMember:
    """学员执行 A 类课目时的角色是「单飞」（D-1）。"""
    return CrewMember(person_id="P08", name="何超", role="单飞")


@pytest.fixture
def recurrent_pilot() -> CrewMember:
    """刘斌 C 类到期后的复训架次角色（S-11）。"""
    return CrewMember(person_id="P04", name="刘斌", role="复训")


@pytest.fixture
def dual_sortie(instructor: CrewMember, student: CrewMember) -> Sortie:
    """一个合法的带飞架次（基准周周一，missionC-1）。"""
    return Sortie(
        sortie_id="S000001",
        date=date(2026, 1, 5),
        weekday="周一",
        takeoff=time(8, 0),
        landing=time(8, 35),
        mission_id="missionC-1",
        mission_name="仪表飞行",
        airspace_id="IFR",
        aircraft_id="AC10",
        runway_id="RWY-1",
        crew=[instructor, student],
    )


def pytest_configure(config: pytest.Config) -> None:
    """CI 上没有真实 Ollama，显式提示跳过口径。"""
    if os.environ.get("APP_ENV") == "ci":
        config.addinivalue_line("markers", "ollama: CI 环境自动跳过")
