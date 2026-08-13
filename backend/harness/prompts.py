"""提示词版本治理（v6 §7.7.1 第 8 行）。

> 每个 LLM 组件的提示词带 `prompt_version`，随 trace 一并记录。
> **提示词是代码：进 Git，改动触发 CI 跑该组件的 eval 子集，指标劣化即阻断合并**。
> §10.6 的 `manifest.yaml` 记录本次运行的全部 prompt 版本。

落地形态：`prompts/<组件>/<键>.md`，YAML frontmatter + 正文。

```markdown
---
component: route
prompt_key: system
prompt_version: v1
description: 意图路由的系统提示词
---
你是……
```

**提示词不是 Skill，两者不能混。** Skill 是业务方可编辑的知识层
（`authoritative: false`，改了不许影响排班结果，v6 §7.8.2）；提示词是代码，
改了必须过 CI、必须换版本号。所以 `prompts/` 在仓库里、进 Git、有锁文件，
而 `skills/` 在运行时按目录加载。

**锁文件 `prompts/PROMPTS.lock.json`** 记录每份提示词的 `prompt_version` 与正文
sha256。改了正文却不换版本号，`deploy/scripts/check_prompt_versions.sh` 直接
让 CI 红——不然 trace 里记的 `prompt_version` 就成了谎话：同一个 `v1` 对应过
两份不同的提示词，事后没法复现任何一次运行。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import Settings, get_settings
from backend.core.errors import RuleParseError
from backend.harness.types import ALL_COMPONENTS, ComponentName

#: 锁文件名（在 `PROMPTS_DIR` 下）。
LOCK_FILENAME: Final[str] = "PROMPTS.lock.json"

_FRONTMATTER = re.compile(r"^---\s*\n(?P<meta>.*?)\n---\s*\n(?P<body>.*)$", re.DOTALL)


class Prompt(BaseModel):
    """一份提示词。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component: ComponentName
    prompt_key: str = Field(min_length=1)
    prompt_version: str = Field(pattern=r"^v\d+$")
    description: str = ""
    body: str = Field(min_length=1)
    path: str = ""

    @property
    def ref(self) -> str:
        """`route/system` 形态的引用名。"""
        return f"{self.component}/{self.prompt_key}"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    @property
    def versioned(self) -> str:
        """随 trace / manifest 记录的形态：`route/system@v1`。"""
        return f"{self.ref}@{self.prompt_version}"


def parse_prompt(text: str, path: Path | None = None) -> Prompt:
    """解析一份带 frontmatter 的提示词文件。"""
    where = str(path) if path else "<内存>"
    match = _FRONTMATTER.match(text)
    if match is None:
        raise RuleParseError(
            f"提示词 {where} 缺少 YAML frontmatter",
            details={"path": where},
            suggestions=["提示词文件必须以 `---` 包裹的 frontmatter 开头，含 prompt_version"],
        )
    try:
        meta = yaml.safe_load(match.group("meta")) or {}
    except yaml.YAMLError as exc:
        raise RuleParseError(
            f"提示词 {where} 的 frontmatter 不是合法 YAML：{exc}",
            details={"path": where},
        ) from exc
    if not isinstance(meta, dict):
        raise RuleParseError(f"提示词 {where} 的 frontmatter 必须是映射", details={"path": where})

    body = match.group("body").strip()
    try:
        return Prompt(
            component=meta.get("component", ""),
            prompt_key=meta.get("prompt_key", ""),
            prompt_version=meta.get("prompt_version", ""),
            description=str(meta.get("description", "")),
            body=body,
            path=where,
        )
    except Exception as exc:  # pydantic 校验失败一律归为解析失败
        raise RuleParseError(
            f"提示词 {where} 的 frontmatter 不合契约：{exc}",
            details={"path": where, "meta": meta},
        ) from exc


class PromptRegistry:
    """加载并索引 `prompts/` 下的全部提示词。"""

    def __init__(self, prompts: dict[str, Prompt], root: Path) -> None:
        self._prompts = prompts
        self._root = root

    @classmethod
    def load(cls, root: Path | None = None, settings: Settings | None = None) -> PromptRegistry:
        cfg = settings or get_settings()
        base = root or cfg.PROMPTS_DIR
        prompts: dict[str, Prompt] = {}
        if base.is_dir():
            for path in sorted(base.rglob("*.md")):
                # 说明性文档不是提示词：README 与 `_` 开头的文件跳过
                if path.name == "README.md" or path.name.startswith("_"):
                    continue
                prompt = parse_prompt(path.read_text(encoding="utf-8"), path)
                if prompt.ref in prompts:
                    raise RuleParseError(
                        f"提示词 {prompt.ref} 重复定义",
                        details={"paths": [prompts[prompt.ref].path, str(path)]},
                    )
                prompts[prompt.ref] = prompt
        return cls(prompts, base)

    # ── 读 ───────────────────────────────────────────────────────────
    def get(self, component: ComponentName, prompt_key: str = "system") -> Prompt:
        ref = f"{component}/{prompt_key}"
        try:
            return self._prompts[ref]
        except KeyError as exc:
            raise RuleParseError(
                f"提示词 {ref} 不存在",
                details={"available": sorted(self._prompts)},
                suggestions=[f"在 {self._root}/{component}/ 下补一份 {prompt_key}.md"],
            ) from exc

    def refs(self) -> tuple[str, ...]:
        return tuple(sorted(self._prompts))

    def versions(self) -> dict[str, str]:
        """`{ref: version}`——进 §10.6 的 manifest.yaml 与每条 trace。"""
        return {ref: p.prompt_version for ref, p in sorted(self._prompts.items())}

    def lock_payload(self) -> dict[str, dict[str, str]]:
        """锁文件内容：版本 + 正文 sha256。"""
        return {
            ref: {"prompt_version": p.prompt_version, "sha256": p.sha256}
            for ref, p in sorted(self._prompts.items())
        }

    def missing_components(self) -> tuple[ComponentName, ...]:
        """哪些组件还没有 `system` 提示词。"""
        return tuple(c for c in ALL_COMPONENTS if f"{c}/system" not in self._prompts)

    # ── 锁文件核对 ───────────────────────────────────────────────────
    def diff_lock(self, lock: dict[str, Any]) -> tuple[str, ...]:
        """与锁文件比对，返回问题清单（空 = 一致）。"""
        problems: list[str] = []
        current = self.lock_payload()
        for ref, entry in sorted(current.items()):
            recorded = lock.get(ref)
            if recorded is None:
                problems.append(f"{ref}：新增提示词但没写进锁文件")
                continue
            if recorded.get("sha256") != entry["sha256"]:
                if recorded.get("prompt_version") == entry["prompt_version"]:
                    problems.append(
                        f"{ref}：正文改了但 prompt_version 还是 "
                        f"{entry['prompt_version']} —— 版本号必须递增，否则 trace 里的版本号是假的"
                    )
                else:
                    problems.append(
                        f"{ref}：版本已从 {recorded.get('prompt_version')} 改为 "
                        f"{entry['prompt_version']}，请更新锁文件并跑该组件的 eval 子集"
                    )
        for ref in sorted(set(lock) - set(current)):
            problems.append(f"{ref}：锁文件里有但仓库里没有")
        return tuple(problems)


__all__ = ["LOCK_FILENAME", "Prompt", "PromptRegistry", "parse_prompt"]
