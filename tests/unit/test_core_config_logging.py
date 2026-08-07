"""配置与结构化日志单测（v6 §11.1 / §11.5）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog
from pydantic import ValidationError

from backend.core.config import PROJECT_ROOT, Settings, get_settings
from backend.core.logging import (
    bind_trace_id,
    clear_trace_id,
    configure_logging,
    get_logger,
    get_trace_id,
    make_person_redactor,
    new_trace_id,
)

# ─── Settings ────────────────────────────────────────────────────────


def test_default_ports_match_v6() -> None:
    """端口为避让系统既有服务而选（v6 §11.1）。"""
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.PG_PORT == 5433
    assert cfg.REDIS_PORT == 6380
    assert cfg.OLLAMA_HOST == "127.0.0.1:11434"
    assert cfg.APP_PORT == 8000
    assert cfg.FRONTEND_PORT == 8501


def test_gpu_pinned_to_device_three() -> None:
    """CLAUDE.md §2 硬约束：只用第 4 块卡。"""
    assert Settings(_env_file=None).CUDA_VISIBLE_DEVICES == "3"  # type: ignore[call-arg]


def test_solver_budget_defaults() -> None:
    """v6 §3.11 求解预算 + §3.9.2 探针预算池。"""
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.SOLVER_SEED == 42  # 可复现性硬要求（铁律 9）
    assert cfg.SOLVER_WORKERS == 4
    assert cfg.SOLVER_TIME_LIMIT_S == 30.0
    assert cfg.SOLVER_RESCHEDULE_TIME_LIMIT_S == 120.0
    assert cfg.SOLVER_DIAGNOSE_TIME_LIMIT_S == 300.0
    assert (cfg.PROBE_TIME_LIMIT_S, cfg.PROBE_MAX_CALLS, cfg.PROBE_TOTAL_BUDGET_S) == (
        30.0,
        5,
        120.0,
    )


def test_derived_urls() -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.DATABASE_URL == "postgresql+psycopg://fts:fts@127.0.0.1:5433/fts"
    assert cfg.REDIS_URL == "redis://127.0.0.1:6380/0"
    assert cfg.OLLAMA_BASE_URL == "http://127.0.0.1:11434"


def test_ollama_host_strips_scheme() -> None:
    cfg = Settings(_env_file=None, OLLAMA_HOST="http://127.0.0.1:11434/")  # type: ignore[call-arg]
    assert cfg.OLLAMA_HOST == "127.0.0.1:11434"


def test_llm_provider_is_enum_constrained() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, LLM_PROVIDER="openai")  # type: ignore[call-arg]


def test_default_model_is_unified_dev_and_prod() -> None:
    """v6 §11.2：开发期 = 上线期，同一个模型 tag。"""
    assert Settings(_env_file=None).LLM_MODEL == "qwen2.5:14b-instruct-q4_K_M"  # type: ignore[call-arg]


def test_rules_paths_point_into_repo() -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.RULESET_PATH == PROJECT_ROOT / "rules" / "ruleset_v1.3.yaml"
    assert cfg.SEMANTICS_PATH == PROJECT_ROOT / "rules" / "semantics.yaml"
    assert cfg.RULESET_PATH.is_file()
    assert cfg.SEMANTICS_PATH.is_file()


def test_settings_reject_invalid_numbers() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SOLVER_WORKERS=0)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, LLM_TEMPERATURE=3.0)  # type: ignore[call-arg]


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()


# ─── 日志 ────────────────────────────────────────────────────────────


def test_trace_id_roundtrip() -> None:
    clear_trace_id()
    assert get_trace_id() is None
    tid = bind_trace_id()
    assert get_trace_id() == tid and len(tid) == 32
    assert bind_trace_id("fixed") == "fixed"
    clear_trace_id()


def test_new_trace_id_unique() -> None:
    assert new_trace_id() != new_trace_id()


def test_person_redactor_masks_identity_keys() -> None:
    redact = make_person_redactor("***")
    out = redact(None, "", {"event": "solved", "person_id": "P04", "name": "刘斌"})
    assert out["person_id"] == "***"
    assert out["name"] == "***"
    assert out["event"] == "solved"


def test_person_redactor_masks_ids_in_free_text() -> None:
    redact = make_person_redactor("***")
    out = redact(None, "", {"event": "候选 P04 与 P08 冲突"})
    assert "P04" not in out["event"]
    assert out["event"] == "候选 *** 与 *** 冲突"


def test_person_redactor_handles_nested_crew() -> None:
    redact = make_person_redactor("#")
    out = redact(None, "", {"crew": [{"person_id": "P01", "name": "孙军"}]})
    assert out["crew"] == [{"person_id": "#", "name": "#"}]


def test_configure_logging_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = Settings(_env_file=None, LOG_FORMAT="json", LOG_REDACT_PERSON=True)  # type: ignore[call-arg]
    configure_logging(cfg)
    bind_trace_id("trace-abc")
    get_logger("test").info("排班完成", person_id="P05", sorties=14)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["trace_id"] == "trace-abc"
    assert payload["person_id"] == "***"  # 脱敏生效
    assert payload["sorties"] == 14  # 业务字段不受影响
    clear_trace_id()
    structlog.reset_defaults()


def test_configure_logging_can_disable_redaction(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = Settings(_env_file=None, LOG_FORMAT="json", LOG_REDACT_PERSON=False)  # type: ignore[call-arg]
    configure_logging(cfg)
    get_logger("test").info("x", person_id="P05")
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["person_id"] == "P05"
    structlog.reset_defaults()


def test_configure_logging_console_format(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = Settings(_env_file=None, LOG_FORMAT="console")  # type: ignore[call-arg]
    configure_logging(cfg)
    get_logger("test").warning("注意")
    assert "注意" in capsys.readouterr().out
    structlog.reset_defaults()


def test_project_root_contains_claude_md(project_root: Path) -> None:
    assert (project_root / "CLAUDE.md").is_file()
