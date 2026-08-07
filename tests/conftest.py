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


@pytest.fixture(autouse=True)
def _force_mock_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """全局默认 mock provider，并清掉配置单例缓存。"""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("APP_ENV", "ci")
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
