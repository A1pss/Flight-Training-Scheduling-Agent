"""校验契约（v6 §4.2 / §4.3）。

`CheckResult.checked_items` 是**刻意设计**：前端展示「约束7 ✅ 已检查 47 项」
比单纯打勾更有说服力，也能发现「检查了 0 项」这种假通过（v6 §4.2 脚注）。
本模块因此把 `checked_items` 设为必填且带下界校验。

注意本模块属于 `schemas/`，被 `validator/` 与 `solver/` 共同依赖——它是**数据
形状**的定义，不含任何约束表达逻辑，因此不违反 CLAUDE.md 铁律 2 的隔离要求。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

ViolationSeverity = Literal["HARD", "SOFT"]

#: 14 条规则的校验器编号（v6 §3.2）。C06 含空域容量，C09 含双跑道密度。
RULE_IDS: tuple[str, ...] = tuple(f"C{i:02d}" for i in range(1, 15))


class Violation(BaseModel):
    """单条违规。`fix_hint` 是给排班员看的可执行建议，不是给求解器的。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(pattern=r"^C(0[1-9]|1[0-4])$")
    severity: ViolationSeverity = "HARD"
    subjects: list[str] = Field(
        default_factory=list, description="涉及的实体：机号 / sortie_id / 跑道 / 空域"
    )
    detail: str = Field(min_length=1)
    fix_hint: str | None = None


class CheckResult(BaseModel):
    """单条规则的校验结果（v6 §4.2）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(pattern=r"^C(0[1-9]|1[0-4])$")
    rule_title: str = Field(min_length=1)
    passed: bool
    checked_items: int = Field(ge=0, description="检查了多少个对象；0 项需警惕假通过")
    violations: list[Violation] = Field(default_factory=list)
    duration_ms: float = Field(ge=0.0)
    #: ★ M2-B 新增（可选、有默认值，对既有调用方向后兼容）：**不是违规、但必须
    #: 出现在报告里的声明**。当前唯一的强制项是 S-11 的「业务方授权改写」
    #: （v6 §1.2.4 / §10.4 区块6）—— 只要 S-11 开关为 on，无论本周是否真排出
    #: 复训架次，这行声明都必须出现，否则评审者看到「刘斌到期后还在飞 C 类」
    #: 会当成校验器漏判（风险项 R17）。
    notes: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    """14 条逐条结果的汇总（v6 §7.4 `state.validation`）。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1)
    ruleset_version: str = Field(min_length=1)
    semantics_version: str = Field(min_length=1)
    results: list[CheckResult] = Field(default_factory=list)
    duration_ms: float = Field(ge=0.0, default=0.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def all_passed(self) -> bool:
        """14 条全部通过才算通过。空结果集不算通过——那是没跑，不是过了。"""
        return bool(self.results) and all(r.passed for r in self.results)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_checked_items(self) -> int:
        return sum(r.checked_items for r in self.results)

    def all_violations(self) -> list[Violation]:
        return [v for r in self.results for v in r.violations]

    def all_notes(self) -> list[str]:
        """报告级声明（当前为 S-11 授权改写）。Sheet 4 区块6 直接取它。"""
        return [n for r in self.results for n in r.notes]

    def missing_rules(self) -> list[str]:
        """未被校验的规则编号。非空即说明校验没跑全，不能宣称 100% 合规。"""
        seen = {r.rule_id for r in self.results}
        return [rid for rid in RULE_IDS if rid not in seen]


class SchemaCheckReport(BaseModel):
    """闸门 3：Excel 回读反解与源对象的深度相等断言（v6 §4.3）。

    `diff` 必须为空——写出的内容能被完整反解回原对象，格式就不可能错。
    """

    model_config = ConfigDict(extra="forbid")

    passed: bool
    diff: list[str] = Field(default_factory=list, description="深度比对的差异路径")
    workbook_path: str | None = None
    sheet_names: list[str] = Field(default_factory=list)


__all__ = [
    "RULE_IDS",
    "CheckResult",
    "SchemaCheckReport",
    "ValidationReport",
    "Violation",
    "ViolationSeverity",
]
