"""One-shot evidence-backed campaign reconstruction importer.

Rows inserted by this module are reconstructed state, not original database
history. The importer is deliberately narrow: it validates the reviewed
manifest and every surviving piece of evidence before opening a write
transaction, then inserts only the minimum rows required for the pre-C73
TraceStack recovery point, in one transaction, with replay protection.

Provenance contract:

* EVIDENCED -- the value exists in a surviving durable artifact and is
  cross-checked against it before any write.
* DERIVED -- the value is computed by a documented deterministic rule from
  evidenced inputs (for example TS-109 run numbers 1..7 in artifact order).
* SYNTHETIC -- the identity is generated on purpose because the original is
  unrecoverable (TS-110's UUIDv5), and is labelled as not the original.

Missing historical values stay omitted (NULL), never invented. Where the
schema requires a non-null value that cannot be evidenced, the importer
stops with an error instead of fabricating one.

The importer never mutates Git, never starts the scheduler, never invokes
concern 73 reconciliation, and never synthesizes model/audit/review history.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator import _build_meta
from apps.orchestrator.db import models
from apps.orchestrator.domain.enums import (
    Complexity,
    EscalationStatus,
    FailureReason,
    ProjectStatus,
    RiskLevel,
    RunStatus,
    TaskStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.escalation import EscalationIntent, run_escalation_options
from apps.orchestrator.domain.manifest import ManifestTask
from apps.orchestrator.services.manifest_loader import load_manifest

RECONSTRUCTION_MESSAGE = "RECONSTRUCTED STATE -- NOT ORIGINAL DATABASE HISTORY"
SUPPORTED_MANIFEST_VERSION = 1

#: Repository root, so repo-relative manifest evidence paths resolve the same
#: way from any working directory.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Documented reconstruction namespace for synthetic identities. The identity
#: generated under this namespace is a reconstruction, never the original.
TS110_RECONSTRUCTION_NAMESPACE = uuid.UUID("9b1b9d5b-1f77-4b43-9f35-9e8f0dff4b6d")
TS110_SYNTHETIC_TASK_ID = "TS-110"

#: Shared limits every reconstruction manifest must agree with, taken from
#: the surviving build.tasks.yaml declarations.
SHARED_LIMITS = {
    "max_attempts": 3,
    "max_review_cycles": 2,
    "max_files_changed": 3,
    "max_diff_lines": 150,
    "max_runtime_minutes": 30,
}

#: Only read-only Git plumbing may ever be executed by this importer.
READ_ONLY_GIT_COMMANDS = frozenset({"rev-parse", "cat-file", "merge-base", "for-each-ref"})

EMPTY_TABLES = (
    models.ArtifactRow,
    models.HumanEscalationRow,
    models.LessonRow,
    models.MilestoneRow,
    models.ModelRunRow,
    models.PauseRequestRow,
    models.ProjectRow,
    models.ReviewIssueRow,
    models.ReviewRow,
    models.RunEventRow,
    models.TaskRunRow,
    models.TaskRow,
    models.TrainingExampleRow,
    models.VerificationRunRow,
    models.WorkflowCheckpointRow,
    models.WorkflowWriteRow,
)


class ReconstructionError(RuntimeError):
    """Importer precondition or reconstruction failure."""


@dataclass(frozen=True)
class ReconstructionPlan:
    manifest: dict[str, Any]
    manifest_hash: str
    task_rows: list[dict[str, Any]]
    run_rows: list[dict[str, Any]]
    project_row: dict[str, Any]
    escalation_row: dict[str, Any]
    row_counts: dict[str, int]
    notes: dict[str, Any] = field(default_factory=dict)


def synthetic_ts110_uuid(project_uuid: str) -> uuid.UUID:
    """Deterministically reconstruct the TS-110 task identity.

    The result is classified SYNTHETIC: it is *not* the original database
    UUID, which was lost with the PostgreSQL history.
    """
    name = f"{project_uuid}:{TS110_SYNTHETIC_TASK_ID}"
    return uuid.uuid5(TS110_RECONSTRUCTION_NAMESPACE, name)


def load_reconstruction_plan(manifest_path: Path) -> ReconstructionPlan:
    manifest = _load_manifest_json(manifest_path)
    _validate_manifest_shape(manifest)
    _validate_synthetic_identity(manifest)
    _validate_artifacts(manifest)
    _validate_task_definitions(manifest)
    manifest_hash = _sha256_bytes(manifest_path.read_bytes())
    project_row = _project_row(manifest)
    task_rows = _task_rows(manifest)
    run_rows = _run_rows(manifest)
    escalation_row = _escalation_row(manifest)
    _assert_required_columns(models.ProjectRow, project_row)
    _assert_required_columns(models.TaskRow, task_rows[0])
    _assert_required_columns(models.TaskRunRow, run_rows[0])
    _assert_required_columns(models.HumanEscalationRow, escalation_row)
    _validate_in_memory_relationships(project_row, task_rows, run_rows, escalation_row)
    row_counts = {
        "projects": 1,
        "tasks": len(task_rows),
        "task_runs": len(run_rows),
        "human_escalations": 1,
        "model_runs": 0,
        "run_events": 0,
        "reviews": 0,
        "review_issues": 0,
        "verification_runs": 0,
        "training_examples": 0,
        "lessons": 0,
        "artifacts": 0,
        "milestones": 0,
        "pause_requests": 0,
        "workflow_checkpoints": 0,
        "workflow_writes": 0,
    }
    return ReconstructionPlan(
        manifest=manifest,
        manifest_hash=manifest_hash,
        task_rows=task_rows,
        run_rows=run_rows,
        project_row=project_row,
        escalation_row=escalation_row,
        row_counts=row_counts,
    )


def validate_reconstruction_preconditions(
    *,
    manifest_path: Path,
    database_url: str,
    confirm_database: str,
    runner: Any = subprocess.run,
) -> ReconstructionPlan:
    plan = load_reconstruction_plan(manifest_path)
    _validate_database_identity(database_url, confirm_database)
    engine = create_engine(database_url, future=True)
    try:
        _validate_database(engine, plan.manifest)
    finally:
        engine.dispose()
    _validate_backup(plan.manifest)
    notes = _validate_git(plan.manifest, runner=runner)
    notes.update(_validate_no_active_operations(runner))
    return ReconstructionPlan(**{**_plan_fields(plan), "notes": notes})


def dry_run_reconstruction(
    *,
    manifest_path: Path,
    database_url: str,
    confirm_database: str,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    plan = validate_reconstruction_preconditions(
        manifest_path=manifest_path,
        database_url=database_url,
        confirm_database=confirm_database,
        runner=runner,
    )
    return _report(plan, dry_run=True, transaction_result="not-run")


def import_reconstruction(
    *,
    manifest_path: Path,
    database_url: str,
    confirm_database: str,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    plan = validate_reconstruction_preconditions(
        manifest_path=manifest_path,
        database_url=database_url,
        confirm_database=confirm_database,
        runner=runner,
    )
    engine = create_engine(database_url, future=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory.begin() as session:
            _assert_campaign_empty(session)
            _insert_rows(session, plan)
        return _report(plan, dry_run=False, transaction_result="committed")
    finally:
        engine.dispose()


def _plan_fields(plan: ReconstructionPlan) -> dict[str, Any]:
    return {
        "manifest": plan.manifest,
        "manifest_hash": plan.manifest_hash,
        "task_rows": plan.task_rows,
        "run_rows": plan.run_rows,
        "project_row": plan.project_row,
        "escalation_row": plan.escalation_row,
        "row_counts": plan.row_counts,
    }


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def _load_manifest_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReconstructionError(f"manifest not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ReconstructionError(f"manifest is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ReconstructionError("manifest must be a JSON object")
    return data


def _validate_manifest_shape(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != SUPPORTED_MANIFEST_VERSION:
        raise ReconstructionError(
            f"unsupported reconstruction manifest version {manifest.get('schema_version')!r}"
        )
    if RECONSTRUCTION_MESSAGE not in manifest.get("statement", ""):
        raise ReconstructionError("manifest must state reconstructed-state provenance")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 10:
        raise ReconstructionError("manifest must contain exactly 10 reconstructed tasks")
    runs = manifest.get("ts109_runs")
    if not isinstance(runs, list) or len(runs) != 7:
        raise ReconstructionError("manifest must contain exactly 7 TS-109 runs")
    if manifest["project"].get("identity_provenance") != "EVIDENCED":
        raise ReconstructionError("project identity must be classified EVIDENCED")
    if [task.get("external_task_id") for task in tasks] != [
        f"TS-{100 + index}" for index in range(1, 11)
    ]:
        raise ReconstructionError("manifest task ids must be TS-101..TS-110 in order")
    for task in tasks:
        if task["external_task_id"] != TS110_SYNTHETIC_TASK_ID:
            if task.get("identity_provenance") != "EVIDENCED":
                raise ReconstructionError(f"{task['external_task_id']} identity must be EVIDENCED")
            if task.get("status") != TaskStatus.COMPLETE.value:
                raise ReconstructionError(
                    f"{task['external_task_id']} must reconstruct as COMPLETE"
                )
        if task["external_task_id"] not in ("TS-109", TS110_SYNTHETIC_TASK_ID):
            commit = task.get("integrated_commit")
            if not commit or len(commit) != 40:
                raise ReconstructionError(
                    f"{task['external_task_id']} must carry the full 40-character "
                    "evidenced integration SHA"
                )
    ts109 = [task for task in tasks if task.get("external_task_id") == "TS-109"][0]
    for run in runs:
        if run.get("task_id") != ts109["id"]:
            raise ReconstructionError("every reconstructed run must belong to TS-109")
        if run.get("starting_commit") != manifest["repository"]["expected_integration_sha"]:
            raise ReconstructionError("every reconstructed run must start at the integration SHA")
        if run.get("candidate_commit") is not None:
            raise ReconstructionError(
                "reconstructed TS-109 runs must keep candidate_commit omitted"
            )
        if run.get("run_number_provenance") != "DERIVED":
            raise ReconstructionError("TS-109 run numbers must be classified DERIVED")
        if run.get("field_provenance", {}).get("status") != "EVIDENCED":
            raise ReconstructionError("TS-109 run terminal states must be classified EVIDENCED")
    external_ids = [run["external_run_id"] for run in runs]
    if external_ids != sorted(external_ids):
        raise ReconstructionError("TS-109 run numbers must follow artifact chronology")
    for index, run in enumerate(runs, start=1):
        if run.get("run_number") != index:
            raise ReconstructionError("TS-109 run numbers must be deterministic 1..7")
    escalation = manifest.get("escalation") or {}
    if escalation.get("resolution") is not None or escalation.get("resolved_at") is not None:
        raise ReconstructionError("missing historical resolution values must stay omitted")
    if escalation.get("human_commit") is not None:
        raise ReconstructionError(
            "the pre-reconciliation escalation must keep human_commit omitted"
        )
    if escalation.get("status") != EscalationStatus.RESOLVED.value:
        raise ReconstructionError("escalation must reconstruct as RESOLVED")
    if escalation.get("resolution_intent") != EscalationIntent.COMPLETED_BY_HAND.value:
        raise ReconstructionError("escalation must reconstruct as COMPLETED_BY_HAND")


def _validate_synthetic_identity(manifest: dict[str, Any]) -> None:
    tasks = manifest["tasks"]
    synthetic_tasks = [task for task in tasks if task.get("identity_provenance") == "SYNTHETIC"]
    if (
        len(synthetic_tasks) != 1
        or synthetic_tasks[0]["external_task_id"] != TS110_SYNTHETIC_TASK_ID
    ):
        raise ReconstructionError("TS-110 identity must be the only SYNTHETIC identity")
    ts110 = synthetic_tasks[0]
    if ts110.get("status") != TaskStatus.READY.value:
        raise ReconstructionError("TS-110 must reconstruct as READY")
    recomputed = synthetic_ts110_uuid(manifest["project"]["id"])
    if str(recomputed) != ts110["id"]:
        raise ReconstructionError(
            f"TS-110 synthetic UUID is not the deterministic reconstruction: "
            f"manifest says {ts110['id']!r}, recomputed {str(recomputed)!r}"
        )
    declared = manifest.get("synthetic_identities") or []
    if len(declared) != 1 or declared[0].get("id") != str(recomputed):
        raise ReconstructionError("manifest synthetic_identities must match the deterministic UUID")
    if declared[0].get("classification") != "SYNTHETIC":
        raise ReconstructionError("TS-110 synthetic identity must be classified SYNTHETIC")


def _validate_backup(manifest: dict[str, Any]) -> None:
    backup = manifest["canonical_backup"]
    path = _resolve_path(backup["path"])
    if not path.exists():
        raise ReconstructionError(f"canonical backup is missing: {path}")
    digest = _sha256_file(path)
    if digest != backup["sha256"]:
        raise ReconstructionError("canonical backup checksum mismatch")
    metadata_path = _resolve_path(backup["metadata"])
    if not metadata_path.exists():
        raise ReconstructionError(f"canonical backup metadata is missing: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not metadata.get("verified_at"):
        raise ReconstructionError("canonical backup metadata is not verified")
    if metadata.get("sha256") != backup["sha256"]:
        raise ReconstructionError("canonical backup metadata checksum mismatch")
    if metadata.get("alembic_revision") != manifest["expected_alembic_head"]:
        raise ReconstructionError("canonical backup metadata revision mismatch")
    restore_checks = metadata.get("restore_checks") or {}
    if not restore_checks:
        raise ReconstructionError("canonical backup has no restore verification record")
    if restore_checks.get("alembic_revision") != manifest["expected_alembic_head"]:
        raise ReconstructionError("canonical backup restore verification did not confirm the head")


def _validate_git(manifest: dict[str, Any], *, runner: Any) -> dict[str, Any]:
    repo = manifest["repository"]
    path = _resolve_path(repo["path"])
    if not path.is_dir():
        raise ReconstructionError(f"TraceStack repository is missing: {path}")
    integration = _git(path, "rev-parse", repo["integration_ref"], runner=runner)
    if integration != repo["expected_integration_sha"]:
        raise ReconstructionError(
            f"agent/integration SHA {integration!r} does not match "
            f"{repo['expected_integration_sha']!r}"
        )
    human = repo["human_commit"]
    _git(path, "cat-file", "-e", f"{human}^{{commit}}", runner=runner)
    ancestor = runner(
        ("git", "merge-base", "--is-ancestor", human, repo["integration_ref"]),
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestor.returncode == 0:
        raise ReconstructionError("human commit is already integrated")
    for task in manifest["tasks"]:
        commit = task.get("integrated_commit")
        if commit:
            _git(path, "cat-file", "-e", f"{commit}^{{commit}}", runner=runner)
    refs_out = _git(
        path,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/agent/TS-109*",
        runner=runner,
    )
    ref_names = [line for line in refs_out.splitlines() if line]
    expected_suffixes = {f"run{number}" for number in range(1, 8)}
    suffixes = {name.rsplit("-", 1)[-1] for name in ref_names}
    if not suffixes <= expected_suffixes or len(ref_names) != len(suffixes):
        raise ReconstructionError(
            f"expected exactly the seven surviving TS-109 run refs, found: {ref_names}"
        )
    if suffixes != expected_suffixes:
        raise ReconstructionError(
            f"TS-109 run refs {sorted(suffixes)} do not cover run1..run7"
        )
    return {
        "integration_sha_observed": integration,
        "ts109_run_refs_observed": sorted(ref_names),
    }


def _validate_no_active_operations(runner: Any) -> dict[str, Any]:
    """Precondition 15: no scheduler/run/reconciliation operation is active.

    The application exposes in-flight work two ways: durable rows in the
    campaign tables (refused separately by the emptiness gate) and worker
    containers running under the ``orchestrator-worker-`` name prefix. If the
    container engine cannot be consulted the importer refuses, because an
    unverified "nothing is running" is not a passed precondition.
    """
    try:
        completed = runner(
            ("docker", "ps", "--format", "{{.Names}}"),
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        raise ReconstructionError(
            "cannot verify that no worker is active: docker client is unavailable"
        ) from None
    if completed.returncode != 0:
        raise ReconstructionError(
            "cannot verify that no worker is active: docker ps failed"
        )
    names = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    active = [name for name in names if name.startswith("orchestrator-worker-")]
    if active:
        raise ReconstructionError(f"worker operation appears active: {active}")
    return {"active_operations": "none observed"}


def _validate_database_identity(database_url: str, confirm_database: str) -> None:
    parsed = make_url(database_url)
    if parsed.database != confirm_database:
        raise ReconstructionError(
            f"target database {parsed.database!r} does not match confirmation {confirm_database!r}"
        )


def _required_alembic_head(manifest: dict[str, Any]) -> str:
    """The head the *target database* has to be at before a row is inserted.

    Distinct from ``expected_alembic_head``, which is the revision the
    canonical backup was *taken* at. That one is a historical fact about a
    checksummed artifact and must never move; this one is a statement about
    the schema the code needs in order to write the columns it now writes, so
    it advances with every migration. Collapsing the two would mean either
    re-cutting a sealed backup each time a migration lands, or -- what
    happened when a migration was added without noticing -- silently refusing
    every reconstruction because the code had moved past the artifact.
    """
    return manifest.get("required_alembic_head") or manifest["expected_alembic_head"]


def _validate_database(engine: Engine, manifest: dict[str, Any]) -> None:
    required = _required_alembic_head(manifest)
    with engine.connect() as connection:
        try:
            revision = connection.execute(text("select version_num from alembic_version")).scalar()
        except Exception:
            raise ReconstructionError(
                "target database does not expose an alembic_version row; "
                "it is not at the expected migration head"
            ) from None
        if revision != required:
            raise ReconstructionError(
                f"database migration head {revision!r} does not match {required!r}"
            )
        _assert_campaign_empty_session(connection)


def _assert_campaign_empty(session: Session) -> None:
    for table in EMPTY_TABLES:
        count = session.scalar(select(func.count()).select_from(table))
        if count:
            raise ReconstructionError(
                f"campaign table {table.__tablename__} is not empty ({count} row(s)); "
                "reconstruction refuses against existing or partial campaign state"
            )


def _assert_campaign_empty_session(connection: Any) -> None:
    for table in EMPTY_TABLES:
        count = connection.execute(text(f"select count(*) from {table.__tablename__}")).scalar()
        if count:
            raise ReconstructionError(
                f"campaign table {table.__tablename__} is not empty ({count} row(s)); "
                "reconstruction refuses against existing or partial campaign state"
            )


def _validate_artifacts(manifest: dict[str, Any]) -> None:
    for run in manifest["ts109_runs"]:
        evidence = run.get("evidence") or {}
        for path in evidence.values():
            artifact = _resolve_path(path)
            if not artifact.exists():
                raise ReconstructionError(f"referenced artifact is missing: {artifact}")
        outcome_path = _resolve_path(evidence["outcome"])
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        comparisons = {
            "id": outcome.get("run_id"),
            "external_run_id": outcome.get("external_run_id"),
            "task_id": outcome.get("task_id"),
            "status": outcome.get("run_status"),
            "attempt_number": outcome.get("attempts"),
            "failure_reason": outcome.get("failure_reason"),
            "starting_commit": outcome.get("starting_commit"),
            "candidate_commit": outcome.get("candidate_commit"),
            "review_cycle": outcome.get("review_cycles"),
        }
        for key, artifact_value in comparisons.items():
            if run[key] != artifact_value:
                raise ReconstructionError(
                    f"artifact mismatch for {run['external_run_id']}:{key} "
                    f"(manifest {run[key]!r} != artifact {artifact_value!r})"
                )
        fix_loop = json.loads(_resolve_path(evidence["fix_loop"]).read_text(encoding="utf-8"))
        if fix_loop.get("task_run_id") != run["id"]:
            raise ReconstructionError(
                f"artifact mismatch for {run['external_run_id']}: fix-loop task_run_id"
            )
        if fix_loop.get("failure_reason") != run["failure_reason"]:
            raise ReconstructionError(
                f"artifact mismatch for {run['external_run_id']}: fix-loop failure_reason"
            )
    escalation = manifest["escalation"]
    if not escalation.get("summary_source"):
        raise ReconstructionError(
            "escalation summary is required by the schema but has no evidenced source; "
            "refusing to invent history"
        )
    summary_path = _resolve_path(escalation["summary_source"])
    if not summary_path.is_file() or not summary_path.read_text(encoding="utf-8").strip():
        raise ReconstructionError(
            f"referenced escalation summary is missing or empty: {summary_path}"
        )
    final_run = manifest["ts109_runs"][-1]
    if escalation["task_run_id"] != final_run["id"]:
        raise ReconstructionError("escalation must be attached to the chronologically final run")
    if escalation["reason"] != final_run["failure_reason"]:
        raise ReconstructionError("escalation reason must match the final run artifact")
    fix_loop = json.loads(
        _resolve_path(final_run["evidence"]["fix_loop"]).read_text(encoding="utf-8")
    )
    if fix_loop.get("outcome") != "ESCALATED":
        raise ReconstructionError("final run artifact must show an ESCALATED outcome")
    if fix_loop.get("escalation_id") != escalation["id"]:
        raise ReconstructionError(
            f"escalation id mismatch for artifact: fix-loop says "
            f"{fix_loop.get('escalation_id')!r}"
        )


def _validate_task_definitions(manifest: dict[str, Any]) -> None:
    source = _resolve_path(manifest["repository"]["path"]) / "build.tasks.yaml"
    parsed = load_manifest(source)
    expected_ids = manifest["task_definition_source"]["expected_task_ids"]
    declared_ids = [task["external_task_id"] for task in manifest["tasks"]]
    actual_ids = [task.external_id for task in parsed.tasks]
    if actual_ids != expected_ids or declared_ids != expected_ids:
        raise ReconstructionError("task definition mismatch: task ids differ")
    by_id = {task.external_id: task for task in parsed.tasks}
    for task in manifest["tasks"]:
        defined = by_id[task["external_task_id"]]
        limits = defined.limits
        for name, expected in SHARED_LIMITS.items():
            actual = getattr(limits, name)
            if actual != expected:
                raise ReconstructionError(
                    f"task definition mismatch: {task['external_task_id']}.{name} "
                    f"is {actual}, expected {expected}"
                )


def _assert_required_columns(row_class: Any, row: dict[str, Any]) -> None:
    """Fail rather than invent: every NOT NULL column must carry a value whose
    provenance is evidenced or derived by a documented rule. A None at a NOT
    NULL column means the history could not support the row and the importer
    must stop instead of fabricating."""
    mapper = row_class.__mapper__
    for column in mapper.columns:
        if column.nullable or column.default is not None or column.server_default is not None:
            continue
        if row.get(column.key) is None:
            raise ReconstructionError(
                f"cannot reconstruct {row_class.__tablename__}.{column.key}: "
                "the schema requires a non-null value that evidence does not supply; "
                "refusing to invent history"
            )


def _project_row(manifest: dict[str, Any]) -> dict[str, Any]:
    project = manifest["project"]
    parsed = load_manifest(_resolve_path(manifest["repository"]["path"]) / "build.tasks.yaml")
    return {
        "id": uuid.UUID(project["id"]),
        "name": project["name"],
        "external_project_id": project["external_project_id"],
        "repository_path": str(_resolve_path(project["repository_path"])),
        "default_branch": manifest["repository"]["default_branch"],
        "worker_profile": WorkerProfile.NODE,
        "status": ProjectStatus.REGISTERED,
        "protected_paths": list(parsed.protected_paths),
        "sensitive_path_exceptions": list(parsed.sensitive_path_exceptions),
        "generated_path_exceptions": list(parsed.generated_path_exceptions),
        "dependency_paths": list(parsed.dependency_paths),
        "approval_gated_categories": (
            list(parsed.approval_gated_categories)
            if parsed.approval_gated_categories is not None
            else None
        ),
        "verification_profile": {
            "build": list(parsed.verification.build),
            "lint": list(parsed.verification.lint),
            "tests": list(parsed.verification.tests),
            "security": list(parsed.verification.security),
        },
        "milestone_interval": parsed.milestone_interval,
    }


def _task_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    parsed = load_manifest(_resolve_path(manifest["repository"]["path"]) / "build.tasks.yaml")
    by_id = {task.external_id: task for task in parsed.tasks}
    return [
        _task_row(manifest, task, by_id[task["external_task_id"]])
        for task in manifest["tasks"]
    ]


def _task_row(
    manifest: dict[str, Any],
    task: dict[str, Any],
    defined: ManifestTask,
) -> dict[str, Any]:
    return {
        "id": uuid.UUID(task["id"]),
        "project_id": uuid.UUID(manifest["project"]["id"]),
        "external_task_id": task["external_task_id"],
        "title": defined.title,
        "section": task["section"],
        "instructions": defined.instructions,
        "complexity": Complexity(defined.complexity),
        "risk_level": RiskLevel(defined.risk_level),
        "status": TaskStatus(task["status"]),
        "depends_on": list(defined.depends_on),
        "verify_commands": list(defined.verify_commands),
        "files_to_inspect": list(defined.files_to_inspect),
        "files_to_modify": list(defined.files_to_modify),
        "files_to_create": list(defined.files_to_create),
        "max_attempts": defined.limits.max_attempts,
        "max_review_cycles": defined.limits.max_review_cycles,
        "max_runtime_minutes": defined.limits.max_runtime_minutes,
        "max_files_changed": defined.limits.max_files_changed,
        "max_diff_lines": defined.limits.max_diff_lines,
        "unintegrated_commit": None,
    }


def _run_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": uuid.UUID(run["id"]),
            "task_id": uuid.UUID(run["task_id"]),
            "run_number": run["run_number"],
            "attempt_number": run["attempt_number"],
            "review_cycle": run["review_cycle"],
            "status": RunStatus(run["status"]),
            "external_run_id": run["external_run_id"],
            "starting_commit": run["starting_commit"],
            "candidate_commit": run["candidate_commit"],
            "failure_reason": run["failure_reason"],
            "artifact_path": run["artifact_path"],
            "active_runtime_ms": 0,
            "execution_generation": 0,
        }
        for run in manifest["ts109_runs"]
    ]


def _escalation_row(manifest: dict[str, Any]) -> dict[str, Any]:
    escalation = manifest["escalation"]
    reason = escalation["reason"]
    return {
        "id": uuid.UUID(escalation["id"]),
        "task_id": uuid.UUID(escalation["task_id"]),
        "task_run_id": uuid.UUID(escalation["task_run_id"]),
        "reason": reason,
        "summary": _resolve_path(escalation["summary_source"]).read_text(encoding="utf-8"),
        "options": [option.describe() for option in run_escalation_options(FailureReason(reason))],
        "status": EscalationStatus.RESOLVED,
        "resolution": None,
        "resolution_intent": EscalationIntent.COMPLETED_BY_HAND.value,
        "human_commit": None,
        "resolved_at": None,
    }


def _validate_in_memory_relationships(
    project: dict[str, Any],
    tasks: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    escalation: dict[str, Any],
) -> None:
    task_ids = {task["id"] for task in tasks}
    if any(task["project_id"] != project["id"] for task in tasks):
        raise ReconstructionError("task/project relationship mismatch")
    if any(run["task_id"] not in task_ids for run in runs):
        raise ReconstructionError("run/task relationship mismatch")
    if escalation["task_id"] not in task_ids:
        raise ReconstructionError("escalation/task relationship mismatch")
    if escalation["task_run_id"] not in {run["id"] for run in runs}:
        raise ReconstructionError("escalation/run relationship mismatch")


def _insert_rows(session: Session, plan: ReconstructionPlan) -> None:
    session.add(models.ProjectRow(**plan.project_row))
    session.flush()
    for row in plan.task_rows:
        session.add(models.TaskRow(**row))
    session.flush()
    for row in plan.run_rows:
        session.add(models.TaskRunRow(**row))
    session.flush()
    session.add(models.HumanEscalationRow(**plan.escalation_row))
    session.flush()


def _report(plan: ReconstructionPlan, *, dry_run: bool, transaction_result: str) -> dict[str, Any]:
    report = {
        "notice": RECONSTRUCTION_MESSAGE,
        "dry_run": dry_run,
        "manifest_version": plan.manifest["schema_version"],
        "manifest_hash": plan.manifest_hash,
        "executed_at": dt.datetime.now(dt.UTC).isoformat(),
        "importer_revision": _build_meta.SOURCE_REVISION,
        "project_id": str(plan.project_row["id"]),
        "task_ids": {row["external_task_id"]: str(row["id"]) for row in plan.task_rows},
        "synthetic_identities": plan.manifest["synthetic_identities"],
        "run_ids": [str(row["id"]) for row in plan.run_rows],
        "escalation_id": str(plan.escalation_row["id"]),
        "inserted_row_counts": plan.row_counts,
        "omitted_history": plan.manifest["omitted_history"],
        "integration_sha_observed": plan.notes.get(
            "integration_sha_observed", plan.manifest["repository"]["expected_integration_sha"]
        ),
        "backup_checksum_verified": plan.manifest["canonical_backup"]["sha256"],
        "transaction_result": transaction_result,
        "proposed_rows": plan.row_counts if dry_run else None,
    }
    if "active_operations" in plan.notes:
        report["active_operations"] = plan.notes["active_operations"]
    return report


def _git(path: Path, *args: str, runner: Any) -> str:
    verb = args[0]
    if verb not in READ_ONLY_GIT_COMMANDS:
        raise ReconstructionError(f"importer may not run git {verb!r}: not a read-only command")
    completed = runner(
        ("git", *args),
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ReconstructionError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import reconstructed TraceStack campaign state")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--confirm-database", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-file", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            report = dry_run_reconstruction(
                manifest_path=args.manifest,
                database_url=args.database_url,
                confirm_database=args.confirm_database,
            )
        else:
            report = import_reconstruction(
                manifest_path=args.manifest,
                database_url=args.database_url,
                confirm_database=args.confirm_database,
            )
    except ReconstructionError as exc:
        print(f"reconstruction error: {exc}", file=os.sys.stderr)
        return 1
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.report_file is not None:
        args.report_file.write_text(rendered + "\n", encoding="utf-8")
    return 0
