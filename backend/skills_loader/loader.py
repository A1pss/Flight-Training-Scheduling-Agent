"""Skill 加载器（v6 §7.8）：frontmatter 解析 + `authoritative: false` 强制。

## 这个模块存在的唯一理由是那条红线

> **Skill 只影响 LLM 组件如何「解析、解释、措辞」，绝不影响「排什么班」。**
> （v6 §7.8.2）

硬约束的唯一来源是 `rules/ruleset_v1.3.yaml` + `rules/semantics.yaml` →
`compile_spec_node` → CP-SAT，**这条链路不读取任何 skill**。三重机制保障：

| 机制 | 落点 |
|---|---|
| 依赖禁令 | `.importlinter` 禁令二：`solver` / `nodes` / `validator` 不得 import 本包 |
| frontmatter 标记 | 本模块：未声明或声明为 `true` 的 skill **拒绝加载并报错**（§12.5.3 S3） |
| 隔离测试 | `tests/guardrail/` 与 `tests/integration/` 的 S1~S6 |

## `authoritative` 为什么必须是**显式** false

「没写就当 false」看起来更宽容，实际上把红线交给了健忘：有人新写一份 skill
忘了这一行，加载器放行，而这份文件将来被谁当成规格来引用，没有任何机制拦得住。
**显式声明是一次刻意的动作**，等于作者签字确认「这份文件不是判定依据」。

## 加载失败不静默跳过

一份 skill 坏了就抛，不是「跳过它接着加载别的」。静默跳过会让
「rule-interpretation 因为缩进写错没加载上」表现为「解释文本忽然变得干巴巴」，
排查方向全歪——这与铁律 7 对摄取的要求是同一条道理。

**例外只有一个：整个 `skills/` 目录不存在。** 那是 §12.5.3 S2 的场景——
「删除全部 skill 目录，重跑基准周 → 排班照常产出且合规，仅记 WARN」。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger

#: skill 正文文件名。目录名即 skill 名（`doc-parsing/aircraft` 这种带层级的也是）。
SKILL_FILENAME: Final[str] = "SKILL.md"

#: 每份 SKILL.md 正文首行必须出现的免责声明（v6 §7.8.1 示例的那句）。
#: 它不是形式主义：业务方打开文件第一眼就该看到「改错了架次一个字节都不会变」。
DISCLAIMER: Final[str] = "本文件不影响排班结果"

_FRONTMATTER = re.compile(r"^---\s*\n(?P<meta>.*?)\n---\s*\n(?P<body>.*)$", re.DOTALL)

_log = get_logger(__name__)


class SkillLoadError(RuntimeError):
    """一份 skill 没能加载。

    不派生自 `FTSError`：v6 §9.3 的 15 个码里没有为 skill 单列一个，而 skill
    的加载失败**不是业务错误**——它是「知识层文件写坏了」，处置是去改那份
    markdown，不进对外错误契约。硬塞进某个现有码只会污染那个码的统计口径。
    """


class SkillNotAuthoritativeError(SkillLoadError):
    """`authoritative` 未声明或声明为 `true` —— §12.5.3 S3 要拦的正是这个。"""


@dataclass(frozen=True)
class Skill:
    """一份已加载的 skill。"""

    name: str
    description: str
    body: str
    version: str = "1.0"
    consumers: tuple[str, ...] = ()
    path: str = ""

    @property
    def authoritative(self) -> bool:
        """恒为 False。

        做成属性而不是字段，是因为**它没有第二种取值**：`authoritative: true`
        的文件在加载期就被拒了，能构造出 `Skill` 对象就说明那一行是 false。
        留成字段会让下游产生「说不定有 true 的」这种错觉。
        """
        return False

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    @property
    def has_disclaimer(self) -> bool:
        """正文里有没有那句免责声明。"""
        return DISCLAIMER in self.body

    def summary(self) -> str:
        """渐进式披露的「名称 + 描述」那一档（v6 §7.8.1）。"""
        return f"{self.name}：{self.description}"


def parse_skill(text: str, *, name: str, path: Path | None = None) -> Skill:
    """解析一份 SKILL.md。**校验顺序刻意如此**：先有 frontmatter，再看 authoritative。"""
    where = str(path) if path else "<内存>"
    match = _FRONTMATTER.match(text)
    if match is None:
        raise SkillLoadError(f"skill {name}（{where}）缺少 YAML frontmatter")

    try:
        meta = yaml.safe_load(match.group("meta")) or {}
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"skill {name}（{where}）的 frontmatter 不是合法 YAML：{exc}") from exc
    if not isinstance(meta, dict):
        raise SkillLoadError(f"skill {name}（{where}）的 frontmatter 必须是映射")

    _assert_non_authoritative(meta, name=name, where=where)

    body = match.group("body").strip()
    if not body:
        raise SkillLoadError(f"skill {name}（{where}）正文为空")

    declared = str(meta.get("name", name))
    if declared != name:
        raise SkillLoadError(
            f"skill 目录名 {name!r} 与 frontmatter 的 name {declared!r} 不一致 —— "
            "路由表按目录名寻址，不一致会路由到一份不是它的文件"
        )

    consumers = meta.get("consumers") or []
    if not isinstance(consumers, list):
        raise SkillLoadError(f"skill {name}（{where}）的 consumers 必须是列表")

    return Skill(
        name=name,
        description=str(meta.get("description", "")).strip(),
        body=body,
        version=str(meta.get("version", "1.0")),
        consumers=tuple(str(c) for c in consumers),
        path=where,
    )


def _assert_non_authoritative(meta: Mapping[str, Any], *, name: str, where: str) -> None:
    """v6 §7.8.2：所有 skill 必须声明 `authoritative: false`；加载器拒绝其余情况。"""
    if "authoritative" not in meta:
        raise SkillNotAuthoritativeError(
            f"skill {name}（{where}）未声明 `authoritative`。"
            "所有 skill 必须显式写 `authoritative: false`（v6 §7.8.2）—— "
            "缺省不等于 false，缺省等于没人为这份文件的定位签过字"
        )
    value = meta["authoritative"]
    if value is not False:
        raise SkillNotAuthoritativeError(
            f"skill {name}（{where}）声明了 `authoritative: {value!r}`，拒绝加载。"
            "知识层永远不是约束判定依据：硬约束的唯一来源是 rules/*.yaml → "
            "compile_spec → CP-SAT，这条链路不读取任何 skill（v6 §7.8.2）"
        )


def discover(root: Path) -> Iterator[tuple[str, Path]]:
    """遍历 skills 根目录，产出 (skill 名, SKILL.md 路径)。

    skill 名是**相对根目录的 POSIX 路径**（`doc-parsing/aircraft`），与
    `SKILL_ROUTES` 的取值一致。用 `sorted` 保证顺序稳定——加载顺序不稳的话，
    `library_fingerprint()` 也就不稳了。
    """
    if not root.is_dir():
        return
    for skill_md in sorted(root.rglob(SKILL_FILENAME)):
        yield skill_md.parent.relative_to(root).as_posix(), skill_md


@dataclass(frozen=True)
class SkillLibrary:
    """已加载的全部 skill。"""

    skills: Mapping[str, Skill]
    root: Path
    #: 目录不存在或一份都没有时为 True（§12.5.3 S2 的 `WARN` 场景）
    empty: bool = False

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def require(self, name: str) -> Skill:
        skill = self.skills.get(name)
        if skill is None:
            raise SkillLoadError(f"skill {name!r} 未加载（已加载：{sorted(self.skills)}）")
        return skill

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.skills))

    def catalog(self) -> str:
        """渐进式披露的第一档：只有名称与描述占用上下文（v6 §7.8.1）。"""
        return "\n".join(f"- {self.skills[name].summary()}" for name in self.names())

    def fingerprint(self) -> str:
        """整个知识层的内容指纹。

        `skill_version` 与 `ruleset_version` **版本号独立**（v6 §7.8.4）：
        前者变了只需跑 §12.5.3 的隔离测试，后者变了要影子回归。指纹进
        manifest，让「这次运行读的是哪一版知识」查得到。
        """
        digest = hashlib.sha256()
        for name in self.names():
            digest.update(name.encode("utf-8"))
            digest.update(self.skills[name].sha256.encode("utf-8"))
        return digest.hexdigest()

    def missing_disclaimer(self) -> tuple[str, ...]:
        """正文里没写那句免责声明的 skill。CI 用它把这条约定钉住。"""
        return tuple(name for name in self.names() if not self.skills[name].has_disclaimer)


def load_library(root: Path | None = None, *, settings: Settings | None = None) -> SkillLibrary:
    """加载整个知识层。

    目录不存在或空目录 → 返回空库并记 `WARN`，**不抛**（§12.5.3 S2）。
    单份文件坏了 → 抛（见模块文档）。
    """
    cfg = settings or get_settings()
    base = root or cfg.SKILLS_DIR
    loaded: dict[str, Skill] = {}
    for name, path in discover(base):
        loaded[name] = parse_skill(path.read_text(encoding="utf-8"), name=name, path=path)

    if not loaded:
        _log.warning(
            "知识层为空，LLM 组件将不加载任何 skill（解释文本质量下降，排班结果不受影响）",
            extra={"skills_dir": str(base)},
        )
        return SkillLibrary(skills={}, root=base, empty=True)
    return SkillLibrary(skills=loaded, root=base)


def render_skills(library: SkillLibrary, names: Sequence[str]) -> str:
    """把命中的 skill 正文拼成一个上下文块（渐进式披露的第二档）。

    命中但没加载上的名字**不静默跳过**：拼一行「未加载」进去。解释文本忽然
    变差时，这一行是唯一能告诉你「是知识层没加载上」的线索。
    """
    chunks: list[str] = []
    for name in names:
        skill = library.get(name)
        if skill is None:
            chunks.append(f"# {name}\n（该 skill 未加载，本次不提供其知识）")
        else:
            chunks.append(f"# {skill.name} v{skill.version}\n{skill.body}")
    return "\n\n".join(chunks)


__all__ = [
    "DISCLAIMER",
    "SKILL_FILENAME",
    "Skill",
    "SkillLibrary",
    "SkillLoadError",
    "SkillNotAuthoritativeError",
    "discover",
    "load_library",
    "parse_skill",
    "render_skills",
]
