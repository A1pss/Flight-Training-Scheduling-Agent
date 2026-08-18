"""摄取的三个端点（v6 §9.1 / §5.1）。

```
POST /ingest            ── 存盘 + 登记，返回 ingest_job_id（幂等键 = 文件 SHA256）
GET  /ingest/{id}/changeset ── 抽取 → 校验 → Diff，返回待确认变更（含 §5.5 冲突）
POST /ingest/{id}/confirm   ── 人工裁决 → 落库 → 新 snapshot
```

## 为什么 changeset 每次都重新抽一遍

`prepare()` 的产物是一堆 Python 对象（`IngestedFacts` + `ChangeSet`），不能直接
塞进 Redis，而进程内缓存在多 uvicorn worker 下是错的（第二个请求可能落到另一个
进程，那里没有缓存）。**上传的文件本身就是唯一的真相**：存盘，每次从盘上重抽。

代价是同一批文件解析两次（`changeset` 一次、`confirm` 一次），基准四份 PDF
实测约数秒。收益是整条链路**无状态**：任何一个 worker 都能接任何一个请求，
进程重启也不丢。而且这两次解析必然得到同一结果——`prepare` 是纯函数
（铁律 9 的同一条要求），否则 `content_sha256` 这套东西根本不成立。

## 确认这一步绝不替用户做决定

`review()` 已经把「问题没答完 / 冲突没裁完」挡在门外（`GateDecision.approved`
为假）。这里把它的 `reasons` 与 `pending_questions` 原样翻成 FTS-1004 交回去
——**不设默认值、不猜、不拿上一版快照顶替**（v6 §5.1.1、CLAUDE.md 反模式）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, UploadFile, status
from sqlalchemy.orm import Session

from backend.api.audit import CurrentAudit
from backend.api.deps import (
    CurrentClientIP,
    CurrentIdempotency,
    CurrentPrincipal,
    CurrentSession,
    CurrentSettings,
    CurrentTraceId,
)
from backend.api.idempotency import file_digest
from backend.api.security import require_role
from backend.api.store import KEY_PREFIX
from backend.core.config import Settings
from backend.core.errors import (
    DataConflictError,
    IngestionError,
    RequiredInputMissingError,
)
from backend.core.ruleset import load_ruleset
from backend.ingestion.conflicts import Conflict
from backend.ingestion.diff import ChangeSet
from backend.ingestion.gate import ConflictResolution, review
from backend.ingestion.pipeline import PreparedIngestion, commit, prepare
from backend.ingestion.questions import OpenQuestion, QuestionAnswer
from backend.schemas.api import (
    ChangeItemView,
    ChangeSetView,
    ConflictView,
    IngestConfirmRequest,
    IngestConfirmView,
    IngestSubmitView,
    OpenQuestionView,
)

router = APIRouter(tags=["摄取"])

#: 上传件的落盘位置。`data/uploads/{ingest_job_id}/原文件名`
UPLOAD_SUBDIR = "uploads"
INGEST_KEY_PREFIX = f"{KEY_PREFIX}:ingest"

#: 上传目录权限：属主可进可写，**组与其他人一律没有任何位**（v6 §11.5）。
DIR_MODE = 0o700
#: 上传文件权限：属主可读写，**任何人都没有执行位**。
FILE_MODE = 0o600


def harden_upload_dir(directory: Path) -> None:
    """把上传目录及其父目录收紧到不可执行（v6 §11.5「上传目录不可执行」）。

    目录的执行位（`x`）语义是「可以进入」，不是「可以运行里面的程序」，所以
    目录本身必须保留属主的 `x`；真正要掐掉的是**文件的执行位**与**其他用户的
    一切权限**。两件事在这里一起做：目录 0o700、文件 0o600。
    """
    directory.chmod(DIR_MODE)
    parent = directory.parent
    if parent.name == UPLOAD_SUBDIR:
        parent.chmod(DIR_MODE)


def upload_root(settings: Settings) -> Path:
    return settings.PLANS_DIR.parent / UPLOAD_SUBDIR


def ingest_job_id(digest: str) -> str:
    """`ing_` + 摘要前 24 位。**id 由内容决定**，所以同一份文件必得同一个 id。"""
    return f"ing_{digest[:24]}"


def _staging_dir(settings: Settings, job_id: str) -> Path:
    return upload_root(settings) / job_id


def _manifest_path(settings: Settings, job_id: str) -> Path:
    return _staging_dir(settings, job_id) / "_upload.json"


def _load_manifest(settings: Settings, job_id: str) -> dict[str, Any]:
    path = _manifest_path(settings, job_id)
    if not path.exists():
        raise RequiredInputMissingError(
            f"找不到上传批次 {job_id}",
            details={"ingest_job_id": job_id, "resolution": "upload"},
            suggestions=["先 POST /api/v1/ingest 上传文件"],
        )
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _prepared(settings: Settings, session: Session, job_id: str) -> PreparedIngestion:
    """从盘上的那批文件重跑 `prepare()`。见模块注释。"""
    manifest = _load_manifest(settings, job_id)
    paths = [Path(p) for p in manifest["paths"]]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise RequiredInputMissingError(
            f"上传批次 {job_id} 的文件已不在磁盘上",
            details={"missing": missing, "resolution": "upload"},
            suggestions=["重新上传这批文件"],
        )
    # 用户上传路径**不设**基准回归护栏（规模核对 / 发布日期比对）——
    # 有多少人、多少飞机是用户的事（v6 §1.3 告警框、§5.1.1）
    return prepare(paths, session=session)


def _conflict_view(conflict: Conflict) -> ConflictView:
    return ConflictView(
        conflict_id=conflict.conflict_id,
        kind=conflict.kind,
        subject=conflict.message,
        options={conflict.value_a: conflict.source_a, conflict.value_b: conflict.source_b},
        adjudication=conflict.adjudicated_value,
        blocking=conflict.requires_human_gate,
    )


def _question_view(question: OpenQuestion) -> OpenQuestionView:
    return OpenQuestionView(
        question_id=question.question_id,
        prompt=question.question,
        resolution=question.resolution,
        detail=question.why_it_matters,
    )


def _changeset_view(job_id: str, changeset: ChangeSet, *, extraction_ok: bool) -> ChangeSetView:
    return ChangeSetView(
        ingest_job_id=job_id,
        base_snapshot_id=changeset.base_snapshot_id,
        summary=changeset.summary(),
        changes=[
            ChangeItemView(
                kind={"ADDED": "added", "MODIFIED": "modified", "REMOVED": "removed"}[c.kind],  # type: ignore[arg-type]
                table=c.entity_type,
                key=c.entity_id,
                fields={
                    "changed": list(c.changed_fields),
                    "before": c.before,
                    "after": c.after,
                },
            )
            for c in changeset.changes
        ],
        conflicts=[_conflict_view(c) for c in changeset.conflicts],
        open_questions=[_question_view(q) for q in changeset.questions],
        extraction_ok=extraction_ok,
    )


@router.post(
    "/ingest",
    response_model=IngestSubmitView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传数据文件（幂等键 = 文件 SHA256）",
)
def post_ingest(
    principal: CurrentPrincipal,
    settings: CurrentSettings,
    idempotency: CurrentIdempotency,
    trace_id: CurrentTraceId,
    files: Annotated[list[UploadFile], File(description="人员/飞机/课目/规则等文件")],
    audit: CurrentAudit,
) -> IngestSubmitView:
    """存盘 + 登记。**不在这里解析**——解析在 `/changeset`。

    多文件时的幂等键是「每份文件摘要按文件名排序后再摘要」：一批文件的身份
    应当与上传顺序无关，而 `sha256(a||b) != sha256(b||a)`。
    """
    require_role(principal, "scheduler", action="上传数据文件")
    if not files:
        raise RequiredInputMissingError(
            "没有收到任何文件",
            details={"resolution": "upload"},
            suggestions=["至少上传一份数据文件"],
        )

    payloads: list[tuple[str, bytes]] = []
    for item in files:
        data = item.file.read()
        if len(data) > settings.UPLOAD_MAX_BYTES:
            raise IngestionError(
                f"{item.filename} 超过单文件上限 {settings.UPLOAD_MAX_BYTES} 字节",
                details={"filename": item.filename, "size": len(data)},
            )
        if not data:
            raise IngestionError(
                f"{item.filename} 是空文件，拒绝入库（铁律 7：不静默降级）",
                details={"filename": item.filename},
            )
        payloads.append((Path(item.filename or "unnamed").name, data))

    payloads.sort(key=lambda pair: pair[0])
    digest = file_digest(
        "".join(f"{name}:{file_digest(data)}" for name, data in payloads).encode("utf-8")
    )
    job_id = ingest_job_id(digest)

    cached = idempotency.lookup("ingest", settings.TENANT_ID, digest)
    if cached is not None:
        return IngestSubmitView.model_validate({**cached, "idempotent_hit": True})

    directory = _staging_dir(settings, job_id)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    harden_upload_dir(directory)
    paths: list[str] = []
    for name, data in payloads:
        target = directory / name
        target.write_bytes(data)
        # ★ 上传目录不可执行（v6 §11.5「文件上传」最后一句）。0o600 = 只有属主
        # 可读写、**任何人都不可执行**。上传的东西是数据，不是程序 —— 万一有一天
        # 这个目录被某个 Web 服务器映射出去，缺这一行就是「传个 .php 上去就能跑」。
        target.chmod(FILE_MODE)
        paths.append(str(target))
    _manifest_path(settings, job_id).write_text(
        json.dumps(
            {
                "ingest_job_id": job_id,
                "content_sha256": digest,
                "paths": paths,
                "uploaded_by": principal.user_id,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    view = IngestSubmitView(
        ingest_job_id=job_id,
        content_sha256=digest,
        idempotent_hit=False,
        filenames=[name for name, _ in payloads],
        trace_id=trace_id,
    )
    idempotency.remember("ingest", settings.TENANT_ID, digest, view.model_dump(mode="json"))
    audit.record(
        action="api.ingest.submit",
        resource_type="ingest_job",
        resource_id=job_id,
        after={
            "content_sha256": digest,
            "filenames": [name for name, _ in payloads],
            "total_bytes": sum(len(data) for _, data in payloads),
            "staging_dir": str(directory),
        },
    )
    return view


@router.get(
    "/ingest/{job_id}/changeset",
    response_model=ChangeSetView,
    summary="取待确认 Diff（含 §5.5 的冲突项与待答问题）",
)
def get_changeset(
    job_id: str,
    principal: CurrentPrincipal,
    session: CurrentSession,
    settings: CurrentSettings,
) -> ChangeSetView:
    require_role(principal, "viewer", action="查看摄取变更集")
    prepared = _prepared(settings, session, job_id)
    # 能走到这里就说明抽取期的后置断言过了 —— 不过的话 `prepare()` 早就抛
    # `IngestionError`（FTS-1003）阻断了，绝不会「尽力而为」地返回半份结果（铁律 7）
    return _changeset_view(job_id, prepared.changeset, extraction_ok=True)


@router.post(
    "/ingest/{job_id}/confirm",
    response_model=IngestConfirmView,
    summary="确认变更 → 落库 → 生成新 snapshot",
)
def post_confirm(
    job_id: str,
    body: IngestConfirmRequest,
    principal: CurrentPrincipal,
    session: CurrentSession,
    settings: CurrentSettings,
    idempotency: CurrentIdempotency,
    audit: CurrentAudit,
    trace_id: CurrentTraceId,
    client_ip: CurrentClientIP,
) -> IngestConfirmView:
    require_role(principal, "scheduler", action="确认数据入库")
    cached = idempotency.lookup("ingest_confirm", settings.TENANT_ID, job_id)
    if cached is not None:
        return IngestConfirmView.model_validate({**cached, "idempotent_hit": True})

    prepared = _prepared(settings, session, job_id)
    resolutions = {
        conflict_id: ConflictResolution(
            conflict_id=conflict_id, chosen_value=value, decided_by=body.approver
        )
        for conflict_id, value in body.resolutions.items()
    }
    answers = {
        question_id: QuestionAnswer(
            question_id=question_id,
            value=value,
            answered_by=body.approver,
            source="ui",
            note=body.comment,
        )
        for question_id, value in body.answers.items()
    }
    decision = review(prepared.changeset, resolutions, answers=answers, approver=body.approver)

    if not decision.approved:
        if decision.pending_questions:
            raise RequiredInputMissingError(
                "还有必须由人回答的问题，未确认",
                details={
                    "questions": [
                        _question_view(q).model_dump(mode="json")
                        for q in decision.pending_questions
                    ],
                    "reasons": decision.reasons,
                },
                suggestions=[
                    "resolution=answer 的给一个值即可；resolution=upload 的必须补传整份文件"
                ],
            )
        raise DataConflictError(
            "人工确认门禁未通过：" + "；".join(decision.reasons),
            details={
                "reasons": decision.reasons,
                "conflicts": [
                    _conflict_view(c).model_dump(mode="json")
                    for c in prepared.changeset.blocking_conflicts
                ],
            },
            suggestions=["按 v6 §5.5 裁定表逐条裁决后重试"],
        )

    base_snapshot_id = prepared.base_snapshot_id
    result = commit(
        prepared,
        decision,
        session,
        ruleset_version=load_ruleset(settings.RULESET_PATH).version,
        trace_id=trace_id,
        actor_ip=client_ip,
    )
    session.commit()
    # 这一行与 `pipeline.commit()` 里那行 `ingest.commit` **不重复**：那一行记的是
    # 快照 A → B 的数据变更（与建快照同一个事务成败与共），这一行记的是「谁从
    # 哪台机器调了这个端点」。查审计时前者回答「数据怎么变的」，后者回答「谁动的手」。
    audit.record(
        action="api.ingest.confirm",
        resource_type="data_snapshot",
        resource_id=result.snapshot_id,
        before={"snapshot_id": base_snapshot_id, "ingest_job_id": job_id},
        after={
            "snapshot_id": result.snapshot_id,
            "ingest_job_id": job_id,
            "approver": body.approver,
            "resolutions": dict(body.resolutions),
            "answers": dict(body.answers),
        },
    )
    view = IngestConfirmView(
        snapshot_id=result.snapshot_id,
        table_counts=result.table_counts,
        vector_counts=result.vector_counts,
        applied_resolutions=result.applied_resolutions,
        idempotent_hit=False,
    )
    idempotency.remember("ingest_confirm", settings.TENANT_ID, job_id, view.model_dump(mode="json"))
    return view


__all__ = ["ingest_job_id", "router", "upload_root"]
