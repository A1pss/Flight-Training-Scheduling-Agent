"""`rules/ruleset_v1.3.yaml` 与 `rules/semantics.yaml` 的类型化加载器。

## 为什么这个加载器在 `core/` 而不在 `solver/`

`rules/ruleset_v1.3.yaml` 的文件头写着：「本文件是 solver/model.py 与
validator/checks.py **两套独立实现的共同依据**」。既然规格参数（20 分钟、
7 分钟、≤3 架次/日…）本身就是两边共用的**同一份数据**，那么「把 YAML 读成
Python 对象」这一步也应当只有一份实现——两份 YAML 解析器会各自漂移出不同的
默认值，那才是真正会让双通道校验失效的隐患。

**这不违反铁律 2。** 那条禁令针对的是「约束表达代码」：怎么把「20 分钟窗口内
起飞 ≤2 次」变成 CP-SAT 的 `AddCumulative` 或变成校验器的滑窗计数，两边必须
分别实现、互不引用。本模块**不表达任何约束**，它只做 `yaml.safe_load` + 字段
取值 + 类型化，与 `backend/core/config.py` 读 `.env` 是同一性质的动作。

## 参数取值优先级

**规则参数取本文件，实体数据取 PG。** 举两个具体例子：

- 空域同时段容量：`rules.yaml` 约束6 的 `params.airspace_capacity` 里抄了一份
  基准数据的值（SAA:2 …），但真正的数据源是 `aircraft.pdf` → `airspaces.capacity`。
  求解器一律读 PG，本文件那份只用于交叉核对（`cross_check_airspace_capacity`）。
- 周转时间：约束7 的 `params.turnaround_min` 按机型给了 JL-8=30 / JL-9=40，
  真正的数据源是 `aircraft.turnaround_minutes`（逐机一列）。同样读 PG。

理由见 CLAUDE.md §11：`JL-8` / `8 机` / `A~H` 一个都不许写成代码常量或校验上限。
**能从上传数据里读到的，就不许从 YAML 里读。**
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, cast

import yaml

from backend.core.config import get_settings
from backend.core.errors import RuleParseError, SemanticsUnconfirmedError

# ─────────────────────────────────────────────────────────────────────
# 身份取值
#
# §5.1.1：「唯一保留已知集合的是 identity 与 level」—— §3.1.1 的机组编成判定式
# 直接读它们，新增取值意味着新的编成规则，必须先有业务方裁决。所以这三个字面量
# 是**规格的一部分**，不是「把基准数据写成常量」。
# 与 `backend.models.entities.IDENTITIES` 的一致性由 tests/unit/test_ruleset_loader.py 钉住。
# ─────────────────────────────────────────────────────────────────────
IDENTITY_INSTRUCTOR: Final[str] = "教员"
IDENTITY_MATURE: Final[str] = "成熟飞行员"
IDENTITY_STUDENT: Final[str] = "学员"

#: 资质等级（`personnel.pdf` 课目级资质明细的「等级」）
LEVEL_INSTRUCTOR: Final[str] = "教员"
LEVEL_SOLO: Final[str] = "单飞"
LEVEL_DUAL: Final[str] = "带飞"

#: v6 §1.1 已封闭的语义开关集合。少一条即 FTS-1002（v6 §9.3）。
REQUIRED_SWITCHES: Final[tuple[str, ...]] = tuple(f"S-{i:02d}" for i in range(1, 14))

#: v6 §3.2 的 14 条规则编号
EXPECTED_RULE_IDS: Final[tuple[int, ...]] = tuple(range(1, 15))


def _as_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RuleParseError(f"{where} 应为映射，实际 {type(value).__name__}")
    return cast(Mapping[str, Any], value)


def _as_time(value: Any, where: str) -> time:
    """YAML 的 `"06:00"` → :class:`datetime.time`。

    PyYAML 会把不带引号的 `06:00` 读成 sexagesimal 整数（360），所以这里同时
    接受 `str` 与 `int`，并在两种形态下都还原成同一个时刻。
    """
    if isinstance(value, str):
        hh, _, mm = value.partition(":")
        try:
            return time(int(hh), int(mm or 0))
        except ValueError as exc:  # pragma: no cover - 由下面的 raise 统一表达
            raise RuleParseError(f"{where} 时刻格式非法：{value!r}") from exc
    if isinstance(value, int):
        return time(value // 60, value % 60)
    raise RuleParseError(f"{where} 时刻格式非法：{value!r}")


def _as_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleParseError(f"{where} 应为整数，实际 {value!r}")
    return value


# ─────────────────────────────────────────────────────────────────────
# ruleset
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RuleSpec:
    """单条规则的机器可读形态（`rules:` 列表的一项）。"""

    id: int
    check_id: str
    title: str
    tier: str
    relaxable: bool
    kind: str
    statement: str
    params: Mapping[str, Any]

    def param(self, key: str, where: str | None = None) -> Any:
        if key not in self.params:
            raise RuleParseError(f"约束{self.id}（{self.title}）缺少参数 {key}（{where or ''}）")
        return self.params[key]


@dataclass(frozen=True)
class RelaxationStep:
    """松弛阶梯的一档（v6 §3.10 Tier 0~3）。"""

    tier: int
    name: str
    relaxes: tuple[int, ...]
    authority: str | None
    note: str


@dataclass(frozen=True)
class Ruleset:
    """`ruleset_v1.3.yaml` 的类型化形态。

    只做取值与类型化，**不表达任何约束**（见模块文档）。
    """

    version: str
    rule_count: int
    rules: Mapping[int, RuleSpec]
    tier_relaxable: Mapping[str, bool]
    ladder: tuple[RelaxationStep, ...]

    # ── 约束1 训练窗 ────────────────────────────────────────────────
    @property
    def window_start(self) -> time:
        return _as_time(self.rules[1].param("window_start"), "约束1.window_start")

    @property
    def window_end(self) -> time:
        return _as_time(self.rules[1].param("window_end"), "约束1.window_end")

    @property
    def cross_day_allowed(self) -> bool:
        return bool(self.rules[1].param("cross_day_allowed"))

    # ── 约束2 到期日语义 ────────────────────────────────────────────
    @property
    def expiry_inclusive(self) -> bool:
        """到期日**当日**仍可执行（v6 §3.2 约束2）。"""
        return bool(self.rules[2].param("expiry_inclusive"))

    # ── 约束3 每周必飞 ──────────────────────────────────────────────
    @property
    def weekly_class_min(self) -> int:
        return _as_int(self.rules[3].param("weekly_a_class_min"), "约束3.weekly_a_class_min")

    # ── 约束8 间隔与休息 ────────────────────────────────────────────
    @property
    def min_gap_minutes(self) -> int:
        return _as_int(self.rules[8].param("min_gap_min"), "约束8.min_gap_min")

    @property
    def rest_after_n(self) -> int:
        return _as_int(self.rules[8].param("rest_after_n"), "约束8.rest_after_n")

    @property
    def rest_minutes(self) -> int:
        return _as_int(self.rules[8].param("rest_min"), "约束8.rest_min")

    # ── 约束9 起降密度 ──────────────────────────────────────────────
    @property
    def density_window_minutes(self) -> int:
        return _as_int(self.rules[9].param("window_min"), "约束9.window_min")

    @property
    def density_window_cap(self) -> int:
        return _as_int(self.rules[9].param("window_max_takeoffs"), "约束9.window_max_takeoffs")

    @property
    def separation_minutes(self) -> int:
        return _as_int(self.rules[9].param("separation_min"), "约束9.separation_min")

    @property
    def density_window_scope(self) -> str:
        return str(self.rules[9].param("window_scope"))

    @property
    def separation_scope(self) -> str:
        return str(self.rules[9].param("separation_scope"))

    # ── 约束10/11/12 上限 ───────────────────────────────────────────
    @property
    def daily_minutes_default(self) -> int:
        return _as_int(self.rules[10].param("default_min"), "约束10.default_min")

    @property
    def daily_minutes_student(self) -> int:
        return _as_int(self.rules[10].param("student_min"), "约束10.student_min")

    @property
    def weekly_sorties_default(self) -> int:
        return _as_int(self.rules[11].param("default_sorties"), "约束11.default_sorties")

    @property
    def weekly_sorties_student(self) -> int:
        return _as_int(self.rules[11].param("student_sorties"), "约束11.student_sorties")

    @property
    def daily_sorties_per_person(self) -> int:
        return _as_int(self.rules[12].param("per_person_per_day"), "约束12.per_person_per_day")

    @property
    def daily_sorties_per_aircraft(self) -> int:
        return _as_int(self.rules[12].param("per_aircraft_per_day"), "约束12.per_aircraft_per_day")

    # ── 约束13 复训窗口 ─────────────────────────────────────────────
    @property
    def recurrent_window_days(self) -> int:
        return _as_int(
            self.rules[13].param("recurrent_window_days"), "约束13.recurrent_window_days"
        )

    # ── 派生工具 ────────────────────────────────────────────────────
    def daily_minute_cap(self, identity: str) -> int:
        """约束10：学员 240，其余 480。"""
        return (
            self.daily_minutes_student
            if identity == IDENTITY_STUDENT
            else self.daily_minutes_default
        )

    def weekly_sortie_cap(self, identity: str) -> int:
        """约束11：学员 10，其余 12。"""
        return (
            self.weekly_sorties_student
            if identity == IDENTITY_STUDENT
            else self.weekly_sorties_default
        )

    def tier_of(self, rule_id: int) -> str:
        return self.rules[rule_id].tier

    def is_relaxable(self, rule_id: int) -> bool:
        """R0 恒不可松弛（v6 §3.10，代码层硬编码禁止）。"""
        rule = self.rules[rule_id]
        if rule.tier == "R0":
            return False
        return rule.relaxable and self.tier_relaxable.get(rule.tier, False)

    def ladder_step(self, tier: int) -> RelaxationStep:
        for step in self.ladder:
            if step.tier == tier:
                return step
        raise RuleParseError(f"松弛阶梯没有 Tier {tier}（合法档位 0~3）")

    def cross_check_airspace_capacity(self, from_db: Mapping[str, int]) -> tuple[str, ...]:
        """把 PG 里的空域容量与 YAML 里抄录的那份对一遍，返回不一致项。

        **PG 是真源**（`aircraft.pdf` → `airspaces.capacity`），YAML 那份只是
        规格文件里的抄录。不一致不抛异常——用户换一批数据时空域本来就会变，
        变的是数据不是规则；但对基准数据回归有价值，故返回差异供调用方记日志。
        """
        yaml_caps = _as_mapping(self.rules[6].param("airspace_capacity"), "约束6.airspace_capacity")
        diffs: list[str] = []
        for aid, cap in yaml_caps.items():
            if aid in from_db and from_db[aid] != cap:
                diffs.append(f"{aid}: YAML={cap} PG={from_db[aid]}")
        return tuple(diffs)


def parse_ruleset(raw: Mapping[str, Any]) -> Ruleset:
    """把 `yaml.safe_load` 的结果解析为 :class:`Ruleset`。"""
    version = str(raw.get("ruleset_version") or "")
    if not version:
        raise RuleParseError("ruleset 缺少 ruleset_version")

    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list):
        raise RuleParseError("ruleset 缺少 rules 列表")

    rules: dict[int, RuleSpec] = {}
    for item in rules_raw:
        entry = _as_mapping(item, "rules[]")
        rid = _as_int(entry.get("id"), "rules[].id")
        tier = str(entry.get("tier") or "")
        if tier not in ("R0", "R1", "R2", "R3"):
            raise RuleParseError(f"约束{rid} 的 tier={tier!r} 非法（合法值 R0~R3）")
        rules[rid] = RuleSpec(
            id=rid,
            check_id=str(entry.get("check_id") or f"C{rid:02d}"),
            title=str(entry.get("title") or ""),
            tier=tier,
            # 未显式写 relaxable 时按分级表推定：R0 不可松弛，其余可
            relaxable=bool(entry.get("relaxable", tier != "R0")),
            kind=str(entry.get("type") or ""),
            statement=str(entry.get("statement") or ""),
            params=_as_mapping(entry.get("params") or {}, f"约束{rid}.params"),
        )

    missing = [rid for rid in EXPECTED_RULE_IDS if rid not in rules]
    if missing:
        raise RuleParseError(f"ruleset 缺少约束 {missing}（v6 §3.2 共 14 条）")

    tiers_raw = _as_mapping(raw.get("tiers") or {}, "tiers")
    tier_relaxable = {
        name: bool(_as_mapping(body, f"tiers.{name}").get("relaxable", False))
        for name, body in tiers_raw.items()
    }
    if tier_relaxable.get("R0", False):
        raise RuleParseError("R0 安全刚性被标成了可松弛 —— 违反 v6 §3.10，代码层硬编码禁止")

    ladder_raw = raw.get("relaxation_ladder")
    if not isinstance(ladder_raw, list):
        raise RuleParseError("ruleset 缺少 relaxation_ladder")
    ladder: list[RelaxationStep] = []
    for item in ladder_raw:
        entry = _as_mapping(item, "relaxation_ladder[]")
        relaxes = tuple(
            _as_int(r, "relaxation_ladder[].relaxes[]")
            for r in cast(Sequence[Any], entry.get("relaxes") or ())
        )
        for rid in relaxes:
            if rules[rid].tier == "R0":
                raise RuleParseError(
                    f"松弛阶梯 Tier {entry.get('tier')} 试图松弛 R0 的约束{rid}"
                    " —— 违反 v6 §3.10「无论松弛到哪一级，R0 恒满足」"
                )
        ladder.append(
            RelaxationStep(
                tier=_as_int(entry.get("tier"), "relaxation_ladder[].tier"),
                name=str(entry.get("name") or ""),
                relaxes=relaxes,
                authority=str(entry["authority"]) if entry.get("authority") else None,
                note=str(entry.get("note") or ""),
            )
        )

    return Ruleset(
        version=version,
        rule_count=_as_int(raw.get("rule_count", len(rules)), "rule_count"),
        rules=rules,
        tier_relaxable=tier_relaxable,
        ladder=tuple(sorted(ladder, key=lambda s: s.tier)),
    )


# ─────────────────────────────────────────────────────────────────────
# semantics
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Semantics:
    """`semantics.yaml` 的类型化形态（v6 §1.1 S-01~S-13）。"""

    version: str
    switches: Mapping[str, Mapping[str, Any]]
    anchor_formula: Mapping[str, Any]

    def value(self, sid: str) -> str:
        try:
            return str(self.switches[sid]["value"])
        except KeyError as exc:
            raise SemanticsUnconfirmedError(f"语义开关 {sid} 未登记") from exc

    def snapshot(self) -> dict[str, str]:
        """S-01~S-13 的取值快照，进 `ConstraintSpec.semantics_switches` 并参与 sha256。"""
        return {sid: self.value(sid) for sid in sorted(self.switches)}

    # ── 逐条的类型化访问器 ─────────────────────────────────────────
    @property
    def s01_class_needs_all(self) -> bool:
        return self.value("S-01") == "all_missions_completed"

    @property
    def s02_class_level(self) -> bool:
        """A 类整体 ≥1 次（True）还是 A-1/A-2 各 1 次（False）。"""
        return self.value("S-02") == "class_level"

    @property
    def s03_incomplete_only(self) -> bool:
        return self.value("S-03") == "incomplete_only"

    @property
    def s04_half_open(self) -> bool:
        return self.value("S-04") == "half_open"

    @property
    def s05_dual_runway(self) -> bool:
        return self.value("S-05") == "dual_runway"

    @property
    def s05_density_scope(self) -> Mapping[str, str]:
        raw = _as_mapping(self.switches["S-05"].get("density_scope") or {}, "S-05.density_scope")
        return {str(k): str(v) for k, v in raw.items()}

    @property
    def s06_landing_to_takeoff(self) -> bool:
        return self.value("S-06") == "landing_to_takeoff"

    @property
    def s07_same_day_only(self) -> bool:
        return self.value("S-07") == "same_day_only"

    @property
    def s08_students_only(self) -> bool:
        """需带飞 = (mission.带飞 == 是) ∧ (person.身份 == 学员)。"""
        return self.value("S-08") == "students_only"

    @property
    def s09_instructors_exempt(self) -> bool:
        return self.value("S-09") in ("instructors_exempt_mature_recurrent", "all_exempt")

    @property
    def s09_mature_recurrent(self) -> bool:
        return self.value("S-09") == "instructors_exempt_mature_recurrent"

    @property
    def s10_airspace_hard(self) -> bool:
        return self.value("S-10") == "hard_constraint"

    @property
    def s11_enabled(self) -> bool:
        return self.value("S-11") == "convert_to_recurrent" and bool(
            self.switches["S-11"].get("enabled", True)
        )

    @property
    def s11_identities(self) -> tuple[str, ...]:
        raw = cast(Sequence[Any], self.switches["S-11"].get("applies_to_identities") or ())
        return tuple(str(x) for x in raw)

    @property
    def s11_window_days(self) -> int:
        return _as_int(self.switches["S-11"].get("window_days"), "S-11.window_days")

    @property
    def s11_start_offset_days(self) -> int:
        return _as_int(self.switches["S-11"].get("start_offset_days"), "S-11.start_offset_days")

    @property
    def s12_from_week_monday(self) -> bool:
        return self.value("S-12") == "from_week_monday"

    @property
    def s12_count_as_debt(self) -> bool:
        """**恒为 False**：S-12 明令「不计欠账」（`gap=999` 是 CLAUDE.md §11 的反模式）。"""
        return bool(self.switches["S-12"].get("count_as_debt", False))

    @property
    def s13_all_students(self) -> bool:
        return self.value("S-13") == "all_students"


def parse_semantics(raw: Mapping[str, Any]) -> Semantics:
    version = str(raw.get("semantics_version") or "")
    if not version:
        raise RuleParseError("semantics 缺少 semantics_version")
    switches_raw = _as_mapping(raw.get("switches") or {}, "switches")
    switches = {
        str(sid): _as_mapping(body, f"switches.{sid}") for sid, body in switches_raw.items()
    }
    missing = [sid for sid in REQUIRED_SWITCHES if sid not in switches]
    if missing:
        raise SemanticsUnconfirmedError(
            f"语义开关缺失：{missing}。v6 §1.1 的 S-01~S-13 已全部裁定，缺条即阻断排班"
        )
    unknown = sorted(set(switches) - set(REQUIRED_SWITCHES))
    if unknown:
        raise SemanticsUnconfirmedError(
            f"出现未裁定的语义开关 {unknown} —— 触发即意味着有人新增了未经业务方裁决的开关"
            "（v6 §9.3 FTS-1002）"
        )
    for sid, body in switches.items():
        options = cast(Sequence[Any], body.get("options") or ())
        if options and str(body.get("value")) not in {str(o) for o in options}:
            raise SemanticsUnconfirmedError(
                f"{sid} 的取值 {body.get('value')!r} 不在 options {list(options)} 内"
            )
    return Semantics(
        version=version,
        switches=switches,
        anchor_formula=_as_mapping(raw.get("frequency_anchor") or {}, "frequency_anchor"),
    )


# ─────────────────────────────────────────────────────────────────────
# 加载入口
# ─────────────────────────────────────────────────────────────────────
def _load_yaml(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        raise RuleParseError(f"规则文件不存在：{path}")
    with path.open("r", encoding="utf-8") as fh:
        return _as_mapping(yaml.safe_load(fh), str(path))


def load_ruleset(path: Path | None = None) -> Ruleset:
    """读 `rules/ruleset_v1.3.yaml`。路径缺省取 `Settings.RULESET_PATH`。"""
    return parse_ruleset(_load_yaml(path or get_settings().RULESET_PATH))


def load_semantics(path: Path | None = None) -> Semantics:
    """读 `rules/semantics.yaml`。路径缺省取 `Settings.SEMANTICS_PATH`。"""
    return parse_semantics(_load_yaml(path or get_settings().SEMANTICS_PATH))


@lru_cache(maxsize=1)
def get_ruleset() -> Ruleset:
    """进程内单例。测试改文件后请 ``get_ruleset.cache_clear()``。"""
    return load_ruleset()


@lru_cache(maxsize=1)
def get_semantics() -> Semantics:
    """进程内单例。测试改文件后请 ``get_semantics.cache_clear()``。"""
    return load_semantics()


def req_max_for(freq_days: int, week_days: int = 7) -> int:
    """约束14：`req_max = ceil(7 / freq_days)`（v6 §3.2）。

    A 类（freq_days=3）→ 3；B~F 类（7）→ 1；G/H 类（14）→ 1。
    `week_days` 参数化只为让公式的来源显式化，排班周恒为 7 天。
    """
    if freq_days <= 0:
        raise RuleParseError(f"freq_days 必须为正，实际 {freq_days}")
    return math.ceil(week_days / freq_days)


__all__ = [
    "EXPECTED_RULE_IDS",
    "IDENTITY_INSTRUCTOR",
    "IDENTITY_MATURE",
    "IDENTITY_STUDENT",
    "LEVEL_DUAL",
    "LEVEL_INSTRUCTOR",
    "LEVEL_SOLO",
    "REQUIRED_SWITCHES",
    "RelaxationStep",
    "RuleSpec",
    "Ruleset",
    "Semantics",
    "get_ruleset",
    "get_semantics",
    "load_ruleset",
    "load_semantics",
    "parse_ruleset",
    "parse_semantics",
    "req_max_for",
]
