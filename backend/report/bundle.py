"""四表输出的**唯一输入对象**（v6 §10）。

## 为什么要有这么一个 bundle

Sheet 4 的七个区块里，只有区块 4（阻塞项）与区块 7（跑道空域明细）能从
`SchedulePlan` 一个对象里写出来。其余五个区块要的东西散在四处：

| 区块 | 数据来自 |
|---|---|
| 1 计划元信息 | `SchedulePlan` + `SolverStats`（状态/耗时/目标值/gap/worker/seed） |
| 2 约束校验结果 | `ValidationReport`（14 条）+ `FormatCheckReport`（格式前两层） |
| 3 训练进度与欠账 | `ValidationContext.progress` + `SchedulePlan.debts` |
| 5 资源利用 | `ValidationContext` 的飞机/人员/空域/跑道事实 |
| 6 松弛与决策记录 | `ValidationReport.all_notes()` + 松弛与审批信息 |

把它们攒成一个冻结的 dataclass，渲染层就只有「摆数据」没有「找数据」，
`manifest.py` / `naming.py` / `archive.py` 也共用同一个入口。

## 一条硬规矩：**这里不生产数据，只搬运数据**

铁律 6（不报告未实际计算的指标）在本模块的落点是 —— 每个字段都必须由调用方
从真实运行里填进来。没有的东西写 `None`，由渲染层显示成 `—`，**不许现编**：

- `solver_log` 为空 → 归档的 `solver_log.txt` 如实写「本次求解未采集 CP-SAT 日志」，
  不伪造一段日志；
- `prompt_versions` / `skill_version` 在 M3 时点还没有实体（M4 才有 Harness），
  → manifest 里如实写 `null`，不编一个 `v1`。

唯一一处「算」出来的是 :func:`ReportBundle.content_fingerprint` 的短指纹，
它只是 `plan.content_sha256` 的前 8 位（§10.6 的 `HASH8`），不是新算的哈希。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from backend.core.config import get_settings
from backend.schemas.plan import SchedulePlan
from backend.schemas.solver import SolverStats
from backend.schemas.validation import ValidationReport
from backend.validator.context import ValidationContext
from backend.validator.schema import FormatCheckReport

#: §10.6 的 `TYPE` 段
PlanType = Literal["WEEKLY", "RESCHED", "DRAFT", "SIM"]
#: §10.6 的 `STATUS` 段
PlanStatus = Literal["DRAFT", "PENDING", "APPROVED", "SUPERSEDED"]

#: 内容指纹在文件名里只取前 8 位（§10.6 `HASH8`）
HASH8_LENGTH = 8


@dataclass(frozen=True)
class ApprovalInfo:
    """人工门禁的审批信息（v6 §10.4 区块6 末行）。

    没批之前就是没批 —— 两个字段都为 None 时区块6 显示 `—`，不填「系统」之类的占位人。
    """

    approver: str | None = None
    approved_at: datetime | None = None


@dataclass(frozen=True)
class RelaxationRecord:
    """区块6 的一条松弛记录。Tier 0 时整个列表为空。"""

    tier: int
    action: str
    cost: str
    authority: str


@dataclass(frozen=True)
class ProvenanceInfo:
    """manifest 的可复现性字段里，**不由本窗口产生**的那几项（v6 §10.6）。

    `llm_*` 从配置读（真实值，随 `.env` 走）；`prompt_versions` / `skill_version`
    要等 M4 的 Harness 才有实体，缺省为空 → manifest 写 `null`。
    """

    code_version: str | None = None
    #: CP-SAT 版本。由调用方从 `ortools` 取真实值 —— `backend/report/` 不 import solver，
    #: 也不该为了一个版本号把 OR-Tools 拖进报告层的依赖里
    solver_version: str | None = None
    prompt_versions: Mapping[str, str] = field(default_factory=dict)
    skill_version: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_digest: str | None = None
    cuda_visible_devices: str | None = None

    @classmethod
    def from_settings(
        cls, *, code_version: str | None = None, solver_version: str | None = None
    ) -> ProvenanceInfo:
        """LLM 三态与 GPU 绑定从 `Settings` 取真实值（v6 §11.2）。

        `solver_version` 仍由调用方给 —— 报告层不 import 求解器，也不去猜它装的是哪版。
        """
        s = get_settings()
        return cls(
            code_version=code_version,
            solver_version=solver_version,
            llm_provider=s.LLM_PROVIDER,
            llm_model=s.LLM_MODEL,
            llm_digest=s.LLM_MODEL_DIGEST or None,
            cuda_visible_devices=s.CUDA_VISIBLE_DEVICES,
        )


@dataclass(frozen=True)
class ReportBundle:
    """渲染 / 回读 / manifest / 归档 四件事共用的输入。"""

    plan: SchedulePlan
    ctx: ValidationContext
    validation: ValidationReport
    stats: SolverStats
    generated_at: datetime
    format_report: FormatCheckReport | None = None
    plan_type: PlanType = "WEEKLY"
    plan_status: PlanStatus = "DRAFT"
    org: str = "NAU"
    relaxations: Sequence[RelaxationRecord] = ()
    conflict_summary: str | None = None
    approval: ApprovalInfo = field(default_factory=ApprovalInfo)
    provenance: ProvenanceInfo = field(default_factory=ProvenanceInfo)
    solver_log: str = ""

    def __post_init__(self) -> None:
        if self.plan.week_start != self.ctx.week_start:
            raise ValueError(
                f"方案的周起点 {self.plan.week_start} 与校验上下文的 "
                f"{self.ctx.week_start} 不一致，报告会摆出错位的事实"
            )

    @property
    def content_fingerprint(self) -> str:
        """§10.6 的 `HASH8`：内容指纹前 8 位。"""
        return self.plan.content_sha256[:HASH8_LENGTH]

    @property
    def s11_enabled(self) -> bool:
        """S-11 开关。为 on 时区块6 的「授权改写声明」是强制项（§10.4 / R17）。"""
        return bool(self.ctx.semantics.s11_enabled)


__all__ = [
    "HASH8_LENGTH",
    "ApprovalInfo",
    "PlanStatus",
    "PlanType",
    "ProvenanceInfo",
    "RelaxationRecord",
    "ReportBundle",
]
