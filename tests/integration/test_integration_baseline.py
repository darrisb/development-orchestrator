"""The cumulative accepted baseline (concern 51).

Task dependencies used to constrain scheduling and nothing else: every task
worktree started from the imported branch, so a dependent task was ordered after
its dependency and then handed a tree without the dependency's accepted work in
it. These tests pin the seven properties the fix has to hold, and the first one
is the defect itself -- task B must see task A's accepted code.

Real Git, real worktrees, real command processes, scripted models: what is being
tested is the orchestrator's integration baseline, not a model.
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
    RunEventType,
    TaskStatus,
    VerificationStatus,
    VerificationType,
    WorkerProfile,
)
from apps.orchestrator.domain.escalation import EscalationIntent
from apps.orchestrator.domain.git import INTEGRATION_BRANCH
from apps.orchestrator.domain.models import Project, Task, TaskLimits
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import (
    EscalationRepository,
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    VerificationRunRepository,
)
from apps.orchestrator.services.delivery import Delivery, deliver_candidate
from apps.orchestrator.services.errors import EntityConflict
from apps.orchestrator.services.integration import (
    integrate_candidate,
    integration_worktree_path,
)
from apps.orchestrator.services.pauses import pause_task, resume_task
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.scheduler import (
    NoTaskReason,
    refresh_readiness,
    select_next_task,
)
from apps.orchestrator.services.workspace import (
    prepare_workspace,
    repository_service,
)
from apps.orchestrator.workflow import WorkflowRunner
from apps.orchestrator.workflow.resolution import (
    apply_escalation_answer,
    intent_for_key,
)
from tests.conftest import run_git
from tests.integration.test_fix_loop import ScriptedModel, _review, reviewer

pytestmark = pytest.mark.integration

PYTHON = "python3"

#: A verifier that passes only when every declared step is present, so a tree
#: that lost an earlier task's work fails rather than passing quietly. This is
#: what makes the cumulative-verification tests measure something.
_VERIFY = """\
import pathlib
import sys

source = pathlib.Path("src/pipeline.py").read_text(encoding="utf-8")
for name in sys.argv[1:]:
    if f"def {name}(" not in source:
        print(f"missing step: {name}", file=sys.stderr)
        raise SystemExit(1)
print("pipeline verified")
"""


#: Cumulative states of src/pipeline.py, each adding one step to the last.
_BASE = "def start():\n    return []\n"
_ALPHA = _BASE + "\n\ndef alpha():\n    return 1\n"
_BETA = _ALPHA + "\n\ndef beta():\n    return 2\n"
_GAMMA = _BETA + "\n\ndef gamma():\n    return 3\n"


def _repository(root: Path) -> Path:
    """A project whose single source file several tasks each add a step to."""
    repository = root / "pipeline-project"
    (repository / "src").mkdir(parents=True)
    (repository / "tools").mkdir()
    (repository / "src" / "pipeline.py").write_text(
        "def start():\n    return []\n", encoding="utf-8"
    )
    (repository / "tools" / "verify.py").write_text(_VERIFY, encoding="utf-8")
    run_git(repository, "init", "--initial-branch=main", "--quiet")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "pipeline fixture")
    return repository


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
    engine = create_db_engine(f"sqlite:///{root / 'baseline.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _project(session: Session, repository: Path, *, verify: tuple[str, ...] = ()) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="Pipeline",
            external_project_id="pipeline-project",
            repository_path=str(repository),
            default_branch="main",
            worker_profile=WorkerProfile.PYTHON,
            verification=VerificationProfile(tests=verify),
        )
    )


def _task(
    session: Session,
    project: Project,
    external_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    ready: bool = True,
) -> Task:
    task = TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id=external_id,
            title=f"Add the {external_id} step",
            instructions="Add one function to src/pipeline.py.",
            complexity=Complexity.LOW,
            depends_on=list(depends_on),
            files_to_modify=["src/pipeline.py"],
            limits=TaskLimits(
                max_attempts=1, max_review_cycles=1, max_files_changed=1, max_diff_lines=40
            ),
        )
    )
    if ready:
        return TaskRepository(session).transition(task.id, TaskStatus.READY)
    return task


def _edit(source: str, summary: str = "Added a step.") -> str:
    """A coder answer rewriting src/pipeline.py with ``source``."""
    import json

    return "```json\n" + json.dumps(
        {
            "summary": summary,
            "edits": [{"path": "src/pipeline.py", "operation": "update", "content": source}],
            "requirementsMet": ["added the step"],
            "testsWritten": [],
            "followUps": [],
            "deviationsFromPlan": [],
        }
    ) + "\n```"


def _baseline_sha(project: Project, settings: Settings) -> str:
    repository = repository_service(project, settings=settings)
    return repository.resolve_sha(INTEGRATION_BRANCH)


# --------------------------------------------------------- the defect itself


@pytest.mark.asyncio
async def test_a_dependent_task_sees_its_dependencys_accepted_code(tmp_path: Path):
    """Concern 51, the property the TraceStack run showed was missing.

    TS-104 was told to reuse the ``find()`` TS-103 had just added, could not see
    it, and wrote its own. Here PIPE-02 is given a context that must contain
    PIPE-01's accepted function, and the assertion is on the *prompt the coder
    was handed*, because that is where the defect actually lived.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        _task(session, project, "PIPE-01")
        _task(session, project, "PIPE-02", depends_on=("PIPE-01",), ready=False)
        project_id = project.id

    first = ScriptedModel(_edit(_ALPHA))
    runner = WorkflowRunner(
        factory, coder=first, reviewer=reviewer(_review(taskId="PIPE-01")), settings=settings
    )
    try:
        selection, state = await runner.run_next(project_id)
    finally:
        await runner.aclose()
    assert state is not None and state["outcome"] == "COMPLETED"
    assert selection.task is not None and selection.task.external_task_id == "PIPE-01"

    second = ScriptedModel(_edit(_BETA))
    runner = WorkflowRunner(
        factory, coder=second, reviewer=reviewer(_review(taskId="PIPE-02")), settings=settings
    )
    try:
        selection, state = await runner.run_next(project_id)
    finally:
        await runner.aclose()
    assert selection.task is not None and selection.task.external_task_id == "PIPE-02"
    assert state is not None and state["outcome"] == "COMPLETED"

    # The prompt PIPE-02's coder received contained PIPE-01's accepted work.
    prompt = "\n".join(
        message.content for message in second.requests[0].messages()
    )
    assert "def alpha(" in prompt, "the dependent task could not see its dependency's code"


# --------------------------------------------------- the ref and the user branch


@pytest.mark.asyncio
async def test_the_integration_ref_advances_and_the_imported_branch_does_not(
    tmp_path: Path,
):
    """Requirements 1 and 2: cumulative state moves, the operator's branch does not."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        _task(session, project, "PIPE-01")
        project_id = project.id
        imported_before = repository_service(project, settings=settings).resolve_sha("main")

    coder = ScriptedModel(_edit(_ALPHA))
    runner = WorkflowRunner(
        factory, coder=coder, reviewer=reviewer(_review(taskId="PIPE-01")), settings=settings
    )
    try:
        _, state = await runner.run_next(project_id)
    finally:
        await runner.aclose()
    assert state is not None and state["outcome"] == "COMPLETED"

    with factory() as session:
        project = ProjectRepository(session).get(project_id)
    git = repository_service(project, settings=settings)

    assert git.branch_exists(INTEGRATION_BRANCH)
    baseline = git.resolve_sha(INTEGRATION_BRANCH)
    assert baseline == state["commit_sha"], "the baseline did not advance to the accepted work"
    # And the imported branch is exactly where the operator left it.
    assert git.resolve_sha("main") == imported_before
    assert "def alpha(" not in (repository / "src" / "pipeline.py").read_text()


@pytest.mark.asyncio
async def test_cumulative_verification_runs_against_the_merged_tree(tmp_path: Path):
    """Requirement 6: the commands execute, over the integrated tree.

    The verifier is given the names of *both* steps, so it can only pass on a
    tree that holds the first task's work as well as the second's -- which is
    the difference between verifying a candidate and verifying the baseline.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(
            session, repository, verify=(f"{PYTHON} tools/verify.py alpha",)
        )
        _task(session, project, "PIPE-01")
        project_id = project.id

    coder = ScriptedModel(_edit(_ALPHA))
    runner = WorkflowRunner(
        factory, coder=coder, reviewer=reviewer(_review(taskId="PIPE-01")), settings=settings
    )
    try:
        _, state = await runner.run_next(project_id)
    finally:
        await runner.aclose()

    assert state is not None and state["outcome"] == "COMPLETED"
    # The log of the cumulative run is filed under the run that caused it.
    logs = list((settings.artifact_root / "runs").rglob("integration/**/*.log"))
    assert logs, "cumulative verification produced no log"
    assert "pipeline verified" in logs[0].read_text(encoding="utf-8")
    with factory() as session:
        events = [e.event_type for e in RunEventRepository(session).list_for_run(
            __import__("uuid").UUID(state["run_id"])
        )]
    assert RunEventType.INTEGRATION_ADVANCED in events


@pytest.mark.asyncio
async def test_cumulative_verification_is_recorded_where_the_run_history_is(
    tmp_path: Path,
):
    """The cumulative gate writes ``verification_runs`` rows, of its own type.

    The gate was the one check in the run whose result was only in a log
    directory and an event payload: a reader of the run's verification history
    saw the candidate's own checks and nothing about the merged tree that
    actually decided whether the baseline moved. Same table, same repository and
    same model as a candidate check -- what separates them is the
    ``verification_type``, because "this passed on its own" and "this passed
    together with what came before" are different questions with different
    answers and one ``TESTS`` row cannot say which one it answered.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(
            session, repository, verify=(f"{PYTHON} tools/verify.py alpha",)
        )
        _task(session, project, "PIPE-01")
        project_id = project.id

    coder = ScriptedModel(_edit(_ALPHA))
    runner = WorkflowRunner(
        factory,
        coder=coder,
        reviewer=reviewer(_review(taskId="PIPE-01")),
        settings=settings,
    )
    try:
        _, state = await runner.run_next(project_id)
    finally:
        await runner.aclose()

    assert state is not None and state["outcome"] == "COMPLETED"
    with factory() as session:
        recorded = VerificationRunRepository(session).list_for_run(
            __import__("uuid").UUID(state["run_id"])
        )
    types = [row.verification_type for row in recorded]
    # The cumulative command is on the record under its own type. The candidate's
    # own check of the same command is still filed as ``TESTS`` -- it ran on a
    # different tree, it is still a real check, and overwriting or relabelling it
    # would lose a fact rather than add one. What the reader gains is that the
    # two are now countable apart.
    assert types.count(VerificationType.INTEGRATION_TESTS) == 1, types
    assert types.count(VerificationType.TESTS) == 1, types
    cumulative = next(
        row for row in recorded if row.verification_type is VerificationType.INTEGRATION_TESTS
    )
    candidate = next(
        row for row in recorded if row.verification_type is VerificationType.TESTS
    )
    assert candidate.command == cumulative.command
    assert cumulative.status is VerificationStatus.PASSED
    assert cumulative.exit_code == 0
    assert cumulative.duration_ms is not None
    # The log is the one the command wrote, so the row and the artifact agree
    # about what ran rather than the row merely claiming it did.
    assert cumulative.stdout_artifact
    assert (settings.artifact_root / cumulative.stdout_artifact).exists()
    # And the two checks did not share a log, because they ran in different
    # trees: the cumulative one is filed under ``integration/``.
    assert cumulative.stdout_artifact != candidate.stdout_artifact
    assert "/integration/" in cumulative.stdout_artifact


# ------------------------------------------------- the two must-not-advance gates


def _accepted_candidate_on_a_diverged_baseline(
    session: Session, project: Project, settings: Settings, *, content: str
) -> tuple[Task, object, str]:
    """A committed candidate whose task is APPROVED, for direct integration."""
    task = _task(session, project, "PIPE-09")
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=settings)
    (workspace.path / "src" / "pipeline.py").write_text(content, encoding="utf-8")
    sha = workspace.git.commit("PIPE-09: candidate")
    for status in (
        TaskStatus.CODING,
        TaskStatus.VERIFYING,
        TaskStatus.REVIEW_PENDING,
        TaskStatus.REVIEWING,
        TaskStatus.APPROVED,
    ):
        TaskRepository(session).transition(task.id, status)
    return task, run, sha


def test_a_merge_conflict_does_not_advance_the_integration_ref(
    tmp_path: Path,
):
    """Requirement 5. The baseline is moved out from under the candidate first,
    so the merge has something to conflict with."""
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task, run, candidate = _accepted_candidate_on_a_diverged_baseline(
            session, project, settings,
            content=_BASE + "\n\ndef alpha():\n    return 'a'\n",
        )
        git = repository_service(project, settings=settings)
        # A second, conflicting history on the same lines, made the baseline.
        diverged = git.for_worktree(
            git.create_detached_worktree(
                settings.worktree_root / str(project.id) / "diverged",
                git.resolve_sha(INTEGRATION_BRANCH),
            ).path
        )
        (diverged.path / "src" / "pipeline.py").write_text(
            _BASE + "\n\ndef alpha():\n    return 'b'\n", encoding="utf-8"
        )
        other = diverged.commit("a conflicting baseline")
        git.force_branch(INTEGRATION_BRANCH, other)

        result = integrate_candidate(
            session, project, task, run, candidate, settings=settings
        )

        assert result.advanced is False
        assert result.conflicts, "no conflicting paths were reported"
        assert result.blocked_reason is not None
        # The ref still names the baseline it named before.
        assert git.resolve_sha(INTEGRATION_BRANCH) == other
        assert result.baseline_sha == other
        events = [e.event_type for e in RunEventRepository(session).list_for_run(run.id)]
        assert RunEventType.INTEGRATION_BLOCKED in events
        assert RunEventType.INTEGRATION_ADVANCED not in events
        # The candidate's own commit is untouched: it is the run's audit trail.
        assert git.resolve_sha(candidate) == candidate


def test_cumulative_verification_failure_does_not_advance_the_integration_ref(
    tmp_path: Path,
):
    """Requirement 7. The candidate passes alone; the merged tree does not.

    The verifier demands a step the candidate deletes, so the only way to fail is
    over the integrated tree -- exactly the case a per-candidate check cannot see.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(
            session, repository, verify=(f"{PYTHON} tools/verify.py start",)
        )
        task, run, candidate = _accepted_candidate_on_a_diverged_baseline(
            session, project, settings,
            # Drops `start`, which cumulative verification requires.
            content="def alpha():\n    return 1\n",
        )
        git = repository_service(project, settings=settings)
        before = git.resolve_sha(INTEGRATION_BRANCH)

        result = integrate_candidate(
            session, project, task, run, candidate, settings=settings
        )

        assert result.advanced is False
        assert result.failed_commands, "no failing command was reported"
        assert result.merged_sha is not None, "the merge itself should have succeeded"
        assert result.commands_run >= 1
        assert git.resolve_sha(INTEGRATION_BRANCH) == before
        events = [e.event_type for e in RunEventRepository(session).list_for_run(run.id)]
        assert RunEventType.INTEGRATION_BLOCKED in events
        assert RunEventType.INTEGRATION_ADVANCED not in events


# ------------------------------------------------------------- the DAG, unchanged


@pytest.mark.asyncio
async def test_independent_tasks_still_follow_the_dag(tmp_path: Path):
    """Requirement 9's last clause: selection is unchanged by any of this.

    Two independent tasks and one dependent one: the independents are selected in
    id order and the dependent is not selected until its dependency completes.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        _task(session, project, "PIPE-01")
        _task(session, project, "PIPE-02")
        _task(session, project, "PIPE-09", depends_on=("PIPE-02",), ready=False)
        project_id = project.id

    order: list[str] = []
    sources = [_ALPHA, _BETA, _GAMMA]
    for source in sources:
        runner = WorkflowRunner(
            factory,
            coder=ScriptedModel(_edit(source)),
            reviewer=reviewer(_review()),
            settings=settings,
        )
        try:
            selection, state = await runner.run_next(project_id)
        finally:
            await runner.aclose()
        assert selection.task is not None
        assert state is not None and state["outcome"] == "COMPLETED"
        order.append(selection.task.external_task_id)

    # Independents first, in id order; the dependent only once PIPE-02 was done.
    assert order == ["PIPE-01", "PIPE-02", "PIPE-09"]


def test_the_integration_worktree_is_not_reported_as_unclaimed(tmp_path: Path):
    """It belongs to no run, and `unclaimed` is the census asking for a human."""
    from apps.orchestrator.services.worktrees import census

    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        task, run, candidate = _accepted_candidate_on_a_diverged_baseline(
            session, project, settings, content=_ALPHA
        )
        integrate_candidate(session, project, task, run, candidate, settings=settings)
        assert integration_worktree_path(project.id, settings=settings).is_dir()
        assert census(session, settings=settings).unclaimed == ()


# ------------------------------------- a blocked integration, and its consequences
#
# What concern 51's first fix left open: a candidate could pass verification and
# review, be delivered, mark its task COMPLETE, and then fail to merge or fail the
# cumulative gate. The baseline correctly did not move -- and nothing stopped the
# *next* task from being scheduled against a baseline that did not contain the
# work it was told to build on. These tests pin the invariant that closes it: a
# dependency is satisfied only when its accepted output is in the baseline.


def _move_the_baseline(
    project: Project, settings: Settings, content: str, *, name: str = "diverged"
) -> str:
    """Put a second history on the same lines, and make it the baseline.

    Stands in for whatever else was accepted between this candidate being built
    and being delivered. Done with the baseline ref rather than with a second run
    because what is being tested is the merge, not how the divergence arose.
    """
    git = repository_service(project, settings=settings)
    worktree = git.create_detached_worktree(
        settings.worktree_root / str(project.id) / name,
        git.resolve_sha(INTEGRATION_BRANCH),
    )
    (worktree.path / "src" / "pipeline.py").write_text(content, encoding="utf-8")
    sha = worktree.commit("a second accepted history on the same lines")
    git.force_branch(INTEGRATION_BRANCH, sha)
    return sha


def _deliver_an_accepted_candidate(
    session: Session,
    project: Project,
    settings: Settings,
    *,
    external_id: str,
    content: str,
    diverge_to: str | None = None,
) -> tuple[Task, Delivery]:
    """Take one task all the way through delivery with ``content`` as its work.

    Delivery rather than ``integrate_candidate`` directly: the point of these
    tests is what a *delivered* task does to the ones after it, and the commit,
    the tag, the COMPLETE transition and the integration attempt all happen in
    that one call, in that order.

    ``diverge_to`` moves the baseline after the candidate is built and before it
    is delivered, which is the only window in which one accepted candidate can
    fail to merge into another: a candidate built on the current baseline
    contains it, so without a divergence every merge is a fast-forward.
    """
    task = _task(session, project, external_id)
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=settings)
    (workspace.path / "src" / "pipeline.py").write_text(content, encoding="utf-8")
    for status in (
        TaskStatus.CODING,
        TaskStatus.VERIFYING,
        TaskStatus.REVIEW_PENDING,
        TaskStatus.REVIEWING,
        TaskStatus.APPROVED,
    ):
        TaskRepository(session).transition(task.id, status)
    if diverge_to is not None:
        _move_the_baseline(project, settings, diverge_to)
    delivery = deliver_candidate(session, workspace, settings=settings)
    return task, delivery


def _blocked_project(
    session: Session,
    repository: Path,
    settings: Settings,
    *,
    cumulative_failure: bool,
) -> tuple[Project, Task, Delivery]:
    """A project whose PIPE-01 is COMPLETE with work outside the baseline.

    Both ways of getting there, because they fail at different gates and the
    consequence has to be identical: a conflict never reaches verification, and a
    cumulative failure merges cleanly and then fails the project's own commands
    over the merged tree.

    PIPE-02 depends on PIPE-01; PIPE-03 depends on nothing, and is here so that
    every test can check the blockage did not spread to an unrelated chain.
    """
    verify = (f"{PYTHON} tools/verify.py start",) if cumulative_failure else ()
    project = _project(session, repository, verify=verify)
    _task(session, project, "PIPE-02", depends_on=("PIPE-01",), ready=False)
    _task(session, project, "PIPE-03", ready=False)
    if cumulative_failure:
        # Merges cleanly onto the baseline and then fails the cumulative gate: it
        # drops `start`, which the project's own verification requires.
        task, delivery = _deliver_an_accepted_candidate(
            session, project, settings,
            external_id="PIPE-01",
            content="def alpha():\n    return 1\n",
        )
    else:
        task, delivery = _deliver_an_accepted_candidate(
            session, project, settings,
            external_id="PIPE-01",
            content=_BASE + "\n\ndef alpha():\n    return 'candidate'\n",
            diverge_to=_BASE + "\n\ndef alpha():\n    return 'other'\n",
        )
    return project, task, delivery


def _open_integration_escalation(session: Session, task: Task):
    escalations = [
        escalation
        for escalation in EscalationRepository(session).list_open(task_id=task.id)
        if escalation.reason == FailureReason.INTEGRATION_BLOCKED.value
    ]
    assert len(escalations) == 1, "a blocked integration must ask exactly once"
    return escalations[0]


@pytest.mark.parametrize("cumulative_failure", [False, True], ids=["conflict", "cumulative"])
def test_a_blocked_integration_leaves_a_complete_task_and_blocks_its_dependents(
    tmp_path: Path, cumulative_failure: bool
):
    """The invariant itself, by both routes to a blocked integration.

    Seven things are true at once, and the first three are what makes the rest
    safe to assert: the task is COMPLETE, its commit and tag are intact, and the
    baseline did not move. Then: the outstanding commit is recorded on the task,
    an escalation is open, the dependent is BLOCKED rather than READY, and nothing
    would be selected to run.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project, task, delivery = _blocked_project(
            session, repository, settings, cumulative_failure=cumulative_failure
        )
        project_id = project.id
        task_id = task.id

    with factory() as session:
        integration = delivery.integration
        assert integration is not None
        assert integration.advanced is False, "this delivery was supposed to be blocked"
        if cumulative_failure:
            assert integration.failed_commands, "no failing command was reported"
            assert integration.merged_sha is not None, "the merge itself should pass"
        else:
            assert integration.conflicts, "no conflicting path was reported"

        stored = TaskRepository(session).get(task_id)
        assert stored is not None
        # Delivered work is not undone and is not called failed.
        assert stored.status is TaskStatus.COMPLETE
        assert stored.unintegrated_commit == delivery.commit_sha
        assert stored.is_integrated is False

        project = ProjectRepository(session).get(project_id)
        git = repository_service(project, settings=settings)
        # The candidate and its tag survive: they are the run's audit trail.
        assert git.resolve_sha(delivery.commit_sha) == delivery.commit_sha
        assert delivery.tag is not None
        assert git.resolve_sha(delivery.tag) == delivery.commit_sha
        # And the baseline does not contain it.
        assert git.contains_commit(delivery.commit_sha, ref=INTEGRATION_BRANCH) is False
        assert git.resolve_sha(INTEGRATION_BRANCH) == integration.baseline_sha

        escalation = _open_integration_escalation(session, stored)
        assert escalation.task_run_id == delivery.task_run_id
        assert [option.intent for option in escalation.options] == [
            EscalationIntent.RETRY_INTEGRATION
        ]
        assert "INTEGRATION BLOCKED" in escalation.summary
        assert "PIPE-02" in escalation.summary, "the page must name what is waiting"

    with factory.begin() as session:
        report = refresh_readiness(session, project_id)
        assert report.blocked.get("PIPE-02") == ("PIPE-01",)
        assert report.unintegrated.get("PIPE-02") == ("PIPE-01",)
        assert "PIPE-02" not in report.ready
        by_id = {
            item.external_task_id: item
            for item in TaskRepository(session).list_for_project(project_id)
        }
        assert by_id["PIPE-02"].status is TaskStatus.BLOCKED
        # PIPE-03 depends on nothing and is unaffected: the DAG is not serialised.
        assert by_id["PIPE-03"].status is TaskStatus.READY
        assert "PIPE-03" in report.ready


@pytest.mark.asyncio
async def test_unattended_execution_stops_at_the_blockage_and_survives_a_restart(
    tmp_path: Path,
):
    """Requirement: stop safely, and still be stopped after a restart.

    The runner is rebuilt on a second session factory over the same database, the
    way a restarted process would be, and asked for work three times. The
    independent chain runs; the dependent one never does; the condition is still
    open at the end.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project, task, delivery = _blocked_project(
            session, repository, settings, cumulative_failure=True
        )
        project_id, task_id = project.id, task.id

    # A fresh factory: nothing of the blocked condition is held in memory.
    restarted = _factory(tmp_path)
    selected: list[str | None] = []
    for source in (_ALPHA, _BETA, _GAMMA):
        runner = WorkflowRunner(
            restarted,
            coder=ScriptedModel(_edit(source)),
            reviewer=reviewer(_review()),
            settings=settings,
        )
        try:
            selection, state = await runner.run_next(project_id)
        finally:
            await runner.aclose()
        selected.append(selection.task.external_task_id if selection.task else None)

    # PIPE-03 is independent and runs; PIPE-02 never becomes eligible, so the
    # second and third attempts find nothing to do rather than working on a
    # baseline that does not contain PIPE-01.
    assert selected == ["PIPE-03", None, None]

    with restarted() as session:
        stored = TaskRepository(session).get(task_id)
        assert stored is not None
        assert stored.status is TaskStatus.COMPLETE
        assert stored.unintegrated_commit == delivery.commit_sha
        assert _open_integration_escalation(session, stored) is not None
        selection = select_next_task(session, project_id)
        assert selection.task is None
        assert selection.reason is NoTaskReason.NO_READY_TASK
        assert selection.readiness.unintegrated.get("PIPE-02") == ("PIPE-01",)


def test_pausing_and_resuming_a_dependent_does_not_promote_it_past_the_blockage(
    tmp_path: Path,
):
    """Requirement: pause/resume preserves the blocked condition.

    Resume promotes a task to READY when its dependencies are satisfied, and it
    is one of the two places that decides that without the scheduler's readiness
    pass. It has to apply the same rule, or a pause and a resume would be a way
    around the invariant.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project, task, _ = _blocked_project(
            session, repository, settings, cumulative_failure=True
        )
        dependent = TaskRepository(session).get_by_external_id(project.id, "PIPE-02")
        assert dependent is not None

        pause_task(session, dependent.id, reason="operator looking at the baseline")
        paused = TaskRepository(session).get(dependent.id)
        assert paused is not None and paused.status is TaskStatus.PAUSED

        resumed = resume_task(session, dependent.id)
        assert resumed.status is TaskStatus.PENDING, "resume must not promote it"

        report = refresh_readiness(session, project.id)
        assert report.unintegrated.get("PIPE-02") == ("PIPE-01",)
        stored = TaskRepository(session).get(dependent.id)
        assert stored is not None and stored.status is TaskStatus.BLOCKED


def test_preparing_a_workspace_refuses_an_unintegrated_dependency(tmp_path: Path):
    """The guard behind the scheduler, for the doors the graph does not watch.

    Readiness will not select PIPE-02, so this never fires on the ordinary path.
    It exists because the invariant belongs to the starting commit, and this is
    the function that resolves one.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project, _, _ = _blocked_project(
            session, repository, settings, cumulative_failure=True
        )
        dependent = TaskRepository(session).get_by_external_id(project.id, "PIPE-02")
        assert dependent is not None
        TaskRepository(session).transition(dependent.id, TaskStatus.READY)
        run = create_run(session, dependent.id)

        with pytest.raises(EntityConflict, match="not in the integration baseline"):
            prepare_workspace(session, run.id, settings=settings)


# ------------------------------------------------------------------- resolution


def _answer(session: Session, escalation, *, key: str = "A", settings: Settings):
    """Answer an escalation the way the endpoint does: by the key on the page."""
    return apply_escalation_answer(
        session,
        escalation.id,
        resolution="Resolved the blockage by hand.",
        intent=intent_for_key(escalation, key),
        settings=settings,
    )


@pytest.mark.asyncio
async def test_resolving_on_the_task_branch_advances_the_baseline_and_unblocks(
    tmp_path: Path,
):
    """The ordinary resolution, end to end.

    A person merges the baseline into the task branch -- here with `-X ours`, so
    the resolution is deterministic and the test is about the orchestrator rather
    than about a conflict -- and answers the escalation. The same merge and the
    same cumulative verification then run again, the baseline advances, and the
    dependent becomes eligible and runs against a tree that contains PIPE-01.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project = _project(session, repository)
        _task(session, project, "PIPE-02", depends_on=("PIPE-01",), ready=False)
        task = _task(session, project, "PIPE-01")
        run = create_run(session, task.id)
        workspace = prepare_workspace(session, run.id, settings=settings)
        (workspace.path / "src" / "pipeline.py").write_text(
            _BASE + "\n\ndef alpha():\n    return 'candidate'\n", encoding="utf-8"
        )
        for status in (
            TaskStatus.CODING,
            TaskStatus.VERIFYING,
            TaskStatus.REVIEW_PENDING,
            TaskStatus.REVIEWING,
            TaskStatus.APPROVED,
        ):
            TaskRepository(session).transition(task.id, status)
        _move_the_baseline(
            project, settings, _BASE + "\n\ndef alpha():\n    return 'other'\n"
        )
        delivery = deliver_candidate(session, workspace, settings=settings)
        assert delivery.integration is not None and not delivery.integration.advanced
        branch = delivery.branch
        project_id, task_id = project.id, task.id
        accepted = delivery.commit_sha

    # The operator resolves it on the task branch. The worktree was released at
    # delivery, so this is a fresh checkout of the branch, as theirs would be.
    with factory() as session:
        project = ProjectRepository(session).get(project_id)
    git = repository_service(project, settings=settings)
    operator_tree = settings.worktree_root / str(project_id) / "operator"
    git.create_worktree(operator_tree, branch, create_branch=False)
    run_git(operator_tree, "merge", "--no-edit", "-X", "ours", INTEGRATION_BRANCH)

    with factory.begin() as session:
        stored = TaskRepository(session).get(task_id)
        escalation = _open_integration_escalation(session, stored)
        answered = _answer(session, escalation, settings=settings)
        assert answered.resolution_intent is EscalationIntent.RETRY_INTEGRATION
        assert answered.status is EscalationStatus.RESOLVED

        stored = TaskRepository(session).get(task_id)
        assert stored is not None
        assert stored.status is TaskStatus.COMPLETE, "the task never moved"
        assert stored.is_integrated, "the block was not cleared"
        assert not EscalationRepository(session).list_open(task_id=task_id)

        # The baseline now contains the accepted candidate *and* what it
        # conflicted with, because the resolution was a merge of the two.
        assert git.contains_commit(accepted, ref=INTEGRATION_BRANCH)
        report = refresh_readiness(session, project_id)
        assert "PIPE-02" in report.ready
        assert not report.unintegrated

    # And the dependent now runs, against a tree holding PIPE-01's work.
    coder = ScriptedModel(_edit(_BETA))
    runner = WorkflowRunner(
        factory, coder=coder, reviewer=reviewer(_review(taskId="PIPE-02")), settings=settings
    )
    try:
        selection, state = await runner.run_next(project_id)
    finally:
        await runner.aclose()
    assert selection.task is not None and selection.task.external_task_id == "PIPE-02"
    assert state is not None and state["outcome"] == "COMPLETED"
    prompt = "\n".join(message.content for message in coder.requests[0].messages())
    assert "def alpha(" in prompt


def test_a_baseline_an_operator_merged_by_hand_clears_the_block(tmp_path: Path):
    """The other resolution: they merged it themselves.

    There is then nothing to merge and nothing new to verify, so the ref is not
    touched -- but the orchestrator's record of it was wrong and is corrected.
    Asked of Git rather than taken from the answer: `contains_commit` is the whole
    check, and an operator who claims a resolution they did not perform gets the
    block back.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project, task, delivery = _blocked_project(
            session, repository, settings, cumulative_failure=True
        )
        project_id, task_id = project.id, task.id
        accepted = delivery.commit_sha

    with factory() as session:
        project = ProjectRepository(session).get(project_id)
    git = repository_service(project, settings=settings)
    # Whatever they did with the failing verification, the work is in the ref now.
    git.force_branch(INTEGRATION_BRANCH, accepted)

    with factory.begin() as session:
        stored = TaskRepository(session).get(task_id)
        escalation = _open_integration_escalation(session, stored)
        _answer(session, escalation, settings=settings)

        stored = TaskRepository(session).get(task_id)
        assert stored is not None and stored.is_integrated
        assert git.resolve_sha(INTEGRATION_BRANCH) == accepted
        events = [
            event
            for event in RunEventRepository(session).list_for_run(delivery.task_run_id)
            if event.event_type == RunEventType.INTEGRATION_ADVANCED
        ]
        assert events and events[-1].payload["resolved_by"] == "operator"
        assert "PIPE-02" in refresh_readiness(session, project_id).ready


def test_a_retry_that_fails_again_blocks_again_and_asks_again(tmp_path: Path):
    """Answering does not clear the condition; only integrating does.

    The escalation is resolved -- somebody did answer it -- and a new one is open,
    because the question is still live. The flag never lifted, so the dependent
    was never eligible in between.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project, task, delivery = _blocked_project(
            session, repository, settings, cumulative_failure=True
        )
        project_id, task_id = project.id, task.id

    with factory.begin() as session:
        stored = TaskRepository(session).get(task_id)
        first = _open_integration_escalation(session, stored)
        # Nothing was resolved in between, so the same gate fails the same way.
        _answer(session, first, settings=settings)

        stored = TaskRepository(session).get(task_id)
        assert stored is not None
        assert stored.unintegrated_commit == delivery.commit_sha
        second = _open_integration_escalation(session, stored)
        assert second.id != first.id, "the new question must be a new record"
        answered_first = EscalationRepository(session).get(first.id)
        assert answered_first is not None
        assert answered_first.status is EscalationStatus.RESOLVED
        report = refresh_readiness(session, project_id)
        assert report.unintegrated.get("PIPE-02") == ("PIPE-01",)


def test_a_branch_that_lost_the_accepted_candidate_is_refused(tmp_path: Path):
    """A retry integrates the branch head only if the reviewed commit is in it.

    Otherwise an escalation answer would be a way to land work nobody reviewed,
    which is the one thing the whole delivery path exists to prevent.
    """
    repository = _repository(tmp_path)
    settings = _settings(tmp_path)
    factory = _factory(tmp_path)

    with factory.begin() as session:
        project, task, delivery = _blocked_project(
            session, repository, settings, cumulative_failure=True
        )
        project_id, task_id = project.id, task.id
        branch = delivery.branch

    with factory() as session:
        project = ProjectRepository(session).get(project_id)
    git = repository_service(project, settings=settings)
    # Someone rewrote the branch to something that does not contain the candidate.
    git.force_branch(branch, git.resolve_sha(INTEGRATION_BRANCH))

    with factory.begin() as session:
        stored = TaskRepository(session).get(task_id)
        escalation = _open_integration_escalation(session, stored)
        with pytest.raises(EntityConflict, match="no longer contains"):
            _answer(session, escalation, settings=settings)
