"""模型完整性校验（v6 §11.5「模型完整性」那一行）。

> Ollama 模型固定 digest，`healthcheck.sh` 与应用启动时**双重校验 SHA256**，
> 防止模型被替换。

## 为什么两处校验共用这一个模块

「双重校验」很容易被做成两份各写各的实现——`healthcheck.sh` 里一段 shell、
应用启动时一段 Python。那样做的结果是**两边算法一旦分叉，其中一边就变成了
橡皮图章**：一个算 manifest 文件的 sha256、另一个算 blob 的，谁都不报错，
而模型被换掉时两边都放行。

所以这里只写一次，`healthcheck.sh` 通过 ``python -m backend.core.integrity``
调它。两处校验的**判据完全相同**，差别只在触发时机（开工体检 / 进程启动）。

## digest 算的是什么

Ollama v0.6.8 的 `/api/show` 不返回 digest，`ollama list` 的 ID 列来自
**manifest 文件本身的 sha256**。这里取同一个量：

    $OLLAMA_MODELS/manifests/registry.ollama.ai/library/<name>/<tag>

manifest 里逐条记着各层 blob 的 digest 与大小，改动任何一层权重都会让
manifest 变，从而让这个 sha256 变。**校验 manifest 等价于校验整棵树**，
且不必读那 9 GB 权重。

## 期望值从哪来

1. `Settings.LLM_MODEL_DIGEST`（即 `.env`）——生产形态，install.sh 会把
   `.env.example` 复制成 `.env`；
2. 没配时回落到仓库里的 `.env.example`——开发机上通常没有 `.env`，而
   `.env.example` 里那一行就是随代码一起版本化的**出厂钉子**。

两者都没有 → 视为「未配置」。`LLM_PROVIDER=ollama` 时未配置即失败：真在用
这个模型，却不知道该用哪个 digest，等于没有这道防线。`mock` / `replay`
两态下**整体跳过**——那两条路一次都不碰 Ollama（v6 §11.2），拿一个用不上的
digest 去卡 CI 启动只会制造假红。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from backend.core.config import PROJECT_ROOT, Settings, get_settings

#: Ollama 的 manifest 目录布局（相对 `OLLAMA_MODELS`）
MANIFEST_PREFIX: Final[str] = "manifests/registry.ollama.ai/library"

#: 出厂钉子所在文件。`.env` 缺失时的回落来源。
SHIPPED_PIN_FILE: Final[Path] = PROJECT_ROOT / ".env.example"

_PIN_RE: Final[re.Pattern[str]] = re.compile(r"^LLM_MODEL_DIGEST=(\S*)\s*$", re.MULTILINE)


class ModelIntegrityError(Exception):
    """模型 digest 校验失败。

    **不派生自 `FTSError`**：v6 §9.3 的 16 个码全是业务语义，没有一个是
    「进程不该启动」。硬塞一个码进去会让 `retryable` 这一位撒谎——模型被换掉
    这件事重试一百次也是同一个结果。与 `AuthError` / `SkillError` 同一处置。
    """


@dataclass(frozen=True)
class DigestCheck:
    """一次校验的结果。`ok=False` 时 `reason` 一定非空。"""

    ok: bool
    model: str
    expected: str
    actual: str
    manifest: Path
    reason: str = ""
    skipped: bool = False

    def render(self) -> str:
        """给 healthcheck 与启动日志用的一行摘要。"""
        if self.skipped:
            return f"跳过模型 digest 校验：{self.reason}"
        if self.ok:
            return f"模型 {self.model} digest 校验通过（{self.expected[:23]}…）"
        return f"模型 {self.model} digest 校验失败：{self.reason}"


def manifest_path(settings: Settings, model: str | None = None) -> Path:
    """模型 tag 对应的 manifest 文件路径。"""
    name = model or settings.LLM_MODEL
    repo, _, tag = name.partition(":")
    return settings.OLLAMA_MODELS / MANIFEST_PREFIX / repo / (tag or "latest")


def compute_digest(path: Path) -> str:
    """manifest 文件的 sha256，带 `sha256:` 前缀（与 `ollama list` 的 ID 同源）。"""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def shipped_pin() -> str:
    """从 `.env.example` 读出厂钉子。文件缺失或没有该行时返回空串。"""
    try:
        text = SHIPPED_PIN_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    found = _PIN_RE.search(text)
    return found.group(1).strip() if found else ""


def expected_digest(settings: Settings) -> str:
    """期望 digest：`.env` 优先，回落到 `.env.example` 的出厂钉子。"""
    return settings.LLM_MODEL_DIGEST.strip() or shipped_pin()


def verify_model_digest(settings: Settings | None = None) -> DigestCheck:
    """校验当前 Ollama 模型的 digest。**只判定，不抛**。

    `LLM_PROVIDER` 不是 `ollama` 时返回 `skipped=True`。
    """
    cfg = settings or get_settings()
    model = cfg.LLM_MODEL
    path = manifest_path(cfg)
    expected = expected_digest(cfg)

    if cfg.LLM_PROVIDER != "ollama":
        return DigestCheck(
            ok=True,
            model=model,
            expected=expected,
            actual="",
            manifest=path,
            reason=f"LLM_PROVIDER={cfg.LLM_PROVIDER}，本进程不调用 Ollama",
            skipped=True,
        )

    if not expected:
        return DigestCheck(
            ok=False,
            model=model,
            expected="",
            actual="",
            manifest=path,
            reason=(
                "未配置 LLM_MODEL_DIGEST，且 .env.example 里也没有出厂钉子 —— "
                "正在用 Ollama 却没有可比对的 digest，这道防线等于不存在"
            ),
        )

    if not path.is_file():
        return DigestCheck(
            ok=False,
            model=model,
            expected=expected,
            actual="",
            manifest=path,
            reason=f"模型 manifest 不存在：{path}（模型未拉取？跑 deploy/native/pull_models.sh）",
        )

    actual = compute_digest(path)
    if actual != expected:
        return DigestCheck(
            ok=False,
            model=model,
            expected=expected,
            actual=actual,
            manifest=path,
            reason=(f"digest 不匹配：期望 {expected}，实际 {actual} —— 模型可能已被替换，拒绝继续"),
        )
    return DigestCheck(ok=True, model=model, expected=expected, actual=actual, manifest=path)


def enforce_model_integrity(settings: Settings | None = None) -> DigestCheck:
    """校验并在失败时抛 :class:`ModelIntegrityError`。**应用启动时调这个。**"""
    check = verify_model_digest(settings)
    if not check.ok:
        raise ModelIntegrityError(check.render())
    return check


def main(argv: list[str] | None = None) -> int:
    """`python -m backend.core.integrity` —— `healthcheck.sh` 调的就是它。"""
    parser = argparse.ArgumentParser(description="Ollama 模型 digest 校验（v6 §11.5）")
    parser.add_argument("--expected", default="", help="覆盖期望 digest（体检脚本用）")
    parser.add_argument(
        "--require",
        action="store_true",
        help="即便 LLM_PROVIDER 不是 ollama 也强制校验（体检脚本用）",
    )
    args = parser.parse_args(argv)

    cfg = get_settings()
    if args.expected or args.require:
        overrides: dict[str, str] = {}
        if args.expected:
            overrides["LLM_MODEL_DIGEST"] = args.expected
        if args.require:
            overrides["LLM_PROVIDER"] = "ollama"
        cfg = cfg.model_copy(update=overrides)

    check = verify_model_digest(cfg)
    print(check.render())
    if check.actual:
        print(f"  manifest = {check.manifest}")
        print(f"  expected = {check.expected}")
        print(f"  actual   = {check.actual}")
    return 0 if check.ok else 1


__all__ = [
    "DigestCheck",
    "ModelIntegrityError",
    "compute_digest",
    "enforce_model_integrity",
    "expected_digest",
    "manifest_path",
    "shipped_pin",
    "verify_model_digest",
]


if __name__ == "__main__":  # pragma: no cover - 入口薄封装
    sys.exit(main())
