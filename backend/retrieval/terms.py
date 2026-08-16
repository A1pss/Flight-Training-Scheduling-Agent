"""术语对齐表（v6 §6.5.2 查询改写第三步）。

「起落航线」是口语，`missionA-1` / `missionA-2` 是系统术语。查询改写要把前者
变成后者，否则 BM25 与向量两路都在拿一个系统里根本不存在的词去召回。

## 三条口径（与 `rules/terminology.yaml` 头部注释一致）

1. **只做「表述 → 候选集」，不做排班判断。** 「仪表」→ C 类之后，落到哪门
   课目、该不该排，仍由 S-01 类展开与资质/先修判定决定。
2. **target 不在当前快照里就静默失效。** 别名指向的类别/跑道/空域若本次数据
   里没有，该条不生效也不报错 —— 换一批数据不该让系统崩，也不该把基准数据的
   编号偷偷带进新快照（`CLAUDE.md` `Z-4`）。
3. **一个别名命中两个以上 target 就是歧义，反问。** 与「何超 / 高超」同一条
   规矩（v6 §6.5.3：命中多个候选时不自行选择）。

## 为什么是 YAML 不是代码常量

业务方要能自己加「我们内部管编队叫『跟飞』」这类说法，而不必找人改代码。
这与 `rules/semantics.yaml` 是同一个设计：**规格与裁定进配置，实现进代码。**
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

import yaml

from backend.core.config import PROJECT_ROOT
from backend.core.errors import RuleParseError

TermKind = Literal["mission_class", "runway", "airspace"]

#: 默认落点。与 `rules/ruleset_v1.3.yaml`、`rules/semantics.yaml` 同目录。
TERMINOLOGY_PATH: Final[Path] = PROJECT_ROOT / "rules" / "terminology.yaml"

#: YAML 里的三个小节 → 该节 target 的键名
_SECTIONS: Final[dict[str, tuple[TermKind, str]]] = {
    "missions": ("mission_class", "mission_class"),
    "runways": ("runway", "runway_id"),
    "airspaces": ("airspace", "airspace_id"),
}


def normalize(text: str) -> str:
    """匹配前的归一：去空白、转小写。

    「小区域 A」「小区域A」「Small Area A」「small area a」是同一个说法，
    用户不会照着某个规范写。**不做别的归一** —— 简繁转换、同音字替换这类
    操作会把「何超 / 高超」这种必须区分的对子给合并掉。
    """
    return "".join(text.split()).lower()


@dataclass(frozen=True)
class TermEntry:
    """一条别名 → target 的映射。"""

    alias: str
    kind: TermKind
    target: str

    @property
    def normalized(self) -> str:
        return normalize(self.alias)


@dataclass(frozen=True)
class TermMatch:
    """一次命中。`surface` 是原句里实际出现的那段字。"""

    surface: str
    kind: TermKind
    target: str

    def as_term(self) -> str:
        """给 BM25 用的系统术语（类别写成「A类」，与规则原文的写法一致）。"""
        return f"{self.target}类" if self.kind == "mission_class" else self.target


@dataclass(frozen=True)
class Terminology:
    """整张术语表。"""

    version: str
    entries: tuple[TermEntry, ...]

    def targets_of(self, kind: TermKind) -> tuple[str, ...]:
        return tuple(sorted({e.target for e in self.entries if e.kind == kind}))

    def align(
        self,
        text: str,
        *,
        known_mission_classes: Iterable[str] = (),
        known_runways: Iterable[str] = (),
        known_airspaces: Iterable[str] = (),
    ) -> tuple[tuple[TermMatch, ...], tuple[str, ...]]:
        """把一句话里的口语术语对齐到系统术语。

        返回 `(命中列表, 歧义列表)`。

        `known_*` 是**当前快照**里实际存在的取值；留空表示不过滤（单测里
        常这么用）。给了就按口径 ② 过滤 —— 指向不存在实体的别名静默失效。

        匹配用**子串**：用户发过来的是「刘斌的仪表等级什么时候到期」整句，
        不是孤零零两个字。命中多条时按别名长度降序保留最长的那条
        （「本场起落航线」优先于「起落航线」），避免同一段字被拆成两个命中。
        """
        allowed: dict[TermKind, frozenset[str] | None] = {
            "mission_class": frozenset(known_mission_classes) or None,
            "runway": frozenset(known_runways) or None,
            "airspace": frozenset(known_airspaces) or None,
        }
        haystack = normalize(text)

        hits: list[TermEntry] = []
        for entry in self.entries:
            scope = allowed[entry.kind]
            if scope is not None and entry.target not in scope:
                continue  # 口径 ②：当前快照里没有这个 target
            if entry.normalized and entry.normalized in haystack:
                hits.append(entry)

        # 长别名优先：「本场起落航线」吃掉「起落航线」与「本场」
        hits.sort(key=lambda e: (-len(e.normalized), e.kind, e.target, e.alias))
        matches: list[TermMatch] = []
        ambiguities: list[str] = []
        consumed: list[str] = []
        seen: set[tuple[TermKind, str]] = set()
        for entry in hits:
            if any(entry.normalized in c and entry.normalized != c for c in consumed):
                continue  # 已被更长的别名覆盖
            rivals = sorted(
                {
                    e.target
                    for e in hits
                    if e.normalized == entry.normalized and e.kind == entry.kind
                }
            )
            if len(rivals) > 1:
                # 口径 ③：同一个说法指向多个 target，不自行选一个
                note = f"「{entry.alias}」可能指 {'、'.join(rivals)}，请指明是哪一个"
                if note not in ambiguities:
                    ambiguities.append(note)
                consumed.append(entry.normalized)
                continue
            key = (entry.kind, entry.target)
            if key not in seen:
                seen.add(key)
                matches.append(TermMatch(surface=entry.alias, kind=entry.kind, target=entry.target))
            consumed.append(entry.normalized)
        return tuple(matches), tuple(ambiguities)


def parse_terminology(raw: Mapping[str, Any]) -> Terminology:
    """解析 YAML。**结构不对就抛**，不做「尽力而为」的部分加载。

    一张只加载了一半的术语表比没有更糟：它会让「仪表」翻译成功、「编队」
    静默失败，而两者的失败表现完全一样（召回不到），排查时看不出区别。
    """
    version = str(raw.get("version") or "").strip()
    if not version:
        raise RuleParseError(
            "terminology.yaml 缺少 version",
            details={"keys": sorted(raw)},
            suggestions=['在文件顶部补一行 `version: "1.0"`'],
        )

    entries: list[TermEntry] = []
    for section, (kind, target_key) in _SECTIONS.items():
        items = raw.get(section) or []
        if not isinstance(items, Sequence) or isinstance(items, str | bytes):
            raise RuleParseError(
                f"terminology.yaml 的 {section} 必须是列表",
                details={"section": section, "got": type(items).__name__},
            )
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise RuleParseError(
                    f"terminology.yaml {section}[{index}] 必须是映射",
                    details={"section": section, "index": index},
                )
            target = str(item.get(target_key) or "").strip()
            if not target:
                raise RuleParseError(
                    f"terminology.yaml {section}[{index}] 缺少 {target_key}",
                    details={"section": section, "index": index, "item": dict(item)},
                )
            aliases = item.get("aliases") or []
            if not isinstance(aliases, Sequence) or isinstance(aliases, str | bytes):
                raise RuleParseError(
                    f"terminology.yaml {section}[{index}].aliases 必须是列表",
                    details={"section": section, "index": index},
                )
            if not aliases:
                raise RuleParseError(
                    f"terminology.yaml {section}[{index}] 的 aliases 为空 —— "
                    "一条没有别名的映射不起任何作用，八成是写漏了",
                    details={"section": section, "target": target},
                )
            for alias in aliases:
                text = str(alias).strip()
                if not text:
                    continue
                entries.append(TermEntry(alias=text, kind=kind, target=target))

    if not entries:
        raise RuleParseError(
            "terminology.yaml 一条映射都没有",
            details={"path": str(TERMINOLOGY_PATH)},
        )
    return Terminology(version=version, entries=tuple(entries))


def load_terminology(path: Path | None = None) -> Terminology:
    """从磁盘读一份术语表。"""
    target = path or TERMINOLOGY_PATH
    if not target.is_file():
        raise RuleParseError(
            f"术语对齐表不存在：{target}",
            details={"path": str(target)},
            suggestions=["从仓库里恢复 rules/terminology.yaml，或按 v6 §6.5.2 重建"],
        )
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise RuleParseError(f"术语对齐表不是 YAML 映射：{target}", details={"path": str(target)})
    return parse_terminology(raw)


@lru_cache(maxsize=1)
def get_terminology() -> Terminology:
    """进程内单例。测试里改表请用 ``get_terminology.cache_clear()``。"""
    return load_terminology()


__all__ = [
    "TERMINOLOGY_PATH",
    "TermEntry",
    "TermKind",
    "TermMatch",
    "Terminology",
    "get_terminology",
    "load_terminology",
    "normalize",
    "parse_terminology",
]
