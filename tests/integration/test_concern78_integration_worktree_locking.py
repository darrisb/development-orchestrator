"""The integration worktree is one resource, so it has one lock.

The previous pass established the shared resource -- the supervisor's
per-project integration worktree, ``integration_worktree_path(project.id)`` --
and serialized *baseline certification* on the project row
(``ProjectRepository.lock``). It left the other writer of that same directory
unprotected: ``integrate_candidate`` resets the same checkout, merges into it,
runs the cumulative gate in it and then moves the ref. A certification and an
integration of the same project could therefore be in the same directory at the
same time, each resetting it under the other.

These tests pin the correction: the same lock, with the same identity, taken
before the worktree is reached on *both* paths.

Deterministic rather than threaded, for the reason the certification locking
tests give: the default test engine is SQLite, where ``with_for_update()`` is a
no-op, so threads would prove nothing about ``SELECT ... FOR UPDATE`` and would
be flaky as well. What is worth pinning is the logic the lock protects -- that
it is acquired before the shared checkout is touched, that both operations name
the same row, that different projects name different rows, that failing to get
it leaves the directory alone and advances no integration state, and that the
existing success and failure outcomes are unchanged.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from apps.orchestrator.domain.enums import EscalationStatus, RunEventType, WorkerProfile
from apps.orchestrator.domain.git import INTEGRATION_BRANCH
from apps.orchestrator.domain.models import Project
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import (
    EscalationRepository,
    ModelRunRepository,
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
)
from apps.orchestrator.services import integration as integration_service
from apps.orchestrator.services.errors import LockWaitTimeout
from apps.orchestrator.services.integration import (
    integrate_candidate,
    integration_worktree_path,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import prepare_workspace, repository_service
from tests.integration.test_integration_baseline import (
    _BASE,
    PYTHON,
    _accepted_candidate_on_a_diverged_baseline,
    _factory,
    _project,
    _repository,
    _settings,
    _task,
)

pytestmark = pytest.mark.integration

#: A candidate that keeps `start`, so the cumulative gate passes.
GOOD_CANDIDATE = _BASE + "\n\ndef alpha():\n    return 1\n"
#: A candidate that drops `start`, so the cumulative gate fails.
BAD_CANDIDATE = "def alpha():\n    return 1\n"
VERIFY = (f"{PYTHON} tools/verify.py start",)


@pytest.fixture
def trace(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The order of the two operations whose interleaving is the bug.

    ``lock`` must always precede ``worktree``: anything else is a path that
    reached the shared checkout without exclusive access to it.
    """
    events: list[str] = []
    real_lock = ProjectRepository.lock
    real_worktree = integration_service._integration_worktree

    def traced_lock(self: ProjectRepository, project_id: UUID):
        events.append(f"lock:{project_id}")
        return real_lock(self, project_id)

    def traced_worktree(*args, **kwargs):
        events.append("worktree")
        return real_worktree(*args, **kwargs)

    monkeypatch.setattr(ProjectRepository, "lock", traced_lock)
    monkeypatch.setattr(integration_service, "_integration_worktree", traced_worktree)
    return events


def timing_out_lock(monkeypatch: pytest.MonkeyPatch, *, after: int = 0) -> list[str]:
    """Make the ``after``-th and every later lock acquisition time out.

    ``after=0`` is "the lock is already held by somebody else"; ``after=1``
    lets one acquisition through -- a certification that owns the worktree --
    and makes the next one wait out its ``lock_timeout``.
    """
    attempts: list[str] = []
    real_lock = ProjectRepository.lock

    def lock(self: ProjectRepository, project_id: UUID):
        attempts.append(f"lock:{project_id}")
        if len(attempts) > after:
            raise LockWaitTimeout("gave up waiting for a database lock after 30s")
        return real_lock(self, project_id)

    monkeypatch.setattr(ProjectRepository, "lock", lock)
    return attempts


# --- the lock is the same lock, in the same place ----------------------------


def test_integration_takes_the_project_lock_before_touching_the_worktree(
    tmp_path: Path, trace: list[str]
):
    """Requirement 2. The lock covers the critical section, not just the write.

    If the worktree were opened first, the serialization would be decorative:
    the certifier and the integrator would both have reset the same checkout
    before either of them held anything.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    with _factory(tmp_path).begin() as session:
        project = _project(session, repository, verify=VERIFY)
        task, run, candidate = _accepted_candidate_on_a_diverged_baseline(
            session, project, settings, content=GOOD_CANDIDATE
        )
        trace.clear()  # preparing the workspace certifies, and that is its own test

        result = integrate_candidate(
            session, project, task, run, candidate, settings=settings
        )

        assert result.advanced
        assert trace[0] == f"lock:{project.id}"
        assert "worktree" in trace
        assert trace.index(f"lock:{project.id}") < trace.index("worktree")


def test_certification_and_integration_lock_the_same_row(
    tmp_path: Path, trace: list[str]
):
    """Requirements 1 and 4. One resource, one lock identity.

    The identity is the project row, which is what makes the two operations
    exclude *each other* rather than only themselves.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    with _factory(tmp_path).begin() as session:
        project = _project(session, repository, verify=VERIFY)
        task = _task(session, project, "PIPE-01")
        run = create_run(session, task.id)

        # Certification, on the preparation path.
        prepare_workspace(session, run.id, settings=settings)
        certification_locks = [event for event in trace if event.startswith("lock:")]
        assert certification_locks == [f"lock:{project.id}"]
        trace.clear()

        task2, run2, candidate = _accepted_candidate_on_a_diverged_baseline(
            session, project, settings, content=GOOD_CANDIDATE
        )
        trace.clear()
        integrate_candidate(session, project, task2, run2, candidate, settings=settings)

        integration_locks = [event for event in trace if event.startswith("lock:")]
        assert integration_locks == certification_locks


def test_different_projects_lock_different_rows(tmp_path: Path, trace: list[str]):
    """Requirement 5. No global serialization: each project owns its own row."""
    settings = _settings(tmp_path)
    first_repo = _repository(tmp_path / "one")
    second_repo = _repository(tmp_path / "two")
    with _factory(tmp_path).begin() as session:
        first = _project(session, first_repo, verify=VERIFY)
        second = ProjectRepository(session).add(
            Project(
                name="Pipeline Two",
                external_project_id="pipeline-project-two",
                repository_path=str(second_repo),
                default_branch="main",
                worker_profile=WorkerProfile.PYTHON,
                verification=VerificationProfile(tests=VERIFY),
            )
        )
        assert first.id != second.id

        locks: list[str] = []
        for project in (first, second):
            task, run, candidate = _accepted_candidate_on_a_diverged_baseline(
                session, project, settings, content=GOOD_CANDIDATE
            )
            trace.clear()
            result = integrate_candidate(
                session, project, task, run, candidate, settings=settings
            )
            assert result.advanced
            locks += [event for event in trace if event.startswith("lock:")]

        assert locks == [f"lock:{first.id}", f"lock:{second.id}"]


# --- the simulated race ------------------------------------------------------


def test_integration_waits_and_fails_before_the_worktree_when_certification_owns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The interleaving this correction exists for, forced rather than raced.

    Certification acquires the project lock and, while holding it, an
    integration of the same project is attempted. The integration's own
    acquisition waits out ``lock_timeout``; what matters is where it stops --
    before the shared checkout, with the baseline where it was.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    with _factory(tmp_path).begin() as session:
        project = _project(session, repository, verify=VERIFY)
        task, run, candidate = _accepted_candidate_on_a_diverged_baseline(
            session, project, settings, content=GOOD_CANDIDATE
        )
        git = repository_service(project, settings=settings)
        before = git.resolve_sha(INTEGRATION_BRANCH)
        worktree_path = integration_worktree_path(project.id, settings=settings)
        head_before = git.for_worktree(worktree_path).resolve_sha("HEAD")

        reached: list[str] = []
        monkeypatch.setattr(
            integration_service,
            "_integration_worktree",
            lambda *args, **kwargs: reached.append("worktree"),
        )

        # Certification holds the lock; the integration attempted inside that
        # window is the one whose acquisition times out.
        attempts = timing_out_lock(monkeypatch, after=1)
        results: list[object] = []

        def certification_owning_the_lock():
            ProjectRepository(session).lock(project.id)  # the winner
            results.append(
                integrate_candidate(
                    session, project, task, run, candidate, settings=settings
                )
            )

        certification_owning_the_lock()

        integration = results[0]
        assert attempts == [f"lock:{project.id}", f"lock:{project.id}"]
        # The loser never reached the shared checkout.
        assert reached == []
        assert "integration worktree lock" in " ".join(integration.failed_commands)
        assert integration.advanced is False
        # Nothing in the shared worktree or the baseline moved.
        assert git.resolve_sha(INTEGRATION_BRANCH) == before
        assert git.for_worktree(worktree_path).resolve_sha("HEAD") == head_before


def test_a_lock_timeout_blocks_the_integration_and_leaves_recovery_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Requirement 3, and the failure semantics it has to reuse.

    A lock it could not get is reported the way every other non-advance is: the
    baseline stays, the task is marked as holding an unintegrated commit, an
    escalation offers the retry. Nothing is partially advanced and the shared
    directory is not touched.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    with _factory(tmp_path).begin() as session:
        project = _project(session, repository, verify=VERIFY)
        task, run, candidate = _accepted_candidate_on_a_diverged_baseline(
            session, project, settings, content=GOOD_CANDIDATE
        )
        git = repository_service(project, settings=settings)
        before = git.resolve_sha(INTEGRATION_BRANCH)

        reached: list[str] = []
        monkeypatch.setattr(
            integration_service,
            "_integration_worktree",
            lambda *args, **kwargs: reached.append("worktree"),
        )
        timing_out_lock(monkeypatch, after=0)

        result = integrate_candidate(
            session, project, task, run, candidate, settings=settings
        )

        assert result.advanced is False
        assert reached == []
        assert git.resolve_sha(INTEGRATION_BRANCH) == before
        assert result.baseline_sha == before
        assert result.merged_sha is None
        assert result.escalation_id is not None
        # The existing recovery machinery, unchanged.
        assert TaskRepository(session).get(task.id).unintegrated_commit == candidate
        escalation = EscalationRepository(session).get(result.escalation_id)
        assert escalation is not None and escalation.status is EscalationStatus.OPEN
        events = [e.event_type for e in RunEventRepository(session).list_for_run(run.id)]
        assert RunEventType.INTEGRATION_BLOCKED in events
        assert RunEventType.INTEGRATION_ADVANCED not in events
        # The candidate's own commit is untouched.
        assert git.resolve_sha(candidate) == candidate


# --- what must not have changed ---------------------------------------------


def test_integration_success_is_unchanged(tmp_path: Path):
    """Requirement 6. The ref advances, the task is integrated, the event says so."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    with _factory(tmp_path).begin() as session:
        project = _project(session, repository, verify=VERIFY)
        task, run, candidate = _accepted_candidate_on_a_diverged_baseline(
            session, project, settings, content=GOOD_CANDIDATE
        )
        git = repository_service(project, settings=settings)
        before = git.resolve_sha(INTEGRATION_BRANCH)

        result = integrate_candidate(
            session, project, task, run, candidate, settings=settings
        )

        assert result.advanced
        assert result.previous_sha == before
        assert result.integrated_sha == candidate
        assert git.resolve_sha(INTEGRATION_BRANCH) == result.baseline_sha
        assert git.contains_commit(candidate, ref=INTEGRATION_BRANCH)
        assert TaskRepository(session).get(task.id).unintegrated_commit is None
        events = [e.event_type for e in RunEventRepository(session).list_for_run(run.id)]
        assert RunEventType.INTEGRATION_ADVANCED in events
        # Requirement 9: nothing here is a model.
        assert ModelRunRepository(session).list_for_run(run.id) == []


def test_integration_failure_is_unchanged(tmp_path: Path):
    """Requirement 7. A cumulative failure still blocks, with the lock in place.

    The candidate drops a function the project's own verification requires, so
    it passes alone and fails merged -- the case the cumulative gate exists for.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    with _factory(tmp_path).begin() as session:
        project = _project(session, repository, verify=VERIFY)
        task, run, candidate = _accepted_candidate_on_a_diverged_baseline(
            session, project, settings, content=BAD_CANDIDATE
        )
        git = repository_service(project, settings=settings)
        before = git.resolve_sha(INTEGRATION_BRANCH)

        result = integrate_candidate(
            session, project, task, run, candidate, settings=settings
        )

        assert result.advanced is False
        assert result.failed_commands
        assert result.commands_run >= 1
        assert git.resolve_sha(INTEGRATION_BRANCH) == before
        assert TaskRepository(session).get(task.id).unintegrated_commit == candidate
        assert result.escalation_id is not None
        assert ModelRunRepository(session).list_for_run(run.id) == []


def test_a_replayed_integration_still_settles_without_the_worktree(tmp_path: Path):
    """Requirement 7, concern 76's half of it: a replay is a settle, not a merge.

    The fast path is before the lock deliberately -- it is a read of Git that
    touches nothing -- so a replayed delivery neither waits for the lock nor
    opens the shared checkout.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    with _factory(tmp_path).begin() as session:
        project = _project(session, repository, verify=VERIFY)
        task, run, candidate = _accepted_candidate_on_a_diverged_baseline(
            session, project, settings, content=GOOD_CANDIDATE
        )
        first = integrate_candidate(
            session, project, task, run, candidate, settings=settings
        )
        assert first.advanced

        replay = integrate_candidate(
            session, project, task, run, candidate, settings=settings
        )

        assert replay.advanced
        assert replay.merged_sha is None
        assert replay.baseline_sha == first.baseline_sha
