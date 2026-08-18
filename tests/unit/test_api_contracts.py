"""API 契约与任务状态机的单测（v6 §8.1 / §9.1 / §9.3）。

四件事：

1. **轮询响应体必须小**（§8.1「保持在几百字节」）——直接量字节数；
2. **六个阶段一个不多一个不少**，且每个图节点都能映射到某个阶段；
3. **`ErrorView` 与 `ErrorResponse` 字段一致**——两处定义漂了，前端就会拿不到
   某个字段而不报错；
4. **回放完整性的判据**：`seq` 恰好是 `0..n-1`。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from backend.api.jobs import NODE_STAGE, JobRecord, JobStore
from backend.api.store import InMemoryStore
from backend.core.errors import ErrorCode, ErrorResponse
from backend.graph.graph import NODE_NAMES
from backend.schemas.api import (
    STAGE_PERCENT,
    ErrorView,
    JobStage,
    JobStatus,
    JobStatusView,
    RunResultView,
)
from backend.schemas.common import TraceEvent


def _event(seq: int, agent: str = "solve") -> TraceEvent:
    return TraceEvent(seq=seq, ts=datetime.now(UTC), agent=agent, kind="decision", payload={})


def test_polling_payload_stays_a_few_hundred_bytes() -> None:
    """v6 §8.1：轮询 1.5 s 一次，响应体保持在几百字节。

    这条不是洁癖：把方案塞进轮询响应的话，一次排班等人确认的十分钟里，
    前端会把那份几百 KB 的方案重复拉 400 次。
    """
    view = JobStatusView(
        job_id="a" * 32,
        trace_id="b" * 32,
        status=JobStatus.RUNNING,
        stage=JobStage.SOLVING,
        percent=65,
        finished_at=datetime.now(UTC),
        error_code=ErrorCode.SOLVE_TIMEOUT_UNKNOWN,
    )
    payload = json.dumps(view.model_dump(mode="json"), ensure_ascii=False)
    assert len(payload.encode("utf-8")) < 400, payload


def test_six_stages_exactly() -> None:
    """§8.1 的六个粗粒度阶段。加第七个要先改设计文档。"""
    assert len(JobStage) == 6
    assert [s.value for s in JobStage] == [
        "解析意图",
        "加载数据",
        "建模",
        "求解",
        "校验",
        "生成报表",
    ]
    assert set(STAGE_PERCENT) == set(JobStage)


def test_every_graph_node_maps_to_a_stage() -> None:
    """漏一个节点 = 那个节点跑完时进度条不动，用户以为卡死了。"""
    assert set(NODE_STAGE) == set(NODE_NAMES)


def test_stage_percent_is_monotonic_along_the_pipeline() -> None:
    order = [
        JobStage.INTENT,
        JobStage.LOADING,
        JobStage.MODELING,
        JobStage.SOLVING,
        JobStage.VALIDATING,
        JobStage.REPORTING,
    ]
    percents = [STAGE_PERCENT[s] for s in order]
    assert percents == sorted(percents)
    assert all(0 <= p <= 100 for p in percents)


def test_error_view_matches_error_response_fields() -> None:
    """两处定义必须同形（一处是异常层，一处是 HTTP 契约）。"""
    assert set(ErrorView.model_fields) == set(ErrorResponse.model_fields)


def test_replay_complete_requires_contiguous_seq() -> None:
    run = RunResultView(
        trace_id="t1",
        status=JobStatus.DONE,
        stage=JobStage.REPORTING,
        trace_events=[_event(0), _event(1), _event(2)],
    )
    assert run.replay_complete() is True

    holed = RunResultView(
        trace_id="t1",
        status=JobStatus.DONE,
        stage=JobStage.REPORTING,
        trace_events=[_event(0), _event(2)],
    )
    assert holed.replay_complete() is False


def test_empty_trace_is_not_complete() -> None:
    """空事件列表是「没跑」，不是「跑完了没事件」。"""
    run = RunResultView(trace_id="t", status=JobStatus.QUEUED, stage=JobStage.INTENT)
    assert run.replay_complete() is False


def test_job_record_json_roundtrip() -> None:
    record = JobRecord(
        job_id="j1",
        trace_id="t1",
        user_id="P01",
        status=JobStatus.AWAITING_HUMAN,
        stage=JobStage.REPORTING,
        percent=95,
        iso_week="2026W02",
        lock_token="tok",
        error_code=ErrorCode.REVISION_INFEASIBLE,
        finished_at=datetime(2026, 1, 2, 9, 14, 3),
    )
    again = JobRecord.from_json(record.to_json())
    assert again == record


def test_advance_only_moves_forward() -> None:
    """修订轮会把图带回 `solve`，进度条**不许**跟着倒退。"""
    store = InMemoryStore()
    jobs = JobStore(store)
    jobs.put(JobRecord(job_id="j", trace_id="t"))
    jobs.advance("j", "validate")
    assert (jobs.get("j") or JobRecord(job_id="", trace_id="")).percent == STAGE_PERCENT[
        JobStage.VALIDATING
    ]
    jobs.advance("j", "solve")
    record = jobs.get("j")
    assert record is not None
    assert record.percent == STAGE_PERCENT[JobStage.VALIDATING], "阶段倒退了"
    assert record.status is JobStatus.RUNNING


def test_advance_ignores_unknown_nodes() -> None:
    store = InMemoryStore()
    jobs = JobStore(store)
    jobs.put(JobRecord(job_id="j", trace_id="t"))
    jobs.advance("j", "__interrupt__")
    record = jobs.get("j")
    assert record is not None
    assert record.percent == 0


def test_mark_done_sets_hundred_percent() -> None:
    store = InMemoryStore()
    jobs = JobStore(store)
    jobs.put(JobRecord(job_id="j", trace_id="t"))
    jobs.mark("j", JobStatus.DONE)
    record = jobs.get("j")
    assert record is not None
    assert record.percent == 100


def test_trace_lookup_finds_the_job() -> None:
    store = InMemoryStore()
    jobs = JobStore(store)
    jobs.put(JobRecord(job_id="j", trace_id="t"))
    assert jobs.job_id_for_trace("t") == "j"
    found = jobs.get_by_trace("t")
    assert found is not None and found.job_id == "j"


@pytest.mark.parametrize("status", list(JobStatus))
def test_job_status_values_are_stable_strings(status: JobStatus) -> None:
    """状态值进 URL、进前端判断，改一个字面量就是破坏性变更。"""
    assert status.value == status.name
