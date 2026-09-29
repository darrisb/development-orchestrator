"""Concern 73: human completion must preserve integration lineage.

When an operator resolves COMPLETED_BY_HAND with a human-produced Git commit,
the orchestrator must integrate that commit through the canonical mechanism
before marking the task COMPLETE. Downstream dependent tasks must then start
from a baseline containing the human work.

The defect this pins: before concern 73, COMPLETED_BY_HAND marked a task
COMPLETE without integrating anything, so downstream tasks started from a
baseline that silently omitted the human work.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.enums import (
    Complexity,
    EscalationStatus,
    FailureReason,
    TaskStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.escalation import (
    EscalationIntent,
    EscalationOption,
)
from apps.orchestrator.domain.git import INTEGRATION_BRANCH
from apps.orchestrator.domain.models import (
    HumanEscalation,
    Project,
    Task,
    TaskLimits,
)
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import (
    EscalationRepository,
    ProjectRepository,
    TaskRepository,
)
from apps.orchestrator.services.errors import EntityConflict, EntityNotFound
from apps.orchestrator.services.git_errors import MergeConflict
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import (
    prepare_workspace,
    repository_service,
)
from apps.orchestrator.workflow.resolution import (
    apply_escalation_answer,
)
from tests.conftest import run_git

pytestmark = pytest.mark.integration


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=root / "data",
        worktree_root=root / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
        worker_timeout_seconds=120,
    )


def _factory(root: Path):
    engine = create_db_engine(f"sqlite:///{root / 'concern73.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _repository(root: Path) -> Path:
    repo = root / "human-commit-project"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "feature.py").write_text(
        "def base():\n    return 0\n", encoding="utf-8"
    )
    run_git(repo, "init", "--initial-branch=main", "--quiet")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "initial")
    return repo


def _project(session: Session, repository: Path) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="HumanCommit",
            external_project_id="human-commit-project",
            repository_path=str(repository),
            default_branch="main",
            worker_profile=WorkerProfile.PYTHON,
            verification=VerificationProfile(),
        )
    )


def _task(
    session: Session,
    project: Project,
    external_id: str,
    *,
    depends_on: tuple[str, ...] = (),
) -> Task:
    task = TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id=external_id,
            title=f"Task {external_id}",
            instructions=f"Implement {external_id}.",
            complexity=Complexity.LOW,
            depends_on=list(depends_on),
            files_to_modify=["src/feature.py"],
            limits=TaskLimits(
                max_attempts=3, max_review_cycles=2, max_files_changed=1, max_diff_lines=50
            ),
        )
    )
    return TaskRepository(session).transition(task.id, TaskStatus.READY)


def _make_escalation(
    session: Session,
    task: Task,
    *,
    task_run_id=None,
) -> HumanEscalation:
    options = [
        EscalationOption(
            "A",
            EscalationIntent.RETRY_TASK,
            "Run the task again.",
        ),
        EscalationOption(
            "B",
            EscalationIntent.COMPLETED_BY_HAND,
            "Fix by hand and mark complete.",
        ),
        EscalationOption(
            "C",
            EscalationIntent.ABANDON_TASK,
            "Abandon the task.",
        ),
    ]
    return EscalationRepository(session).add(
        HumanEscalation(
            task_id=task.id,
            task_run_id=task_run_id,
            reason=FailureReason.RETRY_EXHAUSTED.value,
            summary="All attempts exhausted.",
            options=options,
        )
    )


def _human_commit(repository: Path, content: str, message: str) -> str:
    (repository / "src" / "feature.py").write_text(content, encoding="utf-8")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", message)
    return run_git(repository, "rev-parse", "HEAD").strip()


# ------------------------------------------------------------------ tests


def test_human_commit_with_valid_sha_integrates_and_completes(tmp_path: Path):
    """Requirement 1: human completion with a valid commit integrates it."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-H1")
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        escalation = _make_escalation(session, task)
        project_id = project.id
        task_id = task.id
        escalation_id = escalation.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef human_work():\n    return 42\n",
        "TS-H1: human implementation",
    )

    with factory.begin() as session:
        intent = EscalationIntent.COMPLETED_BY_HAND
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Implemented by hand.",
            intent=intent,
            human_commit=human_sha,
            settings=settings,
        )

    with factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task.status is TaskStatus.COMPLETE

        project = ProjectRepository(session).get(project_id)
        git = repository_service(project, settings=settings)
        assert git.contains_commit(human_sha, ref=INTEGRATION_BRANCH), (
            "the human commit was not integrated into the baseline"
        )

        escalation = EscalationRepository(session).get(escalation_id)
        assert escalation.status is EscalationStatus.RESOLVED
        assert escalation.human_commit == human_sha
        assert escalation.resolution_intent is EscalationIntent.COMPLETED_BY_HAND


def test_dependent_task_starts_from_baseline_containing_human_work(tmp_path: Path):
    """Requirement 3: downstream tasks see the human commit in their starting tree."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task_a = _task(session, project, "TS-A")
        TaskRepository(session).transition(task_a.id, TaskStatus.HUMAN_REVIEW)
        task_b = _task(session, project, "TS-B", depends_on=("TS-A",))
        escalation = _make_escalation(session, task_a)
        task_b_id = task_b.id
        escalation_id = escalation.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef human_feature():\n    return 99\n",
        "TS-A: human implementation",
    )

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done by hand.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=human_sha,
            settings=settings,
        )

    with factory.begin() as session:
        run_b = create_run(session, task_b_id)
        workspace = prepare_workspace(session, run_b.id, settings=settings)

    source = (Path(workspace.path) / "src" / "feature.py").read_text()
    assert "def human_feature(" in source, (
        "the dependent task's starting tree does not contain the human work"
    )


def test_human_commit_provenance_is_distinguishable_from_model_candidate(
    tmp_path: Path,
):
    """Requirement 4: human commit is stored on the escalation, not on the run."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-P1")
        run = create_run(session, task.id)
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        escalation = _make_escalation(session, task, task_run_id=run.id)
        escalation_id = escalation.id
        run_id = run.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef provenance():\n    return 1\n",
        "human work",
    )

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=human_sha,
            settings=settings,
        )

    with factory() as session:
        from apps.orchestrator.repositories import TaskRunRepository

        run = TaskRunRepository(session).get(run_id)
        assert run.candidate_commit is None, (
            "human commit was falsely stored as model candidate provenance"
        )

        escalation = EscalationRepository(session).get(escalation_id)
        assert escalation.human_commit == human_sha


def test_nonexistent_commit_fails_closed(tmp_path: Path):
    """Requirement 5: invalid commit does not produce false COMPLETE."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-N1")
        run = create_run(session, task.id)
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        escalation = _make_escalation(session, task, task_run_id=run.id)
        task_id = task.id
        escalation_id = escalation.id

    fake_sha = "a" * 40

    with factory.begin() as session, pytest.raises(EntityNotFound):
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=fake_sha,
            settings=settings,
        )

    with factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task.status is not TaskStatus.COMPLETE, (
            "task was marked COMPLETE despite invalid commit"
        )
        escalation = EscalationRepository(session).get(escalation_id)
        assert escalation.status is EscalationStatus.OPEN, (
            "escalation was resolved despite failed integration"
        )


def test_merge_conflict_fails_closed(tmp_path: Path):
    """Requirement 7: integration conflict does not produce false COMPLETE."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task_a = _task(session, project, "TS-M1")
        task_b = _task(session, project, "TS-M2")

        run_a = create_run(session, task_a.id)
        workspace_a = prepare_workspace(session, run_a.id, settings=settings)
        (Path(workspace_a.path) / "src" / "feature.py").write_text(
            "def base():\n    return 1\n", encoding="utf-8"
        )
        git_a = repository_service(project, settings=settings).for_worktree(
            Path(workspace_a.path)
        )
        git_a.commit("task A work")

        from apps.orchestrator.services.delivery import deliver_escalated_candidate

        TaskRepository(session).transition(task_a.id, TaskStatus.HUMAN_REVIEW)
        deliver_escalated_candidate(session, workspace_a, settings=settings)

        TaskRepository(session).transition(task_b.id, TaskStatus.HUMAN_REVIEW)
        escalation_b = _make_escalation(session, task_b, task_run_id=None)
        task_b_id = task_b.id
        escalation_b_id = escalation_b.id

    conflict_sha = _human_commit(
        repository,
        "def base():\n    return 2\n\ndef conflict():\n    return 3\n",
        "conflicting human change",
    )

    with factory.begin() as session, pytest.raises((MergeConflict, EntityConflict)):
        apply_escalation_answer(
            session,
            escalation_b_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=conflict_sha,
            settings=settings,
        )

    with factory() as session:
        task_b = TaskRepository(session).get(task_b_id)
        assert task_b.status is not TaskStatus.COMPLETE


def test_explicit_no_code_completion_remains_supported(tmp_path: Path):
    """Requirement 8: COMPLETED_BY_HAND without a commit still works."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-NC")
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        escalation = _make_escalation(session, task)
        task_id = task.id
        escalation_id = escalation.id

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="No code needed.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=None,
            settings=settings,
        )

    with factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task.status is TaskStatus.COMPLETE

        escalation = EscalationRepository(session).get(escalation_id)
        assert escalation.human_commit is None


def test_human_commit_on_non_completed_by_hand_intent_is_rejected(tmp_path: Path):
    """Requirement 9: human_commit is only valid with COMPLETED_BY_HAND."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-IR")
        escalation = _make_escalation(session, task)
        escalation_id = escalation.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef extra():\n    return 1\n",
        "extra work",
    )

    match_msg = "only valid with COMPLETED_BY_HAND"
    with factory.begin() as session, pytest.raises(EntityConflict, match=match_msg):
        apply_escalation_answer(
                session,
                escalation_id,
                resolution="Retry.",
                intent=EscalationIntent.RETRY_TASK,
                human_commit=human_sha,
                settings=settings,
            )


def test_duplicate_resolution_is_rejected(tmp_path: Path):
    """Requirement 10: replaying a resolution is deterministically rejected."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-D1")
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        escalation = _make_escalation(session, task)
        escalation_id = escalation.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef dup():\n    return 1\n",
        "work",
    )

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=human_sha,
            settings=settings,
        )

    with factory.begin() as session, pytest.raises(EntityConflict, match="already"):
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done again.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=human_sha,
            settings=settings,
        )


def test_historical_failed_run_remains_unchanged(tmp_path: Path):
    """Requirement 11: historical TaskRuns are not mutated."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-HR")
        run = create_run(session, task.id)
        from apps.orchestrator.repositories import TaskRunRepository

        TaskRunRepository(session).update_fields(
            run.id, status="FAILED", failure_reason="RETRY_EXHAUSTED"
        )
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        run_id = run.id
        escalation = _make_escalation(session, task, task_run_id=run.id)
        escalation_id = escalation.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef hist():\n    return 1\n",
        "work",
    )

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=human_sha,
            settings=settings,
        )

    with factory() as session:
        from apps.orchestrator.repositories import TaskRunRepository

        run = TaskRunRepository(session).get(run_id)
        assert run.status.value == "FAILED"
        assert run.failure_reason == "RETRY_EXHAUSTED"
        assert run.candidate_commit is None


def test_no_fake_automated_candidate_is_recorded(tmp_path: Path):
    """Requirement 12: human commit is not stored as candidate_commit."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-FC")
        run = create_run(session, task.id)
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        escalation = _make_escalation(session, task, task_run_id=run.id)
        run_id = run.id
        escalation_id = escalation.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef fake():\n    return 1\n",
        "work",
    )

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=human_sha,
            settings=settings,
        )

    with factory() as session:
        from apps.orchestrator.repositories import TaskRunRepository

        run = TaskRunRepository(session).get(run_id)
        assert run.candidate_commit is None, (
            "human commit was falsely recorded as automated candidate"
        )


def test_dependency_guard_correct_for_automated_candidates(tmp_path: Path):
    """Requirement 13: existing dependency guard still works for model candidates."""
    repository = _repository(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task_a = _task(session, project, "TS-DG1")
        task_b = _task(session, project, "TS-DG2", depends_on=("TS-DG1",))

        TaskRepository(session).record_integration(
            task_a.id, unintegrated_commit="abc123"
        )
        task_b_id = task_b.id

    with factory() as session:
        from apps.orchestrator.services.workspace import _assert_dependencies_integrated

        task_b_obj = TaskRepository(session).get(task_b_id)
        with pytest.raises(EntityConflict, match="not in the integration baseline"):
            _assert_dependencies_integrated(session, task_b_obj)


def test_retry_task_and_abandon_unchanged(tmp_path: Path):
    """Requirement 14: RETRY_TASK and ABANDON_TASK remain unchanged."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task_retry = _task(session, project, "TS-RT")
        esc_retry = _make_escalation(session, task_retry)
        task_abandon = _task(session, project, "TS-AB")
        esc_abandon = _make_escalation(session, task_abandon)
        task_retry_id = task_retry.id
        task_abandon_id = task_abandon.id
        esc_retry_id = esc_retry.id
        esc_abandon_id = esc_abandon.id

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            esc_retry_id,
            resolution="Retry.",
            intent=EscalationIntent.RETRY_TASK,
            settings=settings,
        )

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            esc_abandon_id,
            resolution="Abandon.",
            intent=EscalationIntent.ABANDON_TASK,
            settings=settings,
        )

    with factory() as session:
        task_retry = TaskRepository(session).get(task_retry_id)
        assert task_retry.status is TaskStatus.READY

        task_abandon = TaskRepository(session).get(task_abandon_id)
        assert task_abandon.status is TaskStatus.FAILED


def test_already_integrated_human_commit_is_idempotent(tmp_path: Path):
    """If the human commit is already in the baseline, integration is a no-op."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-AI")
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        project_id = project.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef already():\n    return 1\n",
        "work",
    )

    with factory() as session:
        project = ProjectRepository(session).get(project_id)
        git = repository_service(project, settings=settings)
        git.force_branch(INTEGRATION_BRANCH, human_sha)

    with factory.begin() as session:
        escalation = _make_escalation(session, TaskRepository(session).get(task.id))
        escalation_id = escalation.id

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=human_sha,
            settings=settings,
        )

    with factory() as session:
        task = TaskRepository(session).get(task.id)
        assert task.status is TaskStatus.COMPLETE
        assert task.unintegrated_commit is None


# ---------------------------------------------------------------------------
# Reconciliation tests (for historical COMPLETED_BY_HAND escalations)
# ---------------------------------------------------------------------------


def test_reconciliation_integrates_human_commit(tmp_path: Path):
    """Requirement 1: already-resolved COMPLETED_BY_HAND + valid commit reconciles."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-RC1")
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        escalation = _make_escalation(session, task)
        escalation_id = escalation.id
        task_id = task.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef reconciled():\n    return 1\n",
        "reconciled work",
    )

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=None,
            settings=settings,
        )

    with factory.begin() as session:
        from apps.orchestrator.services.reviews import reconcile_human_commit

        reconcile_human_commit(session, escalation_id, human_commit=human_sha)

    with factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task.status is TaskStatus.COMPLETE

        project = ProjectRepository(session).get(project.id)
        git = repository_service(project, settings=settings)
        assert git.contains_commit(human_sha, ref=INTEGRATION_BRANCH)

        escalation = EscalationRepository(session).get(escalation_id)
        assert escalation.human_commit == human_sha


def test_reconciliation_records_human_commit(tmp_path: Path):
    """Requirement 2: human_commit is durably recorded."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-RC2")
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        escalation = _make_escalation(session, task)
        escalation_id = escalation.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef recorded():\n    return 1\n",
        "recorded work",
    )

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=None,
            settings=settings,
        )

    with factory.begin() as session:
        from apps.orchestrator.services.reviews import reconcile_human_commit

        reconcile_human_commit(session, escalation_id, human_commit=human_sha)

    with factory() as session:
        escalation = EscalationRepository(session).get(escalation_id)
        assert escalation.human_commit == human_sha
        assert escalation.status is EscalationStatus.RESOLVED
        assert escalation.resolution_intent is EscalationIntent.COMPLETED_BY_HAND


def test_reconciliation_preserves_task_run_history(tmp_path: Path):
    """Requirement 4: historical TaskRuns unchanged."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-RC4")
        run = create_run(session, task.id)
        from apps.orchestrator.repositories import TaskRunRepository

        TaskRunRepository(session).update_fields(
            run.id, status="FAILED", failure_reason="RETRY_EXHAUSTED"
        )
        run_id = run.id
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        escalation = _make_escalation(session, task, task_run_id=run.id)
        escalation_id = escalation.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef preserved():\n    return 1\n",
        "preserved work",
    )

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=None,
            settings=settings,
        )

    with factory.begin() as session:
        from apps.orchestrator.services.reviews import reconcile_human_commit

        reconcile_human_commit(session, escalation_id, human_commit=human_sha)

    with factory() as session:
        from apps.orchestrator.repositories import TaskRunRepository

        run = TaskRunRepository(session).get(run_id)
        assert run.status.value == "FAILED"
        assert run.failure_reason == "RETRY_EXHAUSTED"
        assert run.candidate_commit is None


def test_reconciliation_rejects_invalid_commit(tmp_path: Path):
    """Requirement 7: invalid commit fails closed."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-RC7")
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        escalation = _make_escalation(session, task)
        escalation_id = escalation.id
        task_id = task.id

    fake_sha = "a" * 40

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=None,
            settings=settings,
        )

    with factory.begin() as session:
        from apps.orchestrator.services.reviews import reconcile_human_commit

        with pytest.raises(EntityNotFound):
            reconcile_human_commit(session, escalation_id, human_commit=fake_sha)

    with factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task.status is TaskStatus.COMPLETE

        escalation = EscalationRepository(session).get(escalation_id)
        assert escalation.human_commit is None


def test_reconciliation_rejects_non_completed_by_hand(tmp_path: Path):
    """Requirement 10: wrong escalation intent/state rejected."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-RC10")
        escalation = _make_escalation(session, task)
        escalation_id = escalation.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef rejected():\n    return 1\n",
        "rejected work",
    )

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Retry.",
            intent=EscalationIntent.RETRY_TASK,
            settings=settings,
        )

    with factory.begin() as session:
        from apps.orchestrator.services.reviews import reconcile_human_commit

        with pytest.raises(EntityConflict, match="not COMPLETED_BY_HAND"):
            reconcile_human_commit(session, escalation_id, human_commit=human_sha)


def test_reconciliation_rejects_already_reconciled(tmp_path: Path):
    """Requirement 9: replay behavior matches documented contract."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task = _task(session, project, "TS-RC9")
        TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
        escalation = _make_escalation(session, task)
        escalation_id = escalation.id

    human_sha = _human_commit(
        repository,
        "def base():\n    return 0\n\ndef reconciled():\n    return 1\n",
        "reconciled work",
    )

    with factory.begin() as session:
        apply_escalation_answer(
            session,
            escalation_id,
            resolution="Done.",
            intent=EscalationIntent.COMPLETED_BY_HAND,
            human_commit=None,
            settings=settings,
        )

    with factory.begin() as session:
        from apps.orchestrator.services.reviews import reconcile_human_commit

        reconcile_human_commit(session, escalation_id, human_commit=human_sha)

    with factory.begin() as session:
        from apps.orchestrator.services.reviews import reconcile_human_commit

        with pytest.raises(EntityConflict, match="already has human_commit"):
            reconcile_human_commit(session, escalation_id, human_commit=human_sha)
