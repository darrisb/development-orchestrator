"""Phase M acceptance exercise against a small, disposable real repository.

This is intentionally broader than the feature-focused integration tests. It
runs ten low-risk tasks through the complete graph and also exercises the
failure and recovery paths Phase M requires before larger repositories are
trusted. Models are scripted so the exercise is deterministic; Git, SQLite,
worktrees, command processes, artifacts, checkpoints, and service boundaries
are real.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.enums import (
    Complexity,
    FailureReason,
    RunStatus,
    TaskStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.models import Project, Task, TaskLimits
from apps.orchestrator.repositories import (
    EscalationRepository,
    ProjectRepository,
    TaskRepository,
    TaskRunRepository,
    VerificationRunRepository,
)
from apps.orchestrator.services.pauses import pause_task
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.worktrees import census
from apps.orchestrator.workflow import WorkflowRunner
from tests.conftest import run_git
from tests.integration.test_fix_loop import (
    BROKEN,
    REVIEWED,
    WORKING,
    ScriptedModel,
    _code,
    _review,
    reviewer,
)

pytestmark = pytest.mark.integration


def _make_repository(root: Path) -> Path:
    repository = root / "phase-m-project"
    (repository / "src").mkdir(parents=True)
    (repository / "tools").mkdir()
    for number in range(1, 13):
        (repository / "src" / f"task_{number:02}.py").write_text(
            "def transform(value):\n    pass\n", encoding="utf-8"
        )
    (repository / "tools" / "verify.py").write_text(
        """\
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
if "return None" in source or "pass" in source:
    print(f"{sys.argv[1]}: implementation is incomplete", file=sys.stderr)
    raise SystemExit(1)
print(f"{sys.argv[1]}: verified")
""",
        encoding="utf-8",
    )
    run_git(repository, "init", "--initial-branch=main", "--quiet")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "phase M fixture")
    return repository


def _task(project: Project, number: int, *, max_attempts: int = 3) -> Task:
    path = f"src/task_{number:02}.py"
    return Task(
        project_id=project.id,
        external_task_id=f"PM-{number:02}",
        title=f"Implement low-risk transform {number}",
        instructions="Return the supplied value without modifying unrelated code.",
        complexity=Complexity.LOW,
        files_to_modify=[path],
        verify_commands=[f"python3 tools/verify.py {path}"],
        limits=TaskLimits(
            max_attempts=max_attempts,
            max_review_cycles=3,
            max_runtime_minutes=5,
            max_files_changed=1,
            max_diff_lines=30,
        ),
    )


def _approved_review(task_id: str) -> str:
    return _review(
        taskId=task_id,
        summary="The bounded transform is implemented and verification passed.",
    )


@pytest.mark.asyncio
async def test_first_real_project_acceptance_exercise(tmp_path: Path):
    repository = _make_repository(tmp_path)
    database = tmp_path / "phase-m.db"
    engine = create_db_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
        worker_timeout_seconds=120,
    )

    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(
                name="Phase M project",
                external_project_id="phase-m-project",
                repository_path=str(repository),
                worker_profile=WorkerProfile.PYTHON,
                protected_paths=["private/**"],
            )
        )
        tasks: list[Task] = []
        for number in range(1, 13):
            task = TaskRepository(session).add(
                _task(project, number, max_attempts=3 if number != 12 else 1)
            )
            tasks.append(TaskRepository(session).transition(task.id, TaskStatus.READY))

    missing_guard = {
        "severity": "HIGH",
        "category": "requirement",
        "file": "src/task_02.py",
        "line": 2,
        "requirementId": "PM-02-R1",
        "problem": "The implementation does not reject a missing value.",
        "requiredFix": "Raise ValueError when value is None.",
    }
    coder = ScriptedModel(
        # PM-01: intentional deterministic failure, then correction.
        _code(BROKEN, path="src/task_01.py"),
        _code(WORKING, path="src/task_01.py"),
        # PM-02: deterministic checks pass, review requests a fix.
        _code(WORKING, path="src/task_02.py"),
        _code(REVIEWED, path="src/task_02.py"),
        # PM-03..PM-10: low-risk first-pass changes.
        *[_code(WORKING, path=f"src/task_{number:02}.py") for number in range(3, 11)],
        # PM-11: spends every attempt and escalates.
        _code(BROKEN, path="src/task_11.py"),
        _code(BROKEN, path="src/task_11.py"),
        _code(BROKEN, path="src/task_11.py"),
        # PM-12: protected-path refusal causes rollback and failure.
        _code(WORKING, path=".env", summary="Added local configuration."),
    )
    review_provider = reviewer(
        _approved_review("PM-01"),
        _review(
            taskId="PM-02",
            decision="CHANGES_REQUESTED",
            summary="The missing-value requirement is not implemented.",
            issues=[missing_guard],
        ),
        _approved_review("PM-02"),
        *[_approved_review(f"PM-{number:02}") for number in range(3, 11)],
    )

    # PM-01 and PM-02 prove both fix-loop routes. Recreating the runner proves
    # no in-memory graph object is required between tasks.
    for task in tasks[:2]:
        runner = WorkflowRunner(factory, coder=coder, reviewer=review_provider, settings=settings)
        state = await runner.run_task(task.id)
        assert state["outcome"] == "COMPLETED"

    # PM-03 pauses before work, then resumes through a newly constructed graph
    # and a newly constructed database engine: orchestrator and database
    # restarts cannot erase its run or checkpoint identity.
    with factory.begin() as session:
        paused_run = create_run(session, tasks[2].id)
        pause_task(session, tasks[2].id, reason="phase M restart exercise")
    runner = WorkflowRunner(factory, coder=coder, reviewer=review_provider, settings=settings)
    paused = await runner.run(paused_run.id)
    assert paused["outcome"] == "PAUSED"
    engine.dispose()
    engine = create_db_engine(f"sqlite:///{database}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    restarted = WorkflowRunner(
        factory, coder=coder, reviewer=review_provider, settings=settings
    )
    resumed = await restarted.resume(paused_run.id)
    assert resumed["outcome"] == "COMPLETED"

    # PM-04..PM-10 bring the accepted low-risk total to ten.
    for task in tasks[3:10]:
        runner = WorkflowRunner(factory, coder=coder, reviewer=review_provider, settings=settings)
        state = await runner.run_task(task.id)
        assert state["outcome"] == "COMPLETED"

    # PM-11 proves retry exhaustion. The candidate remains available because
    # an open human escalation still needs it.
    runner = WorkflowRunner(factory, coder=coder, reviewer=review_provider, settings=settings)
    exhausted = await runner.run_task(tasks[10].id)
    assert exhausted["outcome"] == "ESCALATED"

    # PM-12 proves an unsafe write is rolled back and its worktree released.
    runner = WorkflowRunner(factory, coder=coder, reviewer=review_provider, settings=settings)
    rolled_back = await runner.run_task(tasks[11].id)
    assert rolled_back["outcome"] == "FAILED"
    assert rolled_back["worktree_released"] is True

    with factory() as session:
        accepted = [
            task
            for task in TaskRepository(session).list_for_project(project.id)
            if task.status is TaskStatus.COMPLETE
        ]
        failed_run = TaskRunRepository(session).list_for_task(tasks[11].id)[0]
        exhausted_run = TaskRunRepository(session).list_for_task(tasks[10].id)[0]
        failures = VerificationRunRepository(session).list_failures(exhausted_run.id)
        open_escalations = EscalationRepository(session).list_open(task_id=tasks[10].id)
        worktrees = census(session, settings=settings)

    assert len(accepted) == 10
    assert len(failures) == 3
    assert exhausted_run.status is RunStatus.FAILED
    assert exhausted_run.failure_reason == FailureReason.RETRY_EXHAUSTED.value
    assert len(open_escalations) == 1
    assert failed_run.status is RunStatus.FAILED
    assert failed_run.failure_reason == FailureReason.SCOPE_VIOLATION.value
    # Completed and rolled-back worktrees are cleaned. The only retained tree
    # is the retry-exhausted candidate a person has explicitly been asked to
    # inspect, and it is not releasable.
    assert worktrees.total == 1
    assert worktrees.releasable == 0
    assert worktrees.unclaimed == ()
    assert not coder.answers
    engine.dispose()
