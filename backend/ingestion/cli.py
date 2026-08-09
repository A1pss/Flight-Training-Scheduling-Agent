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
from backend.ingestion.conflicts import BASELINE_WEEK
from backend.ingestion.gate import (
    ConflictResolution,
    baseline_answers,
    baseline_resolutions,
    format_questions,
    review,
)
from backend.ingestion.pipeline import commit, prepare, snapshot_manifest
from backend.ingestion.questions import QID_CYCLE_START, QuestionAnswer
from backend.ingestion.validate import BASELINE_ENTITY_COUNTS

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
    parser.add_argument(
        "--cycle-start",
        default="",
        metavar="YYYY-MM-DD",
        help=(
            "课程周期起点。**只在课目文件里没有「课程开始日期」列时才需要**；"
            "文件里有就以文件为准。两者都没有时管线会把问题打印出来并以退出码 3 停下。"
        ),
    )
    parser.add_argument(
        "--resolve",
        action="append",
        default=[],
        metavar="CONFLICT_ID=VALUE",
        help=(
            "对某条冲突给出裁决，可重复。例如 "
            "--resolve P04:C:expiry=2026-01-07。裁决值必须是冲突两侧取值之一，"
            "且与 §5.5 裁定表一致（否则门禁拒绝）。"
        ),
    )
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
        # `--baseline` 才开基准回归护栏（规模核对 + X4 发布日期比对）。
        # 用户上传路径**不设**这两项：有多少人、多少飞机是用户的事；
        # 排班周由排班请求给出，摄取时无从得知（v6 §1.3 是基准数据的描述，
        # 不是系统上限）。
        prepared = prepare(
            paths,
            session=session,
            expected_counts=BASELINE_ENTITY_COUNTS if args.baseline else None,
            reference_period=BASELINE_WEEK if args.baseline else None,
        )
        manifest = snapshot_manifest(prepared)

        if args.dry_run:
            _emit(manifest, as_json=args.json)
            return 0

        approver = args.approver or (BASELINE_APPROVER if args.baseline else "")
        resolutions = (
            baseline_resolutions(prepared.changeset, decided_by=approver) if args.baseline else {}
        )
        for item in args.resolve:
            conflict_id, sep, value = item.partition("=")
            if not sep or not conflict_id or not value:
                parser.error(f"--resolve 需要 CONFLICT_ID=VALUE 形式，收到 {item!r}")
            resolutions[conflict_id] = ConflictResolution(
                conflict_id=conflict_id, chosen_value=value, decided_by=approver or "cli"
            )

        # 问题的答案来源：命令行 `--cycle-start`（= 对话里说了）优先，
        # 其次是基准数据集的既有裁决；都没有就让门禁把问题抛回给用户。
        answers: dict[str, QuestionAnswer] = (
            baseline_answers(prepared.changeset) if args.baseline else {}
        )
        if args.cycle_start:
            answers[QID_CYCLE_START] = QuestionAnswer(
                question_id=QID_CYCLE_START,
                value=args.cycle_start,
                answered_by=approver or "cli",
                source="prompt",
                note="由 --cycle-start 提供",
            )

        decision = review(prepared.changeset, resolutions, answers=answers, approver=approver)

        if not decision.approved:
            # 只是「有问题没答」→ 把问题原样打印给用户，这不是报错，是提问
            if decision.pending_questions:
                logger.info("需要用户补充后才能继续", count=len(decision.pending_questions))
                print(format_questions(decision.pending_questions))
                if any(q.resolution == "upload" for q in decision.pending_questions):
                    print("\n请补齐上述文件后重新上传。")
                else:
                    print(
                        "\n请用 --cycle-start YYYY-MM-DD 回答，"
                        "或在课目文件里补上「课程开始日期」列后重新上传。"
                    )
                return 3
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
