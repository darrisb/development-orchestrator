from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.base import Base
from apps.orchestrator.domain.enums import EscalationStatus, TaskStatus
from apps.orchestrator.services import reconstruction_importer as importer

MANIFEST = Path("data/recovery/tracestack_reconstruction_manifest.json")
SOURCE_REPO = Path("workspace/tracestack-clean")
#: The pre-C73 campaign baseline the manifest evidences and the importer verifies.
INTEGRATION_SHA = "fc6abc579cee88f821c5f72f00162872b2dc8326"


@pytest.fixture
def pinned_repo(tmp_path: Path) -> Path:
    """A disposable clone frozen at the campaign baseline the manifest describes.

    The importer validates the campaign repository by read-only plumbing against
    its ``repository.path``: it requires ``agent/integration`` at the historical
    baseline, the human source as a non-integrated sibling, and the seven
    ``agent/TS-109-*-runN`` refs. The live campaign has since legitimately advanced
    that ref and merged the human source, so the tests must not read the shared
    repository's mutable position. Cloning brings every real object across (they are
    ancestors of the advanced ref); pinning the single ref back gives a
    deterministic fixture. The canonical repository is never written.
    """
    clone = tmp_path / "tracestack-frozen"
    subprocess.run(
        ["git", "clone", "--quiet", str(SOURCE_REPO.resolve()), str(clone)], check=True
    )
    # Materialise the campaign's local branches (the importer reads refs/heads/*
    # for the TS-109 run refs) and freeze the baseline ref to the historical
    # checkpoint; the objects are already in the clone as ancestors of the
    # advanced ref, so this touches only refs.
    heads = subprocess.run(
        ["git", "-C", str(clone), "for-each-ref", "--format=%(refname)",
         "refs/remotes/origin/"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    default = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    for ref in heads:
        if ref.endswith("/HEAD"):
            continue
        name = ref.removeprefix("refs/remotes/origin/")
        if name == default:
            continue
        subprocess.run(["git", "-C", str(clone), "branch", "--force", name, ref], check=True)
    subprocess.run(
        ["git", "-C", str(clone), "update-ref", "refs/heads/agent/integration", INTEGRATION_SHA],
        check=True,
    )
    return clone


@pytest.fixture
def manifest_copy(tmp_path: Path, pinned_repo: Path) -> Path:
    target = tmp_path / "manifest.json"
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # Point the manifest at the deterministic clone instead of the live campaign
    # repository, whose refs the campaign has since moved.
    payload["repository"]["path"] = str(pinned_repo)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    path = tmp_path / "reconstruction_test.db"
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    # The schema create_all just built corresponds to whatever head the code is
    # at, so the marker has to name that head -- exactly the value the importer
    # will demand via manifest["required_alembic_head"]. Hardcoding an old
    # revision here is how the previous migration silently broke this file.
    manifest = _load(MANIFEST)
    head = manifest.get("required_alembic_head") or manifest["expected_alembic_head"]
    with engine.begin() as connection:
        connection.execute(text("create table alembic_version (version_num varchar(32))"))
        connection.execute(
            text("insert into alembic_version(version_num) values (:head)"), {"head": head}
        )
    engine.dispose()
    return f"sqlite:///{path}"


def _database_name(url: str) -> str:
    return make_url(url).database or ""


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _runner(*, docker_names: str = "", ref_override: str | None = None):
    """Deterministic runner: real git plumbing, controlled docker/for-each-ref."""

    def runner(command, **kwargs):
        if command[0] == "docker":
            return subprocess.CompletedProcess(command, 0, docker_names, "")
        if command[0] == "git" and ref_override is not None and "for-each-ref" in command:
            return subprocess.CompletedProcess(command, 0, ref_override, "")
        return subprocess.run(command, **kwargs)

    return runner


def _dry_run(manifest: Path, url: str, **kwargs):
    return importer.dry_run_reconstruction(
        manifest_path=manifest,
        database_url=url,
        confirm_database=_database_name(url),
        runner=kwargs.pop("runner", _runner()),
        **kwargs,
    )


def _import(manifest: Path, url: str, **kwargs):
    return importer.import_reconstruction(
        manifest_path=manifest,
        database_url=url,
        confirm_database=_database_name(url),
        runner=kwargs.pop("runner", _runner()),
        **kwargs,
    )


def test_valid_manifest_accepted(manifest_copy: Path, sqlite_url: str):
    report = _dry_run(manifest_copy, sqlite_url)

    assert report["dry_run"] is True
    assert report["inserted_row_counts"]["tasks"] == 10
    assert report["inserted_row_counts"]["task_runs"] == 7


def test_wrong_manifest_version_rejected(manifest_copy: Path, sqlite_url: str):
    payload = _load(manifest_copy)
    payload["schema_version"] = 999
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="unsupported"):
        _dry_run(manifest_copy, sqlite_url)


def test_backup_missing_rejected(manifest_copy: Path, sqlite_url: str):
    payload = _load(manifest_copy)
    payload["canonical_backup"]["path"] = "data/backups/missing.dump"
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="missing"):
        _dry_run(manifest_copy, sqlite_url)


def test_backup_checksum_mismatch_rejected(manifest_copy: Path, sqlite_url: str):
    """Zero the checksum in BOTH manifest and metadata.

    The only layer left that can notice is the importer's own SHA-256 of the
    dump file: if that comparison were bypassed, every other check would
    still agree and the dry run would be accepted.
    """
    payload = _load(manifest_copy)
    payload["canonical_backup"]["sha256"] = "0" * 64
    metadata_path = importer.REPO_ROOT / payload["canonical_backup"]["metadata"]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sha256"] = "0" * 64
    # A sibling metadata file that agrees with the tampered manifest checksum:
    # only the importer's own SHA-256 of the dump can catch this.
    sibling = manifest_copy.with_name("metadata-zero.json")
    sibling.write_text(json.dumps(metadata), encoding="utf-8")
    payload["canonical_backup"]["metadata"] = str(sibling)
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="checksum mismatch"):
        _dry_run(manifest_copy, sqlite_url)


def test_backup_not_verified_rejected(manifest_copy: Path, sqlite_url: str, tmp_path: Path):
    backup = Path(_load(manifest_copy)["canonical_backup"]["path"])
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "sha256": importer._sha256_file(backup),
                "alembic_revision": "e8a3c7f21d49",
                "restore_checks": {"alembic_revision": "e8a3c7f21d49"},
            }
        ),
        encoding="utf-8",
    )
    payload = _load(manifest_copy)
    payload["canonical_backup"]["metadata"] = str(metadata)
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="not verified"):
        _dry_run(manifest_copy, sqlite_url)


def test_migration_head_mismatch_rejected(manifest_copy: Path, sqlite_url: str):
    engine = create_engine(sqlite_url, future=True)
    with engine.begin() as connection:
        connection.execute(text("update alembic_version set version_num = 'wrong'"))
    engine.dispose()

    with pytest.raises(importer.ReconstructionError, match="migration head"):
        _dry_run(manifest_copy, sqlite_url)


def test_manifest_required_head_matches_the_code_head():
    """The trap that caught this repo once: a migration lands, the sealed
    ``expected_alembic_head`` must not move, and without
    ``required_alembic_head`` advancing in step, every reconstruction is
    silently refused. This test fails on the day they diverge."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    manifest = _load(MANIFEST)
    assert manifest.get("required_alembic_head") == head
    assert manifest["expected_alembic_head"] == "e8a3c7f21d49"


def test_required_head_falls_back_to_expected_when_absent(manifest_copy: Path, sqlite_url: str):
    """Removing the forward-compat key re-seals the gate to the backup's head.

    The migration-head tests above already run a database at
    ``required_alembic_head``; with the key stripped, the importer must demand
    the historical ``expected_alembic_head`` instead and refuse the newer --
    schema-current -- database rather than write columns into a DB whose
    migration state it cannot confirm.
    """
    payload = _load(manifest_copy)
    assert payload.pop("required_alembic_head") != payload["expected_alembic_head"]
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="does not match"):
        _dry_run(manifest_copy, sqlite_url)


def test_partial_provenance_columns_are_importable_and_null(manifest_copy: Path, sqlite_url: str):
    """A reconstructed escalation carries neither provenance value.

    The reconstruction imports the pre-C73 state: ``human_commit`` and
    ``integration_resolution_commit`` are both NULL, and the source-vs-
    resolution distinction is made only later, by reconciliation. The import
    must not invent either value.
    """
    _import(manifest_copy, sqlite_url)
    engine = create_engine(sqlite_url, future=True)
    with engine.begin() as connection:
        row = connection.execute(
            text("select human_commit, integration_resolution_commit from human_escalations")
        ).one()
    engine.dispose()
    assert row == (None, None)


def test_non_empty_campaign_db_rejected(manifest_copy: Path, sqlite_url: str):
    engine = create_engine(sqlite_url, future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "insert into projects(id, name, repository_path, default_branch, "
                "worker_profile, status, protected_paths, sensitive_path_exceptions, "
                "generated_path_exceptions, dependency_paths, verification_profile) "
                "values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'x', '/tmp/x', "
                "'main', 'node', 'REGISTERED', '[]', '[]', '[]', '[]', '{}')"
            )
        )
    engine.dispose()

    with pytest.raises(importer.ReconstructionError, match="projects"):
        _dry_run(manifest_copy, sqlite_url)


def test_partial_campaign_state_rejected(manifest_copy: Path, sqlite_url: str):
    engine = create_engine(sqlite_url, future=True)
    factory = sessionmaker(bind=engine)
    with factory.begin() as session:
        project = importer._project_row(_load(manifest_copy))
        session.add(models.ProjectRow(**project))
        session.flush()
        tasks = importer._task_rows(_load(manifest_copy))
        for row in tasks[:9]:
            session.add(models.TaskRow(**row))
    engine.dispose()

    with pytest.raises(importer.ReconstructionError, match="not empty"):
        _import(manifest_copy, sqlite_url)
    with pytest.raises(importer.ReconstructionError, match="not empty"):
        _dry_run(manifest_copy, sqlite_url)


def test_integration_sha_mismatch_rejected(manifest_copy: Path, sqlite_url: str):
    payload = _load(manifest_copy)
    payload["repository"]["expected_integration_sha"] = "0" * 40
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="must start at the integration SHA"):
        _dry_run(manifest_copy, sqlite_url)


def test_observed_integration_sha_mismatch_rejected(manifest_copy: Path, sqlite_url: str):
    """Point the manifest's integration ref at a different real branch.

    The manifest still claims the canonical SHA (so the artifact shape checks
    pass), but the importer's own `git rev-parse` of the ref disagrees.
    """
    payload = _load(manifest_copy)
    payload["repository"]["integration_ref"] = "refs/heads/master"
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="does not match"):
        _dry_run(manifest_copy, sqlite_url)


def test_cbff_already_integrated_rejected(manifest_copy: Path, sqlite_url: str):
    def runner(command, **kwargs):
        if command[:3] == ("git", "merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "docker":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.run(command, **kwargs)

    with pytest.raises(importer.ReconstructionError, match="already integrated"):
        _dry_run(manifest_copy, sqlite_url, runner=runner)


def test_missing_git_commit_rejected(manifest_copy: Path, sqlite_url: str):
    payload = _load(manifest_copy)
    payload["repository"]["human_commit"] = "f" * 40
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="git cat-file"):
        _dry_run(manifest_copy, sqlite_url)


def test_missing_ts109_run_ref_rejected(manifest_copy: Path, sqlite_url: str):
    short = "\n".join(
        f"agent/TS-109-keep-only-the-entries-from-one-navigation-source-run{n}"
        for n in range(1, 7)
    )
    with pytest.raises(importer.ReconstructionError, match="run1..run7"):
        _dry_run(manifest_copy, sqlite_url, runner=_runner(ref_override=short))


def test_active_worker_container_rejected(manifest_copy: Path, sqlite_url: str):
    with pytest.raises(importer.ReconstructionError, match="worker operation appears active"):
        _dry_run(
            manifest_copy,
            sqlite_url,
            runner=_runner(docker_names="orchestrator-worker-deadbeef12\n"),
        )


def test_missing_artifact_rejected(manifest_copy: Path, sqlite_url: str):
    payload = _load(manifest_copy)
    payload["ts109_runs"][0]["evidence"]["outcome"] = "data/runs/missing.json"
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="artifact"):
        _dry_run(manifest_copy, sqlite_url)


def test_artifact_manifest_mismatch_rejected(manifest_copy: Path, sqlite_url: str):
    payload = _load(manifest_copy)
    payload["ts109_runs"][0]["status"] = "SUCCEEDED"
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="artifact mismatch"):
        _dry_run(manifest_copy, sqlite_url)


def test_artifact_review_cycle_mismatch_rejected(manifest_copy: Path, sqlite_url: str):
    payload = _load(manifest_copy)
    payload["ts109_runs"][0]["review_cycle"] = 2
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="artifact mismatch"):
        _dry_run(manifest_copy, sqlite_url)


def test_escalation_id_artifact_mismatch_rejected(manifest_copy: Path, sqlite_url: str):
    payload = _load(manifest_copy)
    payload["escalation"]["id"] = "f" * 32 + "-" + "0" * 23
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="escalation id mismatch"):
        _dry_run(manifest_copy, sqlite_url)


def test_task_definition_mismatch_rejected(manifest_copy: Path, sqlite_url: str, tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree("workspace/tracestack-clean", repo, ignore=shutil.ignore_patterns(".git"))
    manifest_yaml = repo / "build.tasks.yaml"
    manifest_yaml.write_text(
        manifest_yaml.read_text(encoding="utf-8").replace("TS-110", "TS-999", 1),
        encoding="utf-8",
    )
    payload = _load(manifest_copy)
    payload["repository"]["path"] = str(repo)
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="task definition mismatch"):
        _dry_run(manifest_copy, sqlite_url)


def test_shared_limits_must_match_declared_values(manifest_copy: Path, sqlite_url: str):
    assert importer.SHARED_LIMITS == {
        "max_attempts": 3,
        "max_review_cycles": 2,
        "max_files_changed": 3,
        "max_diff_lines": 150,
        "max_runtime_minutes": 30,
    }
    plan = importer.load_reconstruction_plan(manifest_copy)
    for row in plan.task_rows:
        for name, expected in importer.SHARED_LIMITS.items():
            assert row[name] == expected


def test_ts110_uuid_deterministic_and_synthetic(manifest_copy: Path):
    first = importer.load_reconstruction_plan(manifest_copy)
    second = importer.load_reconstruction_plan(manifest_copy)

    recomputed = importer.synthetic_ts110_uuid(
        "d98cb1e7-75e4-401a-8b21-d0965b0b3115"
    )
    assert str(recomputed) == "4abc278d-57c4-515a-baef-874e1c89aa7a"
    assert recomputed == importer.synthetic_ts110_uuid("d98cb1e7-75e4-401a-8b21-d0965b0b3115")
    assert first.manifest["synthetic_identities"][0]["id"] == str(recomputed)
    assert first.manifest["synthetic_identities"][0]["classification"] == "SYNTHETIC"
    assert first.manifest["synthetic_identities"] == second.manifest["synthetic_identities"]


def test_ts110_uuid_nondeterministic_manifest_rejected(manifest_copy: Path):
    payload = _load(manifest_copy)
    tampered = "11111111-2222-3333-4444-555555555555"
    payload["tasks"][-1]["id"] = tampered
    payload["synthetic_identities"][0]["id"] = tampered
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="deterministic reconstruction"):
        importer.load_reconstruction_plan(manifest_copy)


def test_ts110_uuid_classified_synthetic(manifest_copy: Path):
    payload = _load(manifest_copy)
    payload["tasks"][-1]["identity_provenance"] = "EVIDENCED"
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="SYNTHETIC"):
        importer.load_reconstruction_plan(manifest_copy)


def test_evidenced_uuids_preserved(manifest_copy: Path):
    plan = importer.load_reconstruction_plan(manifest_copy)

    assert str(plan.project_row["id"]) == "d98cb1e7-75e4-401a-8b21-d0965b0b3115"
    assert str(plan.run_rows[-1]["id"]) == "0a77bbb6-68ca-4e84-9572-5e37552387bf"
    assert plan.manifest["tasks"][0]["id"] == "9d70004c-1a2f-4249-b788-a4c3d59cb8c7"
    for run in plan.manifest["ts109_runs"]:
        assert run["field_provenance"]["id"] == "EVIDENCED"


def test_derived_run_numbers_classified(manifest_copy: Path):
    plan = importer.load_reconstruction_plan(manifest_copy)

    assert [row["run_number"] for row in plan.run_rows] == list(range(1, 8))
    assert all(run["run_number_provenance"] == "DERIVED" for run in plan.manifest["ts109_runs"])
    assert all(
        run["field_provenance"]["run_number"] == "DERIVED" for run in plan.manifest["ts109_runs"]
    )


def test_run_number_evidenced_claim_rejected(manifest_copy: Path):
    payload = _load(manifest_copy)
    payload["ts109_runs"][0]["run_number_provenance"] = "EVIDENCED"
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="DERIVED"):
        importer.load_reconstruction_plan(manifest_copy)


def test_missing_required_non_null_historical_value_fails(manifest_copy: Path):
    payload = _load(manifest_copy)
    payload["escalation"]["summary_source"] = None
    _write(manifest_copy, payload)

    with pytest.raises(importer.ReconstructionError, match="refusing to invent"):
        importer.load_reconstruction_plan(manifest_copy)


def test_required_column_guard_refuses_invention(manifest_copy: Path, monkeypatch):
    original = importer._escalation_row

    def row_with_missing_reason(manifest):
        row = original(manifest)
        row["reason"] = None
        return row

    monkeypatch.setattr(importer, "_escalation_row", row_with_missing_reason)
    with pytest.raises(importer.ReconstructionError, match="refusing to invent"):
        importer.load_reconstruction_plan(manifest_copy)


def test_dry_run_writes_zero_rows(manifest_copy: Path, sqlite_url: str):
    _dry_run(manifest_copy, sqlite_url)

    engine = create_engine(sqlite_url, future=True)
    with engine.connect() as connection:
        for table in importer.EMPTY_TABLES:
            count = connection.execute(
                text(f"select count(*) from {table.__tablename__}")
            ).scalar()
            assert count == 0, table.__tablename__
    engine.dispose()


def test_dry_run_computes_same_identities_as_import(manifest_copy: Path, sqlite_url: str):
    report = _dry_run(manifest_copy, sqlite_url)
    _import(manifest_copy, sqlite_url)

    engine = create_engine(sqlite_url, future=True)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        assert str(session.query(models.ProjectRow).one().id) == report["project_id"]
        for external_id, claimed in report["task_ids"].items():
            row = session.query(models.TaskRow).filter_by(external_task_id=external_id).one()
            assert str(row.id) == claimed
        run_ids = {str(row.id) for row in session.query(models.TaskRunRow)}
        assert run_ids == set(report["run_ids"])
        escalation = session.query(models.HumanEscalationRow).one()
        assert str(escalation.id) == report["escalation_id"]
    engine.dispose()


def test_successful_import_minimum_rows_and_replay_rejected(manifest_copy: Path, sqlite_url: str):
    report = _import(manifest_copy, sqlite_url)

    assert report["transaction_result"] == "committed"
    assert report["notice"] == importer.RECONSTRUCTION_MESSAGE
    engine = create_engine(sqlite_url, future=True)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        assert session.query(models.ProjectRow).count() == 1
        assert session.query(models.TaskRow).count() == 10
        assert session.query(models.TaskRunRow).count() == 7
        assert session.query(models.HumanEscalationRow).count() == 1
        for row_class in (
            models.ModelRunRow,
            models.RunEventRow,
            models.ReviewRow,
            models.ReviewIssueRow,
            models.VerificationRunRow,
            models.TrainingExampleRow,
            models.LessonRow,
            models.ArtifactRow,
            models.MilestoneRow,
            models.PauseRequestRow,
            models.WorkflowCheckpointRow,
            models.WorkflowWriteRow,
        ):
            assert session.query(row_class).count() == 0, row_class.__tablename__
        ts109 = session.query(models.TaskRow).filter_by(external_task_id="TS-109").one()
        ts110 = session.query(models.TaskRow).filter_by(external_task_id="TS-110").one()
        assert ts109.status is TaskStatus.COMPLETE
        assert ts110.status is TaskStatus.READY
        assert session.query(models.TaskRunRow).filter_by(task_id=ts110.id).count() == 0
        escalation = session.query(models.HumanEscalationRow).one()
        assert escalation.status is EscalationStatus.RESOLVED
        assert escalation.resolution_intent == "COMPLETED_BY_HAND"
        assert escalation.human_commit is None
        assert escalation.resolution is None
        assert escalation.resolved_at is None
        assert session.query(models.TaskRunRow).filter_by(task_id=ts109.id).count() == 7

    with pytest.raises(importer.ReconstructionError, match="not empty"):
        _import(manifest_copy, sqlite_url)
    engine.dispose()


def test_replay_after_import_dry_run_also_refuses(manifest_copy: Path, sqlite_url: str):
    _import(manifest_copy, sqlite_url)

    with pytest.raises(importer.ReconstructionError, match="not empty"):
        _dry_run(manifest_copy, sqlite_url)
    with pytest.raises(importer.ReconstructionError, match="not empty"):
        _import(manifest_copy, sqlite_url)


def test_transaction_rollback_leaves_zero_rows(manifest_copy: Path, sqlite_url: str, monkeypatch):
    original = importer._insert_rows

    def fail_after_project(session, plan):
        session.add(models.ProjectRow(**plan.project_row))
        session.flush()
        raise importer.ReconstructionError("boom")

    monkeypatch.setattr(importer, "_insert_rows", fail_after_project)
    with pytest.raises(importer.ReconstructionError, match="boom"):
        _import(manifest_copy, sqlite_url)
    monkeypatch.setattr(importer, "_insert_rows", original)

    engine = create_engine(sqlite_url, future=True)
    with engine.connect() as connection:
        for table in importer.EMPTY_TABLES:
            count = connection.execute(
                text(f"select count(*) from {table.__tablename__}")
            ).scalar()
            assert count == 0, table.__tablename__
    engine.dispose()


def test_importer_never_invokes_scheduler_or_models(manifest_copy: Path, sqlite_url: str):
    from apps.orchestrator.services import model_runs, scheduler

    def explode(*args, **kwargs):
        raise AssertionError("importer must not invoke this")

    original_select = scheduler.select_next_task
    original_model_runs = model_runs.record_model_call
    scheduler.select_next_task = explode
    model_runs.record_model_call = explode
    try:
        _import(manifest_copy, sqlite_url)
    finally:
        scheduler.select_next_task = original_select
        model_runs.record_model_call = original_model_runs


def test_importer_never_invokes_c73_reconciliation(manifest_copy: Path, sqlite_url: str):
    from apps.orchestrator.services import integration, reviews
    from apps.orchestrator.workflow import resolution

    def explode(*args, **kwargs):
        raise AssertionError("importer must not invoke C73 reconciliation")

    originals = (
        (reviews, "reconcile_human_commit", reviews.reconcile_human_commit),
        (integration, "integrate_human_commit", integration.integrate_human_commit),
        (resolution, "apply_escalation_answer", resolution.apply_escalation_answer),
    )
    for module, name, _value in originals:
        setattr(module, name, explode)
    try:
        _import(manifest_copy, sqlite_url)
    finally:
        for module, name, value in originals:
            setattr(module, name, value)


def test_importer_never_mutates_git_refs(manifest_copy: Path, sqlite_url: str, tmp_path: Path):
    observed = []

    def spy(command, **kwargs):
        if command[0] == "git":
            observed.append(tuple(command))
        return _runner()(command, **kwargs)

    before = subprocess.run(
        ("git", "for-each-ref", "refs/heads/agent/"),
        cwd="workspace/tracestack-clean",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    _dry_run(manifest_copy, sqlite_url, runner=spy)
    _import(manifest_copy, sqlite_url, runner=spy)
    after = subprocess.run(
        ("git", "for-each-ref", "refs/heads/agent/"),
        cwd="workspace/tracestack-clean",
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert before == after
    assert observed
    for command in observed:
        assert command[1] in importer.READ_ONLY_GIT_COMMANDS, command


def test_importer_refuses_git_mutations_even_if_asked():
    def runner(command, **kwargs):
        raise AssertionError("runner must never be reached for a mutation")

    with pytest.raises(importer.ReconstructionError, match="read-only"):
        importer._git(
            Path("workspace/tracestack-clean"),
            "update-ref",
            "refs/heads/agent/integration",
            "0" * 40,
            runner=runner,
        )


def test_report_omission_and_provenance_fields(manifest_copy: Path, sqlite_url: str):
    report = _dry_run(manifest_copy, sqlite_url)

    assert report["notice"] == "RECONSTRUCTED STATE -- NOT ORIGINAL DATABASE HISTORY"
    assert report["manifest_version"] == 1
    assert len(report["manifest_hash"]) == 64
    assert report["synthetic_identities"][0]["id"] == report["task_ids"]["TS-110"]
    assert "model_runs" in report["omitted_history"]
    assert report["inserted_row_counts"]["model_runs"] == 0
    assert report["inserted_row_counts"]["run_events"] == 0
    assert report["integration_sha_observed"] == (
        "fc6abc579cee88f821c5f72f00162872b2dc8326"
    )
    assert report["backup_checksum_verified"] == (
        "e64a0e42fa4d7995794fb475d59f2fa6cbc691ad6ad3206abb818731b3a79bf8"
    )
    assert report["dry_run"] is True
