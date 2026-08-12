"""可复现性清单 `manifest.yaml`（v6 §10.6）。

## 两个 v6 新增字段是硬性的

- **`semantics_switches`**：S-01~S-13 的取值快照。同一份数据在不同语义解读下
  排出的两个方案是**两个不同的计划版本**（它参与 `content_sha256`，附录 B 脚注）。
  没有这一项，「为什么上周和这周排得不一样」就查不出来 —— 可能是数据变了，
  也可能是有人把 S-02 从 `class_level` 改成了逐课目。
- **`solver.num_search_workers`**：CP-SAT 多线程搜索在不同 worker 数下可能返回
  **不同的等价最优解**（v6 §3.11.1）。少了它，`seed=42` 也复现不出同一份文件。

## 没有的东西写 `null`，不编

`prompt_versions` / `skill_version` 要等 M4 的 Harness 才有实体。M3 时点如实写
`null`，**不写一个 `v1` 冒充**（铁律 6）。`llm` 段从 `Settings` 读真实值 ——
在 `LLM_PROVIDER=mock` 下跑出来就写 mock，这本身就是复现所需的信息。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from backend.report.bundle import ReportBundle
from backend.report.naming import PlanName

#: CP-SAT 的实现名。版本号由调用方从 `ortools` 取真实值（本包不 import solver）
SOLVER_NAME = "cp-sat"


def _solver_section(bundle: ReportBundle, solver_version: str | None) -> dict[str, Any]:
    stats = bundle.stats
    return {
        "name": SOLVER_NAME,
        "version": solver_version,
        "seed": stats.random_seed,
        # ★ v6 新增字段：可复现性的必要条件（§3.11.1）
        "num_search_workers": stats.num_workers,
        "status": stats.status,
        "objective": stats.objective_value,
        "best_bound": stats.best_bound,
        "gap": stats.gap,
        "wall_time_s": round(stats.wall_time_ms / 1000.0, 3),
        "num_candidates": stats.num_candidates,
        "num_variables": stats.num_variables,
        "num_constraints": stats.num_constraints,
    }


def build_manifest(
    bundle: ReportBundle, name: PlanName, *, solver_version: str | None = None
) -> dict[str, Any]:
    """按 v6 §10.6 的字段清单装配 manifest（纯函数，便于逐字断言）。"""
    plan = bundle.plan
    prov = bundle.provenance
    approval = bundle.approval
    solver_version = solver_version or prov.solver_version
    return {
        "plan_id": plan.plan_id,
        "plan_file": name.xlsx,
        "iso_week": plan.iso_week,
        "week_start": plan.week_start.isoformat(),
        "week_end": plan.week_end.isoformat(),
        "plan_type": bundle.plan_type,
        "status": bundle.plan_status,
        "version": name.version,
        "snapshot_id": plan.snapshot_id,
        "ruleset_version": plan.ruleset_version,
        "semantics_version": plan.semantics_version,
        # ★ v6 新增字段：语义开关快照，逐条记录 S-01~S-13
        "semantics_switches": dict(sorted(plan.semantics_switches.items())),
        "runway_model": plan.runway_model,
        "relaxation_tier": plan.relaxation_tier,
        "solver": _solver_section(bundle, solver_version),
        "llm": {
            "provider": prov.llm_provider,
            "model": prov.llm_model,
            "digest": prov.llm_digest,
            "cuda_visible_devices": prov.cuda_visible_devices,
        },
        "prompt_versions": dict(sorted(prov.prompt_versions.items())) or None,
        "skill_version": prov.skill_version,
        "code_version": prov.code_version,
        "generated_at": bundle.generated_at.isoformat(),
        "approved_by": approval.approver,
        "approved_at": approval.approved_at.isoformat() if approval.approved_at else None,
        "content_sha256": plan.content_sha256,
    }


def dump_manifest(manifest: Mapping[str, Any]) -> str:
    """YAML 文本。`sort_keys=False` 保住 §10.6 的字段顺序，便于人肉对照。"""
    return yaml.safe_dump(
        dict(manifest), allow_unicode=True, sort_keys=False, default_flow_style=False
    )


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_manifest(manifest), encoding="utf-8")
    return path


def load_manifest(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是一个 manifest 映射")
    return data


#: 复现一次求解所必需的字段。缺任意一条，「同 snapshot + 同 ruleset + 同 semantics
#: + seed=42 → 逐字节可复现」（铁律 9）就无从谈起。
REPRODUCIBILITY_KEYS: tuple[str, ...] = (
    "plan_id",
    "snapshot_id",
    "ruleset_version",
    "semantics_version",
    "semantics_switches",
    "relaxation_tier",
    "solver",
    "content_sha256",
)

#: `solver` 段里必须齐全的子字段
SOLVER_KEYS: tuple[str, ...] = ("name", "seed", "num_search_workers", "status")


def missing_reproducibility_fields(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """返回缺失或为空的可复现性字段（空元组 = 齐全）。"""
    missing = [k for k in REPRODUCIBILITY_KEYS if manifest.get(k) in (None, "", {}, [])]
    solver = manifest.get("solver")
    if not isinstance(solver, Mapping):
        missing.append("solver")
    else:
        missing.extend(f"solver.{k}" for k in SOLVER_KEYS if solver.get(k) in (None, ""))
    return tuple(sorted(set(missing)))


__all__ = [
    "REPRODUCIBILITY_KEYS",
    "SOLVER_KEYS",
    "SOLVER_NAME",
    "build_manifest",
    "dump_manifest",
    "load_manifest",
    "missing_reproducibility_fields",
    "write_manifest",
]
