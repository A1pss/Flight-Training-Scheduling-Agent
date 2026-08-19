"""模型完整性双重校验（v6 §11.5「模型完整性」）。

> Ollama 模型固定 digest，`healthcheck.sh` 与应用启动时**双重校验 SHA256**，
> 防止模型被替换。

## 「双重」指的是两个时机，不是两套算法

两处校验共用 `backend/core/integrity.py`。故意分两套实现的话，算法一旦分叉
其中一边就变成橡皮图章 —— 一个算 manifest 的 sha256、另一个算 blob 的，
谁都不报错，而模型被换掉时两边都放行。

## 出口标准：**故意改一个 digest，确认两边都失败**

`test_tampered_digest_fails_both_gates` 就是那条：把期望 digest 改一位，
① `verify_model_digest` 判失败、② `create_app` 抛 `ModelIntegrityError` 起不来、
③ `python -m backend.core.integrity` 退出码非 0（healthcheck 调的就是它）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from backend.core.config import PROJECT_ROOT, Settings
from backend.core.integrity import (
    ModelIntegrityError,
    compute_digest,
    enforce_model_integrity,
    expected_digest,
    manifest_path,
    shipped_pin,
    verify_model_digest,
)

pytestmark = pytest.mark.guardrail

FAKE_MANIFEST = b'{"schemaVersion":2,"layers":[{"digest":"sha256:deadbeef"}]}'


def _settings(tmp_path: Path, *, digest: str, model: str = "qwen2.5:14b-q4") -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        LLM_PROVIDER="ollama",
        LLM_MODEL=model,
        LLM_MODEL_DIGEST=digest,
        OLLAMA_MODELS=tmp_path,
    )


def _plant_manifest(tmp_path: Path, model: str = "qwen2.5:14b-q4") -> tuple[Path, str]:
    """在临时 `OLLAMA_MODELS` 下造一份 manifest，返回 (路径, 真实 digest)。"""
    repo, _, tag = model.partition(":")
    path = tmp_path / "manifests" / "registry.ollama.ai" / "library" / repo / tag
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(FAKE_MANIFEST)
    return path, compute_digest(path)


# ═════════════════════════════════════════════════════════════════════
# 正向
# ═════════════════════════════════════════════════════════════════════
def test_matching_digest_passes(tmp_path: Path) -> None:
    _, digest = _plant_manifest(tmp_path)
    check = verify_model_digest(_settings(tmp_path, digest=digest))
    assert check.ok and not check.skipped
    assert check.actual == digest


def test_manifest_path_follows_the_ollama_layout(tmp_path: Path) -> None:
    settings = _settings(tmp_path, digest="sha256:x", model="qwen2.5:14b-instruct-q4_K_M")
    assert manifest_path(settings).relative_to(tmp_path).as_posix() == (
        "manifests/registry.ollama.ai/library/qwen2.5/14b-instruct-q4_K_M"
    )


def test_provider_mock_skips_the_check(tmp_path: Path) -> None:
    """`mock` / `replay` 两态整体跳过 —— 它们一次都不碰 Ollama（v6 §11.2）。

    不跳过的话 CI 会因为「没有模型文件」而起不来，那是纯粹的假红。
    """
    settings = Settings(_env_file=None, LLM_PROVIDER="mock", OLLAMA_MODELS=tmp_path)  # type: ignore[call-arg]
    check = verify_model_digest(settings)
    assert check.skipped and check.ok


def test_shipped_pin_is_read_from_env_example() -> None:
    """`.env` 缺失时回落到 `.env.example` 的出厂钉子（开发机上通常没有 `.env`）。"""
    pin = shipped_pin()
    assert pin.startswith("sha256:") and len(pin) == 71
    assert pin in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")


def test_env_digest_overrides_the_shipped_pin(tmp_path: Path) -> None:
    settings = _settings(tmp_path, digest="sha256:" + "a" * 64)
    assert expected_digest(settings) == "sha256:" + "a" * 64


# ═════════════════════════════════════════════════════════════════════
# ★ 出口标准：改一个 digest，两道门都要失败
# ═════════════════════════════════════════════════════════════════════
def test_tampered_digest_fails_both_gates(tmp_path: Path) -> None:
    """把期望 digest 改一位 —— 判定、启动、CLI 三处一起失败。"""
    _, real = _plant_manifest(tmp_path)
    # 只改最后一个字符：这是「模型被换了」在 digest 上的最小可见形态
    tampered = real[:-1] + ("0" if real[-1] != "0" else "1")
    settings = _settings(tmp_path, digest=tampered)

    # ① 判定层
    check = verify_model_digest(settings)
    assert not check.ok
    assert "digest 不匹配" in check.reason
    assert check.actual == real and check.expected == tampered

    # ② 应用启动层：起不来，且异常类型明确
    with pytest.raises(ModelIntegrityError) as excinfo:
        enforce_model_integrity(settings)
    assert "模型可能已被替换" in str(excinfo.value)

    # ③ 应用工厂层（`create_app` 真的会拒绝建 app）
    from backend.api.main import create_app

    with pytest.raises(ModelIntegrityError):
        create_app(settings=settings)


def test_tampered_digest_fails_the_cli_that_healthcheck_calls(tmp_path: Path) -> None:
    """`healthcheck.sh` 调的是这个 CLI —— 它必须以非 0 退出，体检才会红。"""
    _, real = _plant_manifest(tmp_path)
    tampered = real[:-1] + ("0" if real[-1] != "0" else "1")
    env_extra = {
        "LLM_PROVIDER": "ollama",
        "LLM_MODEL": "qwen2.5:14b-q4",
        "OLLAMA_MODELS": str(tmp_path),
    }
    import os

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "backend.core.integrity", "--require", "--expected", tampered],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, **env_extra},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "校验失败" in result.stdout

    ok = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "backend.core.integrity", "--require", "--expected", real],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, **env_extra},
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_missing_manifest_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    """模型文件不在 = 失败。**不是「没什么可比就放行」** —— 那是最经典的假绿。"""
    check = verify_model_digest(_settings(tmp_path, digest="sha256:" + "a" * 64))
    assert not check.ok
    assert "manifest 不存在" in check.reason


def test_unconfigured_digest_is_a_failure_under_ollama(tmp_path: Path) -> None:
    """在用 Ollama 却没配 digest = 失败。这道防线不许「没配就当没有」。"""
    _plant_manifest(tmp_path)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        LLM_PROVIDER="ollama",
        LLM_MODEL="qwen2.5:14b-q4",
        LLM_MODEL_DIGEST="",
        OLLAMA_MODELS=tmp_path,
    )
    # 出厂钉子指的是真模型，与这份假 manifest 必然不同 → 照样失败
    check = verify_model_digest(settings)
    assert not check.ok


def test_healthcheck_script_delegates_to_the_shared_module() -> None:
    """体检脚本必须调这个模块，而不是自己再写一遍 sha256（防算法分叉）。"""
    text = (PROJECT_ROOT / "deploy" / "native" / "healthcheck.sh").read_text(encoding="utf-8")
    assert "python -m backend.core.integrity" in text
    assert "sha256sum" not in text, "体检脚本不该自己算 digest —— 判据只能有一份"


def test_app_startup_logs_the_check_result() -> None:
    """启动日志要留下这次校验的结论，出事时能回查。"""
    text = (PROJECT_ROOT / "backend" / "api" / "main.py").read_text(encoding="utf-8")
    assert "enforce_model_integrity" in text
    assert "model_integrity" in text


# ═════════════════════════════════════════════════════════════════════
# CLI 的 main()（healthcheck.sh 调的入口）
# ═════════════════════════════════════════════════════════════════════
def test_cli_main_reports_success_in_process(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 进程内直接调 `main()`。

    上面那两条用 `subprocess` 跑 CLI —— 那验证的是「真实调用形态」，但
    **coverage 收不到子进程**（与 M6 对 `RQRunner` 的处置同一条）。这里补一组
    进程内调用，让这个入口的分支真的被覆盖到，而不是「测过但显示 0%」。
    """
    from backend.core import integrity
    from backend.core.config import get_settings

    _, real = _plant_manifest(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:14b-q4")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    get_settings.cache_clear()
    try:
        assert integrity.main(["--expected", real]) == 0
        out = capsys.readouterr().out
        assert "校验通过" in out
        assert real in out
    finally:
        get_settings.cache_clear()


def test_cli_main_returns_one_on_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core import integrity
    from backend.core.config import get_settings

    _, real = _plant_manifest(tmp_path)
    tampered = real[:-1] + ("0" if real[-1] != "0" else "1")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:14b-q4")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    get_settings.cache_clear()
    try:
        assert integrity.main(["--expected", tampered]) == 1
        assert "校验失败" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_cli_require_flag_forces_the_check_under_mock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--require`：`LLM_PROVIDER=mock` 时也照样校验。

    体检的职责是**看真机状态**，不是「按当前进程的配置决定要不要看」。
    """
    from backend.core import integrity
    from backend.core.config import get_settings

    _, real = _plant_manifest(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:14b-q4")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    get_settings.cache_clear()
    try:
        assert integrity.main(["--require", "--expected", real]) == 0
        assert "跳过" not in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_cli_without_require_skips_under_mock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core import integrity
    from backend.core.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    get_settings.cache_clear()
    try:
        assert integrity.main([]) == 0
        assert "跳过模型 digest 校验" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_shipped_pin_tolerates_a_missing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env.example` 不在时返回空串，**不抛** —— 那会让判定层拿不到「未配置」这个结论。"""
    from backend.core import integrity

    monkeypatch.setattr(integrity, "SHIPPED_PIN_FILE", Path("/nonexistent/.env.example"))
    assert integrity.shipped_pin() == ""
