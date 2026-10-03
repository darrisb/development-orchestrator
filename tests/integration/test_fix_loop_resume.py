"""Resuming a fix loop after a recoverable interruption.

The defect this file exists for was found by a real run. TS-105 asked for a
correction, the reviewer returned ``CHANGES_REQUESTED``, and the correction call
then timed out after 600 seconds. The resume was a blank first try: the prompt
that reached the model had no reviewer findings in it at all, the attempt count
in the outcome said one attempt for a run that had made two, and the record of
the call that timed out was rolled away with everything else in the transaction.

None of that was a provider problem. ``run_fix_loop`` held its iterations and
its feedback in a list that only existed in the process running it, committed
once at the end of a whole turn, and had no way to answer "where was this run
when it stopped" from what was on disk.

The property under test, in one sentence: **after a recoverable interruption,
the next invocation reconstructs the durable state of the one that was
interrupted and continues it.** Everything else here is a way of breaking that
sentence.

Real worktrees, real command processes, real Git, and -- in the last two tests --
real second operating system processes and a real socket. The wait is real too:
those tests run a loopback HTTP endpoint, point a real ``OpenAICompatibleProvider``
at it, and have the endpoint accept the correction and then stop writing to the
connection, so the ``ModelTimeout`` under test is raised by httpx on a socket and
mapped by the provider's own error path. The only concession is the number the
endpoint is configured with: two seconds where the run waited ten minutes. That
is the same waiting, at a speed the suite can pay for, and it means the recorded
durations are measurements rather than constants. The other tests raise the
timeout from a scripted object instead, which is the shape of the failure without
its mechanism; they are here to break the sentence one way at a time.

The in-process tests get a database and a session of their own. The graph
commits per model call -- it passes ``session.commit`` to the loop as its
checkpoint and rolls back on the way out (``workflow/graph.py:_execute``) -- and
the suite's shared session is wrapped in a savepoint that ``commit`` releases
for real, so a test that commits through it leaves rows behind for whatever runs
next. A private database per test is not only closer to the orchestrator, which
has one session per run; it is the only way to test what a commit keeps. The last
test needs none of this argument: it loses the process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator.agents.coding_agent import run_coding_attempt
from apps.orchestrator.agents.fix_loop import (
    FIX_LOOP_ARTIFACT,
    LoopOutcome,
    run_fix_loop,
)
from apps.orchestrator.agents.loop_recovery import recover_loop_state
from apps.orchestrator.agents.review_agent import run_review
from apps.orchestrator.agents.review_prompts import (
    REVIEWER_SYSTEM_PROMPT,
    render_review_instructions,
)
from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.enums import (
    Complexity,
    FailureAction,
    FailureReason,
    ModelRole,
    RunEventType,
    RunStatus,
    TaskStatus,
    VerificationStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.errors import AbandonedRunError
from apps.orchestrator.domain.failure_policy import action_for
from apps.orchestrator.domain.models import Project, Task, TaskLimits, TaskRun
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.providers import OpenAICompatibleProvider, ProviderConfig
from apps.orchestrator.providers.errors import ModelTimeout
from apps.orchestrator.providers.review import ModelReviewProvider, ReviewerUnavailable
from apps.orchestrator.repositories import (
    ModelRunRepository,
    ProjectRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
    VerificationRunRepository,
)
from apps.orchestrator.services.abandon import abandon_run
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.verification import verify_candidate
from apps.orchestrator.services.workspace import (
    TaskWorkspace,
    attach_workspace,
    prepare_workspace,
)
from tests.conftest import run_git
from tests.integration.test_fix_loop import (
    _COMPILE,
    _MISSING_GUARD,
    _TEST,
    BROKEN,
    REVIEWED,
    STUB,
    WORKING,
    ScriptedModel,
    _code,
    _review,
    reviewer,
)

pytestmark = pytest.mark.integration

#: The failure the run hit, raised rather than waited for. ``timeout_seconds`` is
#: the configured 600 because the recorded error text is part of what an operator
#: reads, and a plausible message is worth more here than a fast one.
TIMEOUT_SECONDS = 600


def _timeout() -> ModelTimeout:
    return ModelTimeout(
        "openai-compatible did not respond to generate within 600s",
        timeout_seconds=TIMEOUT_SECONDS,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )


PYTHON = "python3"


@pytest.fixture
def project_repo(tmp_path: Path) -> Path:
    """The same fixture project the fix-loop tests use, built locally.

    Defined here rather than imported so that this file's fixtures are this
    file's, and so a change to the other file's fixture cannot quietly change
    what these assertions mean.
    """
    repo = tmp_path / "tracestack"
    (repo / "src").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "src" / "nav.py").write_text(STUB, encoding="utf-8")
    (repo / "tools" / "compile.py").write_text(_COMPILE)
    (repo / "tools" / "test.py").write_text(_TEST)
    run_git(repo, "init", "--initial-branch=main", "--quiet")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "Initial commit")
    return repo


@dataclasses.dataclass
class _World:
    """A real database, a real session, and a run to spend them on.

    A session of its own because these tests commit, and a run of its own per
    test because a run is state that accumulates: two tests sharing one would
    make the second test's attempt numbering a consequence of the first.
    """

    session: Session
    settings: Settings
    project_id: uuid.UUID

    def make_task(self, **overrides) -> Task:
        fields: dict[str, object] = {
            "project_id": self.project_id,
            "external_task_id": "TS-004",
            "title": "Reject a null navigation target",
            "instructions": "navigate must return its target and reject a null one.",
            "complexity": Complexity.LOW,
            "files_to_modify": ["src/nav.py"],
            "limits": TaskLimits(max_files_changed=3, max_diff_lines=200),
        }
        fields.update(overrides)
        tasks = TaskRepository(self.session)
        task = tasks.add(Task(**fields))  # type: ignore[arg-type]
        return tasks.transition(task.id, TaskStatus.READY)

    def make_run(self, task: Task | None = None) -> TaskRun:
        task = task or self.make_task()
        run = create_run(self.session, task.id)
        self.session.commit()
        return run


@pytest.fixture
def world(tmp_path: Path, project_repo: Path):
    """One engine, one session and one settings object, per test.

    A commit here is a real commit: the loop under test checkpoints itself per
    model call, and these assertions are about what survives that.
    """
    engine = create_db_engine(f"sqlite:///{tmp_path / 'resume.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )
    with factory.begin() as setup_session:
        project = ProjectRepository(setup_session).add(
            Project(
                name="TraceStack",
                repository_path=str(project_repo),
                default_branch="main",
                worker_profile=WorkerProfile.PYTHON,
                protected_paths=[".env", "secrets/**"],
                verification=VerificationProfile(
                    build=(f"{PYTHON} tools/compile.py",),
                    tests=(f"{PYTHON} tools/test.py",),
                ),
            )
        )
    try:
        yield _World(session=session, settings=settings, project_id=project.id)
    finally:
        session.close()
        engine.dispose()


def _run_root(session: Session, run: TaskRun, settings: Settings) -> Path:
    """Where the run's artifacts are, from the run's own row.

    Read back from the database rather than off the fixture object: the row
    gains its external id when it is written, and a copy taken before that
    would send this looking for artifacts in a directory named ``None``.
    """
    external = TaskRunRepository(session).get(run.id).external_run_id
    return settings.artifact_root / "runs" / external


def _read(session: Session, run: TaskRun, settings: Settings, *parts: str) -> str:
    return (_run_root(session, run, settings) / Path(*parts)).read_text(encoding="utf-8")


def _calls(session: Session, run: TaskRun, *purposes: str) -> list:
    wanted = set(purposes or {"CODE", "FIX"})
    return [
        call
        for call in ModelRunRepository(session).list_for_run(run.id)
        if call.purpose.value in wanted
    ]


async def _strand_correction_timeout(
    session: Session,
    run: TaskRun,
    settings: Settings,
    *,
    first_code: str,
    timed_out_code: Exception,
    first_review: str,
    timeout_model: type[ScriptedModel] = ScriptedModel,
    workspace: TaskWorkspace | None = None,
) -> TaskWorkspace:
    """Leave a run in the legacy state produced by an escaped coder timeout."""
    workspace = workspace or prepare_workspace(session, run.id, settings=settings)
    first_attempt = await run_coding_attempt(
        session,
        workspace,
        provider=ScriptedModel(first_code),
        settings=settings,
        review_cycle=1,
        checkpoint_call=session.commit,
    )
    report = verify_candidate(session, workspace, settings=settings)
    review = None
    if report.passed:
        review = await run_review(
            session,
            workspace,
            provider=reviewer(first_review),
            verification=report,
            completion_report=first_attempt.report,
            settings=settings,
            checkpoint_call=session.commit,
        )
        assert review.feedback
        feedback = review.feedback
        review_cycle = 2
    else:
        feedback = first_attempt.feedback
        review_cycle = 1
    session.commit()
    TaskRunRepository(session).update_fields(run.id, attempt_number=2)
    session.commit()
    with pytest.raises(ModelTimeout):
        await run_coding_attempt(
            session,
            workspace,
            provider=timeout_model(timed_out_code),
            settings=settings,
            review_feedback=feedback,
            plan_required=False,
            review_cycle=review_cycle,
            checkpoint_call=session.commit,
        )
    session.rollback()
    return workspace


async def _interrupt_then_resume(
    session: Session,
    run: TaskRun,
    settings: Settings,
    *,
    coders: tuple[tuple, ...],
    reviewers: tuple[tuple, ...],
):
    """Create a legacy interrupted correction, then run the loop again.

    ``run_fix_loop`` now consumes coder-side provider failures and routes them
    through the bounded retry/exhaustion workflow, so an interrupted historical
    run is manufactured at the narrower old failure boundary:
    ``run_coding_attempt`` records the failed model call and raises.
    """
    await _strand_correction_timeout(
        session,
        run,
        settings,
        first_code=coders[0][0],
        timed_out_code=coders[0][1],
        first_review=reviewers[0][0],
    )
    return await run_fix_loop(
        session,
        attach_workspace(session, run.id, settings=settings),
        coder=ScriptedModel(*coders[1]),
        reviewer=reviewer(*reviewers[1]),
        settings=settings,
        checkpoint_turn=session.commit,
    )


#: A candidate that compiles and passes the project's checks, and is still not
#: finished: it returns a null target instead of rejecting one, which is what
#: the reviewer asks about. This is the shape the real run had -- the defect was
#: in what happened *after* a review asked for a correction, so a fixture that
#: never got a review would not be testing it.
_FIRST_CANDIDATE = _code(WORKING, summary="navigate returns its target.")
_CHANGES_REQUESTED = _review(decision="CHANGES_REQUESTED", issues=[_MISSING_GUARD])
_APPROVED = _review(decision="APPROVED", issues=[])
#: What the review asked for.
_CORRECTED = _code(REVIEWED)

#: The two halves of the story: a review that asks for a correction, then a
#: correction call that never comes back.
_INTERRUPTED = ((_FIRST_CANDIDATE, _timeout()), (_CORRECTED,))
_INTERRUPTED_REVIEWS = ((_CHANGES_REQUESTED,), (_APPROVED,))


# ------------------------------- the interruption, as a real timeout on a socket

#: A scripted step that is not an answer: this request is accepted, and then
#: nothing is ever written to it.
STALL = object()


def _completion(content: str) -> dict:
    """One chat completion in the shape the provider parses."""
    return {
        "id": "chatcmpl-1",
        "model": "local",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020},
    }


class _Endpoint:
    """A real HTTP endpoint on a real socket, scripted, and able to go quiet.

    The stall is a real one: the connection is accepted, the whole request is
    read, and then nothing is written for longer than the client's read
    timeout. The ``ModelTimeout`` that follows is raised by httpx on a socket
    and mapped by the provider's own error path -- it is not handed to the loop
    by a fixture, which is the whole difference between this and the scripted
    answers above. Everything else here is real for the same reason: an HTTP
    server on a loopback port, a request body the provider builds, a reply it
    parses, and JSON that has to survive the same reasoning-block stripping.

    What the production run waited 600 seconds for, this waits
    ``timeout_seconds`` for. That is the only concession, and it is in the
    number the endpoint is configured with rather than in the mechanism.
    """

    def __init__(self, script: list, *, timeout_seconds: float) -> None:
        self.script = list(script)
        self.timeout_seconds = timeout_seconds
        #: Long enough that the client gives up first, which is the ordering
        #: that makes this a timeout rather than a refused connection.
        self.stall_seconds = timeout_seconds + 0.5
        #: Every request body the endpoint received, in order.
        self.asked: list[dict] = []
        self.stalls = 0
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _EndpointHandler)
        self._server.daemon_threads = True
        self._server.endpoint = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def answer(self, body: dict) -> str | object:
        with self._lock:
            self.asked.append(body)
            if not self.script:
                raise AssertionError(
                    f"the endpoint was asked for {body.get('model')!r} with nothing "
                    f"left in its script; the test's script is shorter than the run"
                )
            answer = self.script.pop(0)
            if answer is STALL:
                self.stalls += 1
            return answer

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _EndpointHandler(BaseHTTPRequestHandler):
    """Answers what the endpoint scripted, and stalls where it scripted a stall."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        endpoint: _Endpoint = self.server.endpoint  # type: ignore[attr-defined]
        length = int(self.headers.get("content-length") or 0)
        answer = endpoint.answer(json.loads(self.rfile.read(length)))
        if answer is STALL:
            time.sleep(endpoint.stall_seconds)
            return
        body = json.dumps(_completion(str(answer))).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        body = json.dumps({"data": [{"id": "coder-test"}, {"id": "reviewer-test"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """The endpoint does not narrate its own requests; the assertions do."""


@pytest.fixture
def endpoint():
    """Start scripted endpoints, and shut every one of them down afterwards."""
    started: list[_Endpoint] = []

    def start(script: list, *, timeout_seconds: float = 2.0) -> _Endpoint:
        live = _Endpoint(script, timeout_seconds=timeout_seconds)
        started.append(live)
        return live

    yield start
    for live in started:
        live.close()


def _live_provider(live: _Endpoint, role: ModelRole) -> OpenAICompatibleProvider:
    """A provider with nothing stubbed, pointed at the loopback endpoint."""
    return OpenAICompatibleProvider(
        ProviderConfig(
            provider_id=f"local-{role.value.casefold()}",
            base_url=live.base_url,
            model_name=f"{role.value.casefold()}-test",
            role=role,
            context_window=32768,
            timeout_seconds=live.timeout_seconds,
        )
    )


#: The script of the real failure, in the order the run made the calls: the
#: coder answers, the reviewer asks for a correction, and the correction is
#: accepted by the endpoint and never answered.
_LIVE_SCRIPT = [_FIRST_CANDIDATE, _CHANGES_REQUESTED, STALL, _CORRECTED, _APPROVED]


@pytest.mark.asyncio
async def test_an_endpoint_that_goes_quiet_does_not_take_the_review_with_it(
    world: _World, endpoint
):
    """The interruption the run really hit, injected rather than waited for.

    Every other test here raises a timeout from a scripted object, which is the
    shape of the failure but not its mechanism: nothing measures the wait, the
    message is one this file wrote, and the call is instant. The defect was
    found against a real endpoint that accepted a real correction and then sat
    on it for 600 seconds, so that is what happens here -- on a loopback socket,
    through the provider's own timeout, with the only concession being that the
    endpoint is configured to give up in two seconds instead of ten minutes.

    Two seconds is the whole budget of the test, and it is spent where the real
    run spent ten minutes: waiting for a correction that never arrives.
    """
    live = endpoint(_LIVE_SCRIPT)
    coder = _live_provider(live, ModelRole.CODER)
    judge = ModelReviewProvider(
        _live_provider(live, ModelRole.REVIEWER),
        system_prompt=REVIEWER_SYSTEM_PROMPT,
        instruction_renderer=render_review_instructions,
    )
    session, settings = world.session, world.settings
    run = world.make_run()

    result = await run_fix_loop(
        session,
        prepare_workspace(session, run.id, settings=settings),
        coder=coder,
        reviewer=judge,
        settings=settings,
        checkpoint_turn=session.commit,
    )

    # The failure is the provider's own, not this file's: it names the endpoint
    # and the timeout it was configured with.
    assert live.stalls == 1

    failed = [call for call in _calls(session, run) if call.status.value == "FAILED"]
    assert len(failed) == 1, [call.describe() for call in _calls(session, run)]
    # A real wait, measured: the duration is the endpoint's silence, so it is
    # at least the timeout and nowhere near a hang.
    assert failed[0].duration_ms >= live.timeout_seconds * 1000 - 100, failed[0].duration_ms
    assert failed[0].duration_ms < live.timeout_seconds * 1000 + 5000, failed[0].duration_ms
    assert failed[0].attempt == 2 and failed[0].review_cycle == 2
    assert "did not respond" in (failed[0].error_detail or "")

    # The request that was left hanging was the correction, and it went out
    # carrying the reviewer's findings -- the one that reached the endpoint is
    # the same text the prompt artifact holds, so this is the real request the
    # real run never got an answer to.
    stalled_request = live.asked[2]
    assert stalled_request["model"] == "coder-test"
    correction_prompt = "\n".join(message["content"] for message in stalled_request["messages"])
    assert _MISSING_GUARD["problem"] in correction_prompt
    assert _MISSING_GUARD["requiredFix"] in correction_prompt

    assert result.outcome is LoopOutcome.APPROVED
    assert result.attempts_used == 3
    assert result.cycles_used == 2
    assert result.recovery is not None and not result.recovery.recovered
    # The correction that came back carries what the review asked for.
    resumed = result.iterations[-1].attempt
    prompt = _read(session, run, settings, f"attempt-{resumed}-cycle-2", "prompt.txt")
    assert _MISSING_GUARD["problem"] in prompt
    assert "ValueError" in _read(
        session, run, settings, f"attempt-{resumed}-cycle-2", "candidate.patch"
    )
    # Both interrupted and resumed calls are on the record, and the second one
    # through the same socket that went quiet the first time.
    assert [call.attempt for call in _calls(session, run)] == [1, 2, 3]
    assert [call.status.value for call in _calls(session, run)] == [
        "SUCCEEDED",
        "FAILED",
        "SUCCEEDED",
    ]



# ------------------------------------------------- the failing call is on record


@pytest.mark.asyncio
async def test_a_timed_out_correction_call_is_recorded_before_the_rollback(world: _World):
    """A call that failed has a row, an error, a real duration and a place.

    This is the first thing the run got wrong: the record of the call was added
    in the same transaction as everything else, and the failure path rolled the
    transaction back, so the one call that cost 600 seconds was the one call
    with no row. The row is now committed by the call itself before the
    exception leaves, which is the last moment at which writing it is possible.
    """
    session, settings = world.session, world.settings
    run = world.make_run()
    await _strand_correction_timeout(
        session,
        run,
        settings,
        first_code=_FIRST_CANDIDATE,
        timed_out_code=_timeout(),
        first_review=_CHANGES_REQUESTED,
    )

    failed = [call for call in _calls(session, run) if call.status.value == "FAILED"]
    assert len(failed) == 1, [call.describe() for call in _calls(session, run)]
    call = failed[0]
    assert call.attempt == 2, "the interrupted attempt must be the one on the record"
    assert call.review_cycle == 2, "the correction belongs to the cycle it corrects"
    assert "did not respond" in (call.error_detail or "")
    assert call.model_id is not None, "a row with no model cannot answer for itself"
    assert call.started_at is not None
    assert call.duration_ms is not None
    # The review that asked for the correction is durable too, or there would be
    # nothing for the resume to reconstruct.
    reviews = ReviewRepository(session).list_for_run(run.id)
    assert [review.cycle for review in reviews] == [1]
    assert reviews[0].decision.value == "CHANGES_REQUESTED"


@pytest.mark.asyncio
async def test_a_failed_call_reports_the_time_it_actually_took(world: _World):
    """``duration_ms`` is measured, not defaulted to zero.

    Pinned with a sleep rather than a 600-second wait: what matters is that the
    number came from a clock. A call that took 60ms must not be filed as ``0``,
    because the difference between "failed immediately" and "failed after a long
    time" is the difference between a bad request and an unreachable host, and
    zero is what an unrecorded timeout looked like.
    """
    session, settings = world.session, world.settings
    run = world.make_run()

    class SlowToFail(ScriptedModel):
        async def generate(self, request):  # type: ignore[no-untyped-def]
            if len(self.answers) == 1:
                await asyncio.sleep(0.06)
            return await super().generate(request)

    await _strand_correction_timeout(
        session,
        run,
        settings,
        first_code=_FIRST_CANDIDATE,
        timed_out_code=_timeout(),
        first_review=_CHANGES_REQUESTED,
        timeout_model=SlowToFail,
        workspace=prepare_workspace(session, run.id, settings=settings),
    )

    timed_out = [call for call in _calls(session, run) if call.status.value == "FAILED"]
    assert len(timed_out) == 1
    # Generous, because a loaded test machine is not a monotonic clock: this
    # asserts the order of magnitude, not the exact value.
    assert timed_out[0].duration_ms is not None
    assert timed_out[0].duration_ms >= 40, timed_out[0].duration_ms


# ------------------------------------------- the correction comes back the same


@pytest.mark.asyncio
async def test_an_interrupted_correction_comes_back_with_the_reviewers_findings(world: _World):
    """The defect itself: the resumed prompt must carry the findings.

    Not "the findings are somewhere in the resumed run" and not "the review row
    still exists" -- the findings have to be in the prompt that reached the
    model, which is the only place the coder can act on them. It is read off
    disk, because that is the artifact the run keeps, and compared against the
    reviewer's own words rather than a summary of them.
    """
    session, settings = world.session, world.settings
    run = world.make_run()
    result = await _interrupt_then_resume(
        session,
        run,
        settings,
        coders=_INTERRUPTED,
        reviewers=_INTERRUPTED_REVIEWS,
    )

    assert result.outcome is LoopOutcome.APPROVED
    resumed = result.iterations[0].attempt
    prompt = _read(session, run, settings, f"attempt-{resumed}-cycle-2", "prompt.txt")
    assert _MISSING_GUARD["problem"] in prompt
    assert _MISSING_GUARD["requiredFix"] in prompt
    # And the file: the resume fixed what the review asked about, rather than
    # the review having been satisfiable anyway.
    patch = _read(session, run, settings, f"attempt-{resumed}-cycle-2", "candidate.patch")
    assert "ValueError" in patch


@pytest.mark.asyncio
async def test_a_resumed_run_continues_the_attempt_numbering(world: _World):
    """Attempts move forward, and the interrupted one is still charged.

    Two properties pulling in opposite directions. The numbering must not reuse
    the interrupted attempt, or the resumed turn would overwrite the artifacts of
    the attempt that was lost. And the interrupted attempt must still count
    against the task's ceiling, or a run whose provider keeps timing out would
    retry forever.
    """
    session, settings = world.session, world.settings
    run = world.make_run()
    result = await _interrupt_then_resume(
        session,
        run,
        settings,
        coders=_INTERRUPTED,
        reviewers=_INTERRUPTED_REVIEWS,
    )

    assert [i.attempt for i in result.iterations] == [3], (
        "attempt 2 was begun and lost, so the resume is attempt 3"
    )
    directories = {
        i.coding.artifacts["candidate.patch"].rsplit("/", 1)[0] for i in result.iterations
    }
    assert {d.rsplit("/", 1)[-1] for d in directories} == {"attempt-3-cycle-2"}
    # The lost attempt's own artifacts are still on disk, not overwritten.
    run_root = _run_root(session, run, settings)
    assert (run_root / "attempt-2-cycle-2").is_dir()
    assert (run_root / "attempt-3-cycle-2").is_dir()
    # Both attempts are on the record, and both belong to this run.
    assert sorted(call.attempt for call in _calls(session, run)) == [1, 2, 3]
    # The counts reported are the run's, not this process's turn list.
    assert result.attempts_used == 3
    assert result.cycles_used == 2


@pytest.mark.asyncio
async def test_an_interrupted_review_does_not_spend_a_review_cycle(world: _World):
    """A cycle is charged when a reviewer answers, not when one is asked for.

    The reviewer is what closes a cycle: its answer either accepts the candidate
    or says what to change. A process that dies waiting for that answer has spent
    nothing, and resuming inside the same cycle is the only honest reading -- the
    alternative spends a review cycle on a review that never happened, and a task
    with two cycles would silently get one.
    """
    session, settings = world.session, world.settings
    run = world.make_run()
    workspace = prepare_workspace(session, run.id, settings=settings)
    # A reviewer that cannot answer is the reviewer's failure, and it arrives
    # as ``ReviewerUnavailable`` -- the provider error underneath it is in the
    # message, and the row it wrote records which call failed.
    with pytest.raises(ReviewerUnavailable):
        await run_fix_loop(
            session,
            workspace,
            coder=ScriptedModel(_FIRST_CANDIDATE, _CORRECTED),
            reviewer=reviewer(_CHANGES_REQUESTED, _timeout()),
            settings=settings,
            checkpoint_turn=session.commit,
        )
    session.rollback()

    # One review on the record, and the run's counter agrees with it.
    reviews = ReviewRepository(session).list_for_run(run.id)
    assert [review.cycle for review in reviews] == [1]
    assert TaskRunRepository(session).get(run.id).review_cycle == 1
    # The failed review call is filed under the cycle it was in, which is how a
    # later reader can tell it was attempted and never answered.
    failed = [call for call in _calls(session, run, "REVIEW") if call.status.value == "FAILED"]
    assert [call.review_cycle for call in failed] == [2]

    result = await run_fix_loop(
        session,
        attach_workspace(session, run.id, settings=settings),
        coder=ScriptedModel(_CORRECTED),
        reviewer=reviewer(_APPROVED),
        settings=settings,
        checkpoint_turn=session.commit,
    )
    assert result.outcome is LoopOutcome.APPROVED
    # The resumed review is cycle 2, not cycle 3: cycle 2 was begun, never spent.
    assert [r.cycle for r in ReviewRepository(session).list_for_run(run.id)] == [1, 2]
    assert result.cycles_used == 2


@pytest.mark.asyncio
async def test_recovering_twice_does_not_move_the_run(world: _World):
    """Recovery is a read. Asking twice must not advance anything.

    A restart can happen more than once -- the worker is restarted while the
    orchestrator is still coming back -- and if reading the state moved it, the
    second restart would skip an attempt number and charge a budget for work
    nobody did.
    """
    session, settings = world.session, world.settings
    run = world.make_run()
    await _strand_correction_timeout(
        session,
        run,
        settings,
        first_code=_FIRST_CANDIDATE,
        timed_out_code=_timeout(),
        first_review=_CHANGES_REQUESTED,
    )

    first = recover_loop_state(session, run, settings=settings)
    second = recover_loop_state(session, run, settings=settings)
    assert first.next_attempt == second.next_attempt == 3
    assert first.attempts_started == second.attempts_started == 2
    assert first.cycle == second.cycle == 2
    assert first.reviews_completed == second.reviews_completed == 1
    assert first.interrupted_attempt == second.interrupted_attempt == 2
    assert first.feedback == second.feedback
    assert first.feedback is not None
    assert _MISSING_GUARD["problem"] in first.feedback


# --------------------------------------------- the run reports its real history


@pytest.mark.asyncio
async def test_the_run_settles_on_the_history_not_on_this_processes_turns(world: _World):
    """The outcome and the fix-loop artifact count the real history.

    Audit C. The reported attempt count came from ``len(iterations)``, a list
    that is empty on a resume, so a run that had made three attempts reported one
    -- in ``outcome.json``, in ``fix-loop.json``, and in the escalation a person
    reads to decide what to do about it. It is counted from the records now.
    """
    session, settings = world.session, world.settings
    run = world.make_run()
    result = await _interrupt_then_resume(
        session,
        run,
        settings,
        coders=_INTERRUPTED,
        reviewers=_INTERRUPTED_REVIEWS,
    )
    assert result.attempts_used == 3
    # ``outcome.json`` takes its numbers from the run row, which is the
    # durable counter rather than this process's turn list.
    outcome = json.loads(_read(session, run, settings, "outcome.json"))
    assert outcome["attempts"] == 3, outcome
    assert outcome["review_cycles"] == 2, outcome
    assert [r["cycle"] for r in outcome["reviews"]] == [1, 2], outcome
    fix_loop = json.loads(_read(session, run, settings, FIX_LOOP_ARTIFACT))
    assert fix_loop["attempts_used"] == 3
    assert fix_loop["cycles_used"] == 2
    # The recovery itself is reported, so a reader of the artifact can see the
    # run was resumed rather than infer it from a missing iteration.
    assert fix_loop["recovery"]["recovered"] is True
    assert fix_loop["recovery"]["interrupted_attempt"] == 2


@pytest.mark.asyncio
async def test_a_resumed_run_with_no_attempts_left_escalates_with_the_truth(world: _World):
    """An escalation from a resumed run states how many attempts were made.

    The escalation summary is what a person reads, so a count of one for a run
    that tried twice is the most expensive version of this bug. This task is
    given two attempts and both are spent, so the resumed loop has nothing left
    to try and must escalate -- and say that both attempts were made, rather than
    the one this process managed.
    """
    session, settings = world.session, world.settings
    run = world.make_run()
    session = world.session
    task = world.make_task(
        external_task_id="TS-005",
        limits=TaskLimits(
            max_attempts=2, max_review_cycles=2, max_files_changed=3, max_diff_lines=200
        ),
    )
    run = create_run(session, task.id)
    await _strand_correction_timeout(
        session,
        run,
        settings,
        first_code=_FIRST_CANDIDATE,
        timed_out_code=_timeout(),
        first_review=_CHANGES_REQUESTED,
    )

    result = await run_fix_loop(
        session,
        attach_workspace(session, run.id, settings=settings),
        coder=ScriptedModel(_CORRECTED),
        reviewer=reviewer(_APPROVED),
        settings=settings,
        checkpoint_turn=session.commit,
    )
    assert result.outcome is LoopOutcome.ESCALATED
    assert result.failure_reason is FailureReason.RETRY_EXHAUSTED
    assert result.attempts_used == 2
    assert result.escalation is not None
    assert "2 of the task's 2 permitted attempts" in result.escalation.summary
    # The escalation is checkable rather than merely asserted: the turns it
    # describes, including the one that was lost, are on the record.
    recovered = recover_loop_state(session, run, settings=settings)
    assert [turn.attempt for turn in recovered.turns] == [1, 2]
    assert TaskRunRepository(session).get(run.id).status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_a_correction_that_survives_a_restart_reaches_verification_and_review(world: _World):
    """The happy path still works, and it goes all the way.

    A durability fix that only ever escalated would pass every test above. This
    one asserts the ordinary ending of the same story: the resumed attempt passes
    the project's own commands, the reviewer approves, the task is approved, and
    the verification rows for the resumed attempt are on the record.
    """
    session, settings = world.session, world.settings
    run = world.make_run()
    result = await _interrupt_then_resume(
        session,
        run,
        settings,
        coders=_INTERRUPTED,
        reviewers=_INTERRUPTED_REVIEWS,
    )
    assert result.outcome is LoopOutcome.APPROVED
    assert result.task_status is TaskStatus.APPROVED
    assert result.review is not None
    assert result.review.result.decision.value == "APPROVED"
    assert result.escalation is None
    passed = [
        row
        for row in VerificationRunRepository(session).list_for_run(run.id)
        if row.status is VerificationStatus.PASSED and row.verification_type.value == "TESTS"
    ]
    assert passed, "the resumed attempt's tests were not recorded"
    assert {row.task_run_id for row in passed} == {run.id}


# ------------------------------------------------------ a real process restart

_DRIVER = '''
"""Drive one phase of the fix loop in its own interpreter, then exit.

A file run as a subprocess rather than imported, because the property under test
is that a *different process* can pick the run up. Importing would share an
engine, a session and a heap, and would prove nothing about what survived.
"""

import asyncio
import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import uuid

REPO_ROOT = {repo_root!r}
ROOT = pathlib.Path({root!r})
#: The endpoint belongs to the test process, which is the point: it outlives
#: the interpreters below the way a served model outlives a request, and it is
#: told to go quiet for exactly one of them.
BASE_URL = {base_url!r}
TIMEOUT = {timeout!r}
REPO = ROOT / "tracestack"
sys.path.insert(0, REPO_ROOT)

from sqlalchemy.orm import sessionmaker

from apps.orchestrator.agents.coding_agent import run_coding_attempt
from apps.orchestrator.agents.fix_loop import run_fix_loop
from apps.orchestrator.agents.review_agent import run_review
from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.agents.review_prompts import (
    REVIEWER_SYSTEM_PROMPT,
    render_review_instructions,
)
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.enums import Complexity, ModelRole, TaskStatus, WorkerProfile
from apps.orchestrator.domain.models import Project, Task, TaskLimits
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.providers import OpenAICompatibleProvider, ProviderConfig
from apps.orchestrator.providers.errors import ModelTimeout
from apps.orchestrator.providers.review import ModelReviewProvider, ReviewerUnavailable
from apps.orchestrator.repositories import ProjectRepository, TaskRepository, TaskRunRepository
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.verification import verify_candidate
from apps.orchestrator.services.workspace import attach_workspace, prepare_workspace
PYTHON = "python3"
STUB = "def navigate(target):\\n    pass  # TODO: TS-004\\n"
COMPILE = """\\
import sys
source = open("src/nav.py").read()
if "SYNTAX ERROR" in source:
    print("src/nav.py:1: invalid syntax", file=sys.stderr)
    sys.exit(2)
print("compiled 1 file")
"""
TESTS = """\\
import sys
source = open("src/nav.py").read()
if "return None" in source:
    print("FAILED tests/test_nav.py::test_navigate - AssertionError: got None", file=sys.stderr)
    sys.exit(1)
print("2 passed")
"""


def git(*args):
    subprocess.run(("git", *args), cwd=REPO, check=True, capture_output=True)


def live_provider(role):
    """A provider with nothing stubbed, pointed at the test's endpoint."""
    return OpenAICompatibleProvider(
        ProviderConfig(
            provider_id="local-" + role.value.casefold(),
            base_url=BASE_URL,
            model_name=role.value.casefold() + "-test",
            role=role,
            context_window=32768,
            timeout_seconds=TIMEOUT,
        )
    )


def settings():
    return Settings(
        _env_file=None,
        artifact_root=ROOT / "data",
        worktree_root=ROOT / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )


def write_report(name, payload):
    (ROOT / f"report-{{name}}.json").write_text(json.dumps(payload), encoding="utf-8")


def setup(factory):
    (REPO / "src").mkdir(parents=True)
    (REPO / "tools").mkdir()
    (REPO / "src" / "nav.py").write_text(STUB, encoding="utf-8")
    (REPO / "tools" / "compile.py").write_text(COMPILE, encoding="utf-8")
    (REPO / "tools" / "test.py").write_text(TESTS, encoding="utf-8")
    git("init", "--initial-branch=main", "--quiet")
    git("add", "-A")
    git("commit", "--quiet", "-m", "Initial commit")
    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(
                name="TraceStack",
                repository_path=str(REPO),
                default_branch="main",
                worker_profile=WorkerProfile.PYTHON,
                verification=VerificationProfile(
                    build=(PYTHON + " tools/compile.py",),
                    tests=(PYTHON + " tools/test.py",),
                ),
            )
        )
        tasks = TaskRepository(session)
        task = tasks.add(
            Task(
                project_id=project.id,
                external_task_id="TS-004",
                title="Reject a null navigation target",
                instructions="navigate must return its target and reject a null one.",
                complexity=Complexity.LOW,
                files_to_modify=["src/nav.py"],
                limits=TaskLimits(
                    max_attempts=3,
                    max_review_cycles=2,
                    max_files_changed=3,
                    max_diff_lines=200,
                ),
            )
        )
        tasks.transition(task.id, TaskStatus.READY)
        run = create_run(session, task.id)
    write_report("setup", {{"run_id": str(run.id)}})


def main():
    phase = sys.argv[1]
    engine = create_db_engine("sqlite:///" + str(ROOT / "resume.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    if phase == "setup":
        setup(factory)
        return

    run_id = uuid.UUID(json.loads((ROOT / "report-setup.json").read_text())["run_id"])
    with factory() as session:
        coder = live_provider(ModelRole.CODER)
        judge = ModelReviewProvider(
            live_provider(ModelRole.REVIEWER),
            system_prompt=REVIEWER_SYSTEM_PROMPT,
            instruction_renderer=render_review_instructions,
        )
        if phase == "interrupted":
            workspace = prepare_workspace(session, run_id, settings=settings())

            async def interrupt():
                first_attempt = await run_coding_attempt(
                    session,
                    workspace,
                    provider=coder,
                    settings=settings(),
                    review_cycle=1,
                    checkpoint_call=session.commit,
                )
                report = verify_candidate(session, workspace, settings=settings())
                review = await run_review(
                    session,
                    workspace,
                    provider=judge,
                    verification=report,
                    completion_report=first_attempt.report,
                    settings=settings(),
                    checkpoint_call=session.commit,
                )
                session.commit()
                TaskRunRepository(session).update_fields(run_id, attempt_number=2)
                session.commit()
                await run_coding_attempt(
                    session,
                    workspace,
                    provider=coder,
                    settings=settings(),
                    review_feedback=review.feedback,
                    plan_required=False,
                    review_cycle=2,
                    checkpoint_call=session.commit,
                )

            try:
                asyncio.run(interrupt())
            except ModelTimeout:
                session.rollback()
                write_report("interrupted", {{"raised": "ModelTimeout"}})
                return
            raise AssertionError("interrupted phase did not time out")
        else:
            workspace = attach_workspace(session, run_id, settings=settings())
        result = asyncio.run(
            run_fix_loop(
                session,
                workspace,
                coder=coder,
                reviewer=judge,
                settings=settings(),
                checkpoint_turn=session.commit,
            )
        )
        write_report(phase, {{"result": result.describe()}})


main()
'''


def test_a_process_restart_resumes_the_interrupted_correction(tmp_path: Path, endpoint):
    """The whole defect, with a real process boundary in the middle.

    Three interpreters, in order: one sets the run up, one loses the correction
    to a provider timeout, one picks it up. The middle process exits while the
    correction is in flight -- not "raises and unwinds", *exits*, with the
    database committed as far as the loop got -- so nothing can be carried over
    in memory even by accident. The assertions are that the third process's
    prompt carries the reviewer's findings, that the attempt numbering did not
    reuse a number or a directory, and that the run finishes approved.

    None of the three interpreters is talking to a stub: they are clients of
    the same loopback endpoint this file runs, which goes quiet for exactly one
    request. The endpoint outliving the interpreter that timed out is the part
    the real run got for free from a served model, and having it here means the
    timeout is the provider's own rather than an exception a fixture threw.

    The first phase is a whole process rather than a fixture because the run has
    to exist in the same database the other two will open, and creating it in
    this process would mean a fixture session holding it: exactly the shared
    state the test is meant not to have.
    """
    repo_root = Path(__file__).resolve().parents[2]
    root = tmp_path / "restart"
    root.mkdir()
    live = endpoint(_LIVE_SCRIPT)
    driver = root / "driver.py"
    driver.write_text(
        _DRIVER.format(
            repo_root=str(repo_root),
            root=str(root),
            base_url=live.base_url,
            timeout=live.timeout_seconds,
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(repo_root), "PYTHONDONTWRITEBYTECODE": "1"}

    def phase(name: str) -> None:
        completed = subprocess.run(
            [sys.executable, str(driver), name, live.base_url, str(live.timeout_seconds)],
            capture_output=True,
            text=True,
            env=env,
            cwd=repo_root,
            timeout=300,
        )
        assert completed.returncode == 0, (
            f"phase {name} failed\n{completed.stdout}\n{completed.stderr}"
        )

    def report(name: str) -> dict:
        return json.loads((root / f"report-{name}.json").read_text(encoding="utf-8"))

    phase("setup")
    phase("interrupted")
    assert report("interrupted")["raised"] == "ModelTimeout"
    # One stall, from the endpoint's side, in the process that then exits.
    assert live.stalls == 1
    assert live.asked[2]["model"] == "coder-test"
    phase("resumed")
    # The third interpreter was served by the same endpoint, still answering.
    assert live.stalls == 1
    assert len(live.asked) == 5

    resumed = report("resumed")["result"]
    assert resumed["outcome"] == "APPROVED", resumed
    assert resumed["attempts_used"] == 3, resumed
    assert resumed["cycles_used"] == 2, resumed
    recovery = resumed["recovery"]
    assert recovery["recovered"] is True
    assert recovery["interrupted_attempt"] == 2
    assert recovery["next_attempt"] == 3

    # A separate engine on the same file, opened after the fact, is what a
    # restarted orchestrator would read.
    engine = create_db_engine(f"sqlite:///{root / 'resume.db'}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as verify_session:
            run_id = uuid.UUID(report("setup")["run_id"])
            run_row = TaskRunRepository(verify_session).get(run_id)
            run_root = root / "data" / "runs" / run_row.external_run_id
            calls = [
                call
                for call in ModelRunRepository(verify_session).list_for_run(run_id)
                if call.purpose.value in {"CODE", "FIX"}
            ]
            assert [call.attempt for call in calls] == [1, 2, 3]
            failed = [call for call in calls if call.status.value == "FAILED"]
            assert [call.attempt for call in failed] == [2]
            assert failed[0].review_cycle == 2
            assert "did not respond" in (failed[0].error_detail or "")
            reviews = ReviewRepository(verify_session).list_for_run(run_id)
            assert [r.cycle for r in reviews] == [1, 2]
            assert [r.decision.value for r in reviews] == [
                "CHANGES_REQUESTED",
                "APPROVED",
            ]
            assert run_row.attempt_number == 3
            assert run_row.review_cycle == 2

            # The artifacts of both attempts are on disk, and only the resumed
            # prompt carries the correction -- the first attempt's prompt is the
            # one that could not have.
            first = (run_root / "attempt-2-cycle-2" / "prompt.txt").read_text(encoding="utf-8")
            third = (run_root / "attempt-3-cycle-2" / "prompt.txt").read_text(encoding="utf-8")
            assert _MISSING_GUARD["problem"] in first
            assert _MISSING_GUARD["problem"] in third
            assert _MISSING_GUARD["requiredFix"] in third
    finally:
        engine.dispose()


# ---------------------------------------------------------------- Concern 66


@pytest.mark.asyncio
async def test_model_wait_has_no_open_database_transaction(world: _World):
    """The provider boundary is outside the transaction, not just a fast wait."""
    session, settings = world.session, world.settings
    run = world.make_run()

    class TransactionProbe(ScriptedModel):
        async def generate(self, request):  # type: ignore[no-untyped-def]
            assert not session.in_transaction(), (
                "a database transaction crossed the model-provider boundary"
            )
            return await super().generate(request)

    scripted_reviewer = reviewer(_APPROVED)

    class ReviewTransactionProbe:
        config = scripted_reviewer.config

        async def review(self, request):  # type: ignore[no-untyped-def]
            assert not session.in_transaction(), (
                "a database transaction crossed the review-provider boundary"
            )
            return await scripted_reviewer.review(request)

    result = await run_fix_loop(
        session,
        prepare_workspace(session, run.id, settings=settings),
        coder=TransactionProbe(_FIRST_CANDIDATE),
        reviewer=ReviewTransactionProbe(),  # type: ignore[arg-type]
        settings=settings,
        checkpoint_turn=session.commit,
    )

    assert result.outcome is LoopOutcome.APPROVED
    assert [call.status.value for call in _calls(session, run)] == ["SUCCEEDED"]


@pytest.mark.asyncio
async def test_invalidating_the_pre_call_session_cannot_lose_the_model_result(
    world: _World,
):
    """Post-call work starts on a valid checkout after a stale one is discarded."""
    session, settings = world.session, world.settings
    run = world.make_run()

    class InvalidatingCoder(ScriptedModel):
        async def generate(self, request):  # type: ignore[no-untyped-def]
            assert not session.in_transaction()
            session.invalidate()
            return await super().generate(request)

    result = await run_fix_loop(
        session,
        prepare_workspace(session, run.id, settings=settings),
        coder=InvalidatingCoder(_FIRST_CANDIDATE),
        reviewer=reviewer(_APPROVED),
        settings=settings,
        checkpoint_turn=session.commit,
    )

    assert result.outcome is LoopOutcome.APPROVED
    assert TaskRunRepository(session).get(run.id).attempt_number == 1
    assert [call.status.value for call in _calls(session, run)] == ["SUCCEEDED"]


@pytest.mark.asyncio
async def test_a_late_model_answer_is_refused_before_it_is_recorded(world: _World):
    """The post-call fence, independently of the turn checkpoint fence."""
    session, settings = world.session, world.settings
    run = world.make_run()
    concurrent = sessionmaker(bind=session.get_bind(), expire_on_commit=False)

    class AbandonWhileCalled(ScriptedModel):
        async def generate(self, request):  # type: ignore[no-untyped-def]
            assert not session.in_transaction()
            with concurrent.begin() as operator:
                abandon_run(operator, run.id, reason="stop the in-flight model")
            return await super().generate(request)

    with pytest.raises(AbandonedRunError):
        await run_fix_loop(
            session,
            prepare_workspace(session, run.id, settings=settings),
            coder=AbandonWhileCalled(_FIRST_CANDIDATE),
            reviewer=reviewer(_APPROVED),
            settings=settings,
            # Deliberately unguarded: this test pins _generate_recorded's own
            # post-call validation rather than durable_checkpoint's duplicate.
            checkpoint_turn=session.commit,
        )
    session.rollback()

    assert TaskRunRepository(session).get(run.id).status is RunStatus.ABANDONED
    assert _calls(session, run) == []


@pytest.mark.asyncio
async def test_timeout_longer_than_idle_limit_is_durable_without_a_long_sleep(
    world: _World,
):
    """A simulated 600s call outlives the DB limit while no transaction is idle."""
    session = world.session
    settings = world.settings.model_copy(
        update={"db_idle_in_transaction_timeout_seconds": 0.001}
    )
    run = world.make_run()

    class LongTimeout(ScriptedModel):
        async def generate(self, request):  # type: ignore[no-untyped-def]
            assert not session.in_transaction()
            raise _timeout()

    with pytest.raises(ModelTimeout):
        await run_coding_attempt(
            session,
            prepare_workspace(session, run.id, settings=settings),
            provider=LongTimeout(),
            settings=settings,
            checkpoint_call=session.commit,
        )
    session.rollback()

    assert action_for(FailureReason.MODEL_TIMEOUT) is FailureAction.RETRY
    failed = _calls(session, run)
    assert len(failed) == 1 and failed[0].status is RunStatus.FAILED
    assert recover_loop_state(session, run, settings=settings).next_attempt == 2


@pytest.mark.asyncio
async def test_build_failure_then_slow_second_call_timeout_is_recoverable(
    world: _World,
):
    """Regression for RUN-20260928-000005, with bounded deterministic timing."""
    session, settings = world.session, world.settings
    run = world.make_run()

    class SlowSecondCall(ScriptedModel):
        async def generate(self, request):  # type: ignore[no-untyped-def]
            if len(self.answers) == 1:
                assert not session.in_transaction()
                await asyncio.sleep(0.02)
            return await super().generate(request)

    await _strand_correction_timeout(
        session,
        run,
        settings,
        first_code=_code("SYNTAX ERROR\n"),
        timed_out_code=_timeout(),
        first_review=_APPROVED,
        timeout_model=SlowSecondCall,
    )

    events = RunEventRepository(session).list_for_run(run.id)
    assert RunEventType.BUILD_FAILED in [event.event_type for event in events]
    assert action_for(FailureReason.BUILD_FAILED) is FailureAction.SEND_TO_CODER
    calls = _calls(session, run)
    assert [call.attempt for call in calls] == [1, 2]
    assert [call.status for call in calls] == [RunStatus.SUCCEEDED, RunStatus.FAILED]
    recovered = recover_loop_state(session, run, settings=settings)
    assert recovered.attempts_started == 2
    assert recovered.next_attempt == 3
    assert recovered.interrupted_attempt == 2


# --- concern 79: the allowance survives the process that granted it ----------
#
# The allowance is one turn past a ceiling, which makes "how many have you had"
# the only question that matters, and an in-memory boolean answers it wrongly
# the moment the process dies. These two tests are the two places a crash can
# land: inside the repair, and after it.


def _grants(world: _World, run: TaskRun) -> list:
    return [
        event
        for event in RunEventRepository(world.session).list_for_run(run.id)
        if event.event_type == RunEventType.VERIFICATION_REPAIR_GRANTED
    ]


def _two_attempt_run(world: _World) -> TaskRun:
    """A run whose reviewer-driven correction is its last permitted attempt."""
    return world.make_run(
        world.make_task(
            limits=TaskLimits(
                max_attempts=2,
                max_review_cycles=3,
                max_files_changed=3,
                max_diff_lines=200,
            )
        )
    )


@pytest.mark.asyncio
async def test_a_crash_inside_the_repair_does_not_buy_a_second_one(world: _World):
    """G, first half. The grant is durable before the repair is attempted.

    The loop is killed in the middle of the repair turn -- the coder is asked
    and never answers at all -- which is the worst case for the allowance: it
    has been spent and has produced nothing. The resumed invocation reads the
    grant back off the run's own events and refuses, rather than starting the
    repair over as if it had never happened.
    """
    run = _two_attempt_run(world)
    workspace = prepare_workspace(world.session, run.id, settings=world.settings)

    with pytest.raises(AssertionError):
        await run_fix_loop(
            world.session,
            workspace,
            coder=ScriptedModel(_FIRST_CANDIDATE, _code(BROKEN)),
            reviewer=reviewer(_CHANGES_REQUESTED),
            settings=world.settings,
            checkpoint_turn=world.session.commit,
        )
    # Everything after the last turn boundary goes with the process.
    world.session.rollback()

    # The grant is on the record, because it was appended inside the turn that
    # granted it rather than after the repair it paid for.
    assert len(_grants(world, run)) == 1
    recovered = recover_loop_state(
        world.session,
        TaskRunRepository(world.session).get(run.id),
        settings=world.settings,
    )
    assert recovered.verification_repairs_granted == 1

    resumed_coder = ScriptedModel(_CORRECTED)
    result = await run_fix_loop(
        world.session,
        attach_workspace(world.session, run.id, settings=world.settings),
        coder=resumed_coder,
        reviewer=reviewer(_APPROVED),
        settings=world.settings,
        checkpoint_turn=world.session.commit,
    )

    assert result.outcome is LoopOutcome.ESCALATED
    assert result.failure_reason is FailureReason.RETRY_EXHAUSTED
    assert resumed_coder.requests == [], "the repair was not started over"
    assert result.iterations == ()
    assert len(_grants(world, run)) == 1, "the allowance was not granted twice"
    assert result.recovery is not None
    assert result.recovery.verification_repairs_granted == 1


@pytest.mark.asyncio
async def test_resuming_after_a_spent_repair_grants_nothing(world: _World):
    """G, second half. A repair that ran and failed is not re-run either.

    The first invocation uses the whole lifecycle: correction, grant, repair,
    and a repair the verifier rejects. A second invocation over the same run --
    an operator retrying, a scheduler re-dispatching -- must find a spent
    budget and a spent allowance.
    """
    run = _two_attempt_run(world)

    first = await run_fix_loop(
        world.session,
        prepare_workspace(world.session, run.id, settings=world.settings),
        coder=ScriptedModel(_FIRST_CANDIDATE, _code(BROKEN), _code(BROKEN)),
        reviewer=reviewer(_CHANGES_REQUESTED),
        settings=world.settings,
        checkpoint_turn=world.session.commit,
    )
    world.session.commit()

    assert first.verification_repairs_used == 1
    assert first.verification_repair_attempt == 3
    assert first.failure_reason is FailureReason.RETRY_EXHAUSTED
    assert len(_grants(world, run)) == 1

    second_coder = ScriptedModel(_CORRECTED)
    second = await run_fix_loop(
        world.session,
        attach_workspace(world.session, run.id, settings=world.settings),
        coder=second_coder,
        reviewer=reviewer(_APPROVED),
        settings=world.settings,
        checkpoint_turn=world.session.commit,
    )

    assert second.outcome is LoopOutcome.ESCALATED
    assert second.failure_reason is FailureReason.RETRY_EXHAUSTED
    assert second_coder.requests == []
    assert second.verification_repairs_used == 1, "read back, not re-granted"
    assert len(_grants(world, run)) == 1
