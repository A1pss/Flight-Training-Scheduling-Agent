"""manifest.yaml 与归档四件套（v6 §10.6）。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from backend.core.errors import ExportVerifyError
from backend.report.archive import NO_SOLVER_LOG, archive_plan, code_version_from_git
from backend.report.manifest import (
    build_manifest,
    dump_manifest,
    load_manifest,
    missing_reproducibility_fields,
)
from backend.report.naming import parse_name, read_ledger, week_dir
from backend.report.verify import FAILED_JSON_SUFFIX, STAGING_SUFFIX, export_workbook
from tests.fixtures.report_bundle import GENERATED_AT, sample_bundle


@pytest.fixture(scope="module")
def archived(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, object]:
    root = tmp_path_factory.mktemp("plans")
    bundle = sample_bundle(solver_log="#Model fts_2026W02\n#Done wall_time 21.0s\n")
    result = archive_plan(bundle, root=root, now=GENERATED_AT)
    return root, result


# ─────────────────────────────────────────────────────────────────────
# manifest 字段
# ─────────────────────────────────────────────────────────────────────
def test_manifest_has_the_two_v6_new_fields(archived: tuple[Path, object]) -> None:
    """`semantics_switches` 与 `solver.num_search_workers` 是 v6 新增的硬性字段。"""
    manifest = load_manifest(archived[1].manifest)  # type: ignore[attr-defined]
    assert manifest["semantics_switches"], (
        "缺 semantics_switches：不同语义解读排出的班将无法审计追溯"
    )
    assert manifest["solver"]["num_search_workers"] == 8
    assert manifest["solver"]["seed"] == 42


def test_manifest_is_reproducibility_complete(archived: tuple[Path, object]) -> None:
    manifest = load_manifest(archived[1].manifest)  # type: ignore[attr-defined]
    assert missing_reproducibility_fields(manifest) == ()


def test_missing_fields_are_reported_not_silently_defaulted() -> None:
    assert "snapshot_id" in missing_reproducibility_fields({"plan_id": "p"})
    assert "solver.num_search_workers" in missing_reproducibility_fields(
        {"solver": {"name": "cp-sat", "seed": 42, "status": "OPTIMAL"}}
    )


def test_manifest_mirrors_the_plan(archived: tuple[Path, object]) -> None:
    _root, result = archived
    manifest = load_manifest(result.manifest)  # type: ignore[attr-defined]
    plan_json = yaml.safe_load(result.plan_json.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert manifest["plan_id"] == plan_json["plan_id"]
    assert manifest["content_sha256"] == plan_json["content_sha256"]
    assert manifest["semantics_switches"] == plan_json["semantics_switches"]
    assert manifest["snapshot_id"] == plan_json["snapshot_id"]
    assert manifest["relaxation_tier"] == plan_json["relaxation_tier"]


def test_absent_provenance_is_null_not_invented() -> None:
    """M4 才有 prompt/skill 版本 —— 此刻写 null，不编一个 v1（铁律 6）。"""
    bundle = sample_bundle()
    manifest = build_manifest(
        bundle, parse_name("FTP_NAU_WEEKLY_2026W02_20260105-20260111_v1_DRAFT_00000000.xlsx")
    )
    assert manifest["prompt_versions"] is None
    assert manifest["skill_version"] is None
    assert "prompt_versions: null" in dump_manifest(manifest)


def test_manifest_yaml_is_deterministic() -> None:
    bundle = sample_bundle()
    name = parse_name("FTP_NAU_WEEKLY_2026W02_20260105-20260111_v1_DRAFT_00000000.xlsx")
    assert dump_manifest(build_manifest(bundle, name)) == dump_manifest(
        build_manifest(bundle, name)
    )


def test_code_version_reads_git_head_without_subprocess() -> None:
    version = code_version_from_git()
    assert version is None or (version.startswith("git:") and len(version) == 11)


# ─────────────────────────────────────────────────────────────────────
# 归档结构
# ─────────────────────────────────────────────────────────────────────
def test_archive_writes_the_full_five_file_set(archived: tuple[Path, object]) -> None:
    root, result = archived
    directory = week_dir("2026W02", root=root)
    assert result.directory == directory  # type: ignore[attr-defined]
    for path in result.all_paths():  # type: ignore[attr-defined]
        assert path.exists() and path.stat().st_size > 0
    names = {p.name for p in directory.iterdir()}
    assert names == {
        result.name.xlsx,  # type: ignore[attr-defined]
        result.name.json,  # type: ignore[attr-defined]
        result.name.manifest,  # type: ignore[attr-defined]
        result.name.validation_report,  # type: ignore[attr-defined]
        result.name.solver_log,  # type: ignore[attr-defined]
        "versions.json",
    }


def test_validation_report_carries_all_three_gates(archived: tuple[Path, object]) -> None:
    payload = yaml.safe_load(archived[1].validation_report.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert len(payload["gate1_rules"]["results"]) == 14
    assert payload["gate2_format"]["passed"] is True
    assert payload["gate3_workbook"]["passed"] is True


def test_solver_log_is_written_verbatim(archived: tuple[Path, object]) -> None:
    text = archived[1].solver_log.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "#Done wall_time 21.0s" in text


def test_missing_solver_log_says_so_instead_of_faking_one(tmp_path: Path) -> None:
    result = archive_plan(sample_bundle(solver_log=""), root=tmp_path, now=GENERATED_AT)
    assert result.solver_log.read_text(encoding="utf-8") == NO_SOLVER_LOG


def test_failed_readback_delivers_nothing_but_keeps_the_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FTS-5001：回读不等 → 不交付 xlsx，但保留中间 JSON。"""
    import backend.report.verify as verify_mod

    bundle = sample_bundle()
    real_render = verify_mod.render_workbook

    def tampered(path: Path, b: object, *, readback_passed: bool | None = None) -> Path:
        # 渲染一份「快照号被改过」的表 → 回读结果与源对象必然不等
        wrong = bundle.plan.model_copy(update={"snapshot_id": "snap_tampered"})
        return real_render(
            path,
            sample_bundle(plan=wrong, ctx=bundle.ctx),
            readback_passed=readback_passed,
        )

    monkeypatch.setattr(verify_mod, "render_workbook", tampered)
    target = tmp_path / "plan.xlsx"
    with pytest.raises(ExportVerifyError) as exc:
        export_workbook(target, bundle)

    assert exc.value.code == "FTS-5001"
    assert not target.exists(), "回读失败仍然交付了文件"
    assert not target.with_name(target.stem + STAGING_SUFFIX).exists()
    kept = target.with_name(target.stem + FAILED_JSON_SUFFIX)
    assert kept.exists()
    payload = yaml.safe_load(kept.read_text(encoding="utf-8"))
    assert payload["plan"]["plan_id"] == bundle.plan.plan_id
    assert any("snapshot_id" in d for d in payload["diff"])


def test_failed_export_still_burns_the_version_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """版本号一旦分配就作废，不回收 —— 与 §10.6「永不复用」一致。"""
    import backend.report.verify as verify_mod

    bundle = sample_bundle()

    def broken(path: Path, b: object, *, readback_passed: bool | None = None) -> Path:
        path.write_bytes(b"not an xlsx")
        return path

    monkeypatch.setattr(verify_mod, "render_workbook", broken)
    with pytest.raises(Exception):  # noqa: B017 - 坏文件在 openpyxl 层就炸，类型不定
        archive_plan(bundle, root=tmp_path, now=GENERATED_AT)

    monkeypatch.undo()
    second = archive_plan(bundle, root=tmp_path, now=GENERATED_AT)
    assert second.name.version == 2
    assert [e.version for e in read_ledger(week_dir("2026W02", root=tmp_path))] == [1, 2]
