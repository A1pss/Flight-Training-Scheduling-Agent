"""摄取 CLI —— 把 `data/origin/` 的四份 PDF 跑成基准 snapshot。

```bash
conda run -n schedule python -m backend.ingestion.cli --baseline
conda run -n schedule python -m backend.ingestion.cli --baseline --dry-run
```

`--baseline` 用 §5.5 裁定表为已裁定的冲突自动生成裁决（`baseline_resolutions`），
批准人记为 `baseline(v6 §5.5 裁定表)` 并进审计日志。**这不是绕过人工门禁**：
门禁的判定逻辑照样全跑一遍，只是把「按裁定选 2026-01-07」这个动作从人手里
换成了那张版本化的裁定表 —— 因为基准快照必须能非交互地逐字节重跑（铁律 9）。
任何**未裁定**的冲突都不会被自动放行，门禁会拒绝并列出待裁项。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from backend.core.config import PROJECT_ROOT, get_settings
from backend.core.db import session_scope
from backend.core.logging import configure_logging, get_logger
from backend.ingestion.gate import baseline_resolutions, review
from backend.ingestion.pipeline import commit, prepare, snapshot_manifest

logger = get_logger(__name__)

#: 基准四份 PDF
BASELINE_SOURCES = (
    "personnel.pdf",
    "aircraft.pdf",
    "missions.pdf",
    "rules.pdf",
)

BASELINE_APPROVER = "baseline(v6 §5.5 裁定表)"


def _ruleset_version() -> str:
    doc = yaml.safe_load(get_settings().RULESET_PATH.read_text(encoding="utf-8"))
    return str(doc["ruleset_version"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FTS 文档摄取管线")
    parser.add_argument("paths", nargs="*", type=Path, help="要摄取的文件；与 --baseline 二选一")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="摄取 data/origin/ 的四份基准 PDF，并按 §5.5 裁定表自动裁决已裁定冲突",
    )
    parser.add_argument("--dry-run", action="store_true", help="只跑到 Diff，不落库")
    parser.add_argument("--no-vectors", action="store_true", help="跳过 Chroma 写入")
    parser.add_argument("--approver", default="", help="人工确认的批准人")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    configure_logging()

    if args.baseline:
        origin = PROJECT_ROOT / "data" / "origin"
        paths = [origin / name for name in BASELINE_SOURCES]
    elif args.paths:
        paths = list(args.paths)
    else:
        parser.error("需要给出文件路径，或使用 --baseline")

    with session_scope() as session:
        prepared = prepare(paths, session=session)
        manifest = snapshot_manifest(prepared)

        if args.dry_run:
            _emit(manifest, as_json=args.json)
            return 0

        approver = args.approver or (BASELINE_APPROVER if args.baseline else "")
        resolutions = (
            baseline_resolutions(prepared.changeset, decided_by=approver) if args.baseline else {}
        )
        decision = review(prepared.changeset, resolutions, approver=approver)

        if not decision.approved:
            logger.error("人工确认门禁未通过", reasons=decision.reasons)
            _emit(
                {**manifest, "gate": {"outcome": decision.outcome, "reasons": decision.reasons}},
                as_json=args.json,
            )
            return 2

        result = commit(
            prepared,
            decision,
            session,
            ruleset_version=_ruleset_version(),
            write_vectors=not args.no_vectors,
        )
        _emit(
            {
                **manifest,
                "committed_snapshot_id": result.snapshot_id,
                "table_counts": result.table_counts,
                "vector_counts": result.vector_counts,
                "applied_resolutions": result.applied_resolutions,
            },
            as_json=args.json,
        )
    return 0


def _emit(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for key, value in payload.items():
        print(f"{key}: {json.dumps(value, ensure_ascii=False)}")


if __name__ == "__main__":
    sys.exit(main())
