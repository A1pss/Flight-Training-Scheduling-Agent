"""文件命名与版本分配（v6 §10.6）。

```
FTP_{ORG}_{TYPE}_{ISOWEEK}_{START}-{END}_v{N}_{STATUS}_{HASH8}.xlsx
FTP_NAU_WEEKLY_2026W02_20260105-20260111_v3_APPROVED_7f3a9c21.xlsx
```

## 「版本号永不复用」怎么保证

只数目录里现存的文件是不够的 —— 删掉 `v3` 再导一次就又发一个 `v3`，而
`v3` 这个号可能已经打印出来发给塔台了。所以本模块在周目录下维护一份
**只增不改的台账** `versions.json`：每次分配写一条（版本号、`plan_id`、
内容指纹、分配时间、文件名），下一个号取「台账最大值」与「目录里现存文件
最大值」两者的更大者 +1。**文件被删除也不会让号回收。**

台账是排序稳定的 JSON（`indent=2` + `ensure_ascii=False`），可以直接进 Git
做审计线索，也方便人肉查「v4 是哪天谁导的」。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.core.config import get_settings
from backend.report.bundle import PlanStatus, PlanType, ReportBundle

#: 归档目录下的版本台账文件名
LEDGER_NAME = "versions.json"

FILENAME_TEMPLATE = "FTP_{org}_{plan_type}_{iso_week}_{start}-{end}_v{version}_{status}_{hash8}"

NAME_RE = re.compile(
    r"^FTP_(?P<org>[A-Z0-9]+)_(?P<plan_type>WEEKLY|RESCHED|DRAFT|SIM)_"
    r"(?P<iso_week>\d{4}W\d{2})_(?P<start>\d{8})-(?P<end>\d{8})_"
    r"v(?P<version>\d+)_(?P<status>DRAFT|PENDING|APPROVED|SUPERSEDED)_"
    r"(?P<hash8>[0-9a-f]{8})$"
)

_ISO_WEEK_RE = re.compile(r"^(?P<year>\d{4})W(?P<week>\d{2})$")


@dataclass(frozen=True)
class PlanName:
    """一个已分配的文件名。`stem` 是四件套里三件的共同前缀。"""

    org: str
    plan_type: PlanType
    iso_week: str
    week_start: str
    week_end: str
    version: int
    status: PlanStatus
    hash8: str

    @property
    def stem(self) -> str:
        return FILENAME_TEMPLATE.format(
            org=self.org,
            plan_type=self.plan_type,
            iso_week=self.iso_week,
            start=self.week_start,
            end=self.week_end,
            version=self.version,
            status=self.status,
            hash8=self.hash8,
        )

    @property
    def xlsx(self) -> str:
        return f"{self.stem}.xlsx"

    @property
    def json(self) -> str:
        return f"{self.stem}.json"

    @property
    def manifest(self) -> str:
        return f"{self.stem}.manifest.yaml"

    @property
    def validation_report(self) -> str:
        return f"validation_report_v{self.version}.json"

    @property
    def solver_log(self) -> str:
        return f"solver_log_v{self.version}.txt"


def parse_name(stem: str) -> PlanName:
    """反解文件名。传入的可以带扩展名，也可以只有主干。"""
    core = stem
    for suffix in (".manifest.yaml", ".xlsx", ".json"):
        if core.endswith(suffix):
            core = core[: -len(suffix)]
            break
    m = NAME_RE.match(core)
    if not m:
        raise ValueError(f"文件名不合 v6 §10.6 命名规范：{stem!r}")
    return PlanName(
        org=m.group("org"),
        plan_type=m.group("plan_type"),  # type: ignore[arg-type]
        iso_week=m.group("iso_week"),
        week_start=m.group("start"),
        week_end=m.group("end"),
        version=int(m.group("version")),
        status=m.group("status"),  # type: ignore[arg-type]
        hash8=m.group("hash8"),
    )


def week_dir(iso_week: str, *, root: Path | None = None) -> Path:
    """`2026W02` → `data/plans/2026/W02`（v6 §10.6 归档结构）。"""
    m = _ISO_WEEK_RE.match(iso_week)
    if not m:
        raise ValueError(f"ISO 周格式非法：{iso_week!r}（应为 YYYY'W'WW）")
    base = root if root is not None else get_settings().PLANS_DIR
    return base / m.group("year") / f"W{m.group('week')}"


@dataclass(frozen=True)
class LedgerEntry:
    version: int
    plan_id: str
    content_sha256: str
    allocated_at: str
    filename: str


def read_ledger(directory: Path) -> tuple[LedgerEntry, ...]:
    path = directory / LEDGER_NAME
    if not path.exists():
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        LedgerEntry(
            version=int(item["version"]),
            plan_id=str(item["plan_id"]),
            content_sha256=str(item["content_sha256"]),
            allocated_at=str(item["allocated_at"]),
            filename=str(item["filename"]),
        )
        for item in raw
    )


def _write_ledger(directory: Path, entries: Sequence[LedgerEntry]) -> None:
    payload = [
        {
            "version": e.version,
            "plan_id": e.plan_id,
            "content_sha256": e.content_sha256,
            "allocated_at": e.allocated_at,
            "filename": e.filename,
        }
        for e in sorted(entries, key=lambda e: e.version)
    ]
    (directory / LEDGER_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def existing_versions(directory: Path) -> tuple[int, ...]:
    """目录里现存 xlsx 的版本号（台账丢失时的兜底来源）。"""
    if not directory.exists():
        return ()
    versions: list[int] = []
    for path in sorted(directory.glob("*.xlsx")):
        try:
            versions.append(parse_name(path.name).version)
        except ValueError:
            continue  # 目录里的无关文件不参与分配，也不报错
    return tuple(sorted(versions))


def next_version(directory: Path) -> int:
    """下一个可用版本号 = max(台账, 现存文件) + 1，从 1 起。"""
    ledger = [e.version for e in read_ledger(directory)]
    return max([*ledger, *existing_versions(directory), 0]) + 1


def allocate_name(bundle: ReportBundle, *, root: Path | None = None, now: datetime) -> PlanName:
    """分配一个**新**版本号并落台账。同周内递增，**永不复用**。"""
    directory = week_dir(bundle.plan.iso_week, root=root)
    directory.mkdir(parents=True, exist_ok=True)
    name = PlanName(
        org=bundle.org,
        plan_type=bundle.plan_type,
        iso_week=bundle.plan.iso_week,
        week_start=bundle.plan.week_start.strftime("%Y%m%d"),
        week_end=bundle.plan.week_end.strftime("%Y%m%d"),
        version=next_version(directory),
        status=bundle.plan_status,
        hash8=bundle.content_fingerprint,
    )
    entries = [
        *read_ledger(directory),
        LedgerEntry(
            version=name.version,
            plan_id=bundle.plan.plan_id,
            content_sha256=bundle.plan.content_sha256,
            allocated_at=now.isoformat(),
            filename=name.xlsx,
        ),
    ]
    _write_ledger(directory, entries)
    return name


__all__ = [
    "FILENAME_TEMPLATE",
    "LEDGER_NAME",
    "NAME_RE",
    "LedgerEntry",
    "PlanName",
    "allocate_name",
    "existing_versions",
    "next_version",
    "parse_name",
    "read_ledger",
    "week_dir",
]
