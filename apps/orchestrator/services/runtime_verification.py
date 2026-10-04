"""Executing a project's runtime contract (concern 81).

The pipeline before this one proves things about the source: it compiles, it
lints, its tests pass, its dependencies carry no known advisory. None of that
catches the defect this module exists for. The orchestrator's own UI campaign
reached ``COMPLETE`` with every check green while ``GET /api/projects`` on the
running application returned HTTP 200 with ``Content-Type: text/html`` -- the
single-page application's ``index.html`` served where the API should have been.
Only a running browser could see it.

So, when and only when a project declares one, this module honours a small
contract: start the application, wait for it to answer, open one page, observe
the handful of facts the contract names, and stop everything again.

The shape is three things kept apart on purpose:

* **``domain.runtime_contract``** owns what may be declared and what the
  observations mean. It is pure, so every assertion in this concern can be
  tested without a browser, a worker or a network.
* **``WorkerProbe``** owns the mechanism: a dependency-free Node program,
  copied into the worktree and run by the worker, that polls an HTTP URL or
  drives Chromium over the DevTools Protocol and prints facts. It asserts
  nothing.
* **this module** owns the lifecycle and the classification: start, readiness,
  observe, evaluate, stop, and the one distinction that must never blur --
  *the candidate failed its contract* against *the orchestrator could not
  check*. The first goes to the coder; the second fails closed as
  infrastructure, because sending a coder to fix an application that works
  because the image has no browser is worse than stopping.

Readiness polling is interleaved with liveness on purpose. Only this module can
see the managed background process, so it polls in short bounded chunks and
checks between them whether the application is still there: a server that died
on startup is reported immediately, with its exit code and its output, rather
than after the whole readiness timeout has elapsed.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from time import monotonic
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings
from ..domain.commands import ApprovedCommand, CommandPolicy, CommandRejected
from ..domain.enums import FailureReason, VerificationStatus
from ..domain.runtime_contract import (
    MAX_EVIDENCE_CHARS,
    MAX_EVIDENCE_CONSOLE_ENTRIES,
    MAX_EVIDENCE_REQUESTS,
    AssertionFailure,
    RuntimeContract,
    RuntimeObservation,
    evaluate,
)
from . import artifact_store
from .worker_errors import (
    BackgroundProcessCleanupFailed,
    BackgroundProcessStartFailed,
    WorkerBackendUnavailable,
    WorkerNotRunning,
)
from .worker_service import BackgroundProcess, Worker

logger = get_logger(__name__)

#: Where the probe is installed inside the run's worktree. The worker's only
#: writable mount is that worktree, and there is no ``docker cp`` in this
#: service's vocabulary, so the probe travels the same way a build's output
#: does -- and is removed on the way out, twice: explicitly here and again by
#: the candidate restore that concern 80 put around the whole command phase.
PROBE_DIRECTORY = ".orchestrator-runtime"
PROBE_SCRIPT = "probe.mjs"
#: The same program as it is stored in this repository, reviewed alongside
#: the service that runs it rather than built from a string at run time.
PROBE_ASSET = "runtime_probe.mjs"
PROBE_CONFIG = "probe.json"

#: The marker the probe prints its one machine-readable line behind.
PROBE_SENTINEL = "__ORCHESTRATOR_RUNTIME__"

#: One readiness poll's budget. The loop runs the probe for this long, then
#: comes back to ask whether the application is still alive. Short enough that
#: a server which died is noticed in seconds; long enough that a 120-second
#: readiness timeout costs a couple of dozen executions, not a thousand.
READINESS_CHUNK_SECONDS = 5

#: Ceiling on how long the browser observation may take, over and above the
#: contract's own readiness budget. Loading one page is not a long operation,
#: and a hung browser must not become the run's deadline.
OBSERVE_TIMEOUT_SECONDS = 180
BROWSER_START_TIMEOUT_SECONDS = 60
PAGE_TIMEOUT_SECONDS = 60

#: Bounded network quiet after the load event. A single-page application issues
#: its API calls *after* ``load``, so an observation that stopped there would
#: see none of them -- including the one this concern exists to catch.
SETTLE_QUIET_MS = 1_000
SETTLE_MAX_MS = 8_000

#: Ceilings the probe applies to what it collects, so the bound exists where
#: the data is produced rather than only where it is reported.
MAX_OBSERVED_RESPONSES = 200
MAX_PAGE_TEXT_CHARS = 20_000

#: Lines of the application's own output kept as evidence.
APPLICATION_OUTPUT_LINES = 40

#: Chromium, in the order the probe tries. ``CHROME_BIN`` first because the
#: worker image sets it to its own launcher, which is also what the image's
#: existing browser-test path uses; the rest are fallbacks for a differently
#: built image. No framework and no project configuration is involved.
CHROMIUM_CANDIDATES: tuple[str, ...] = (
    "/usr/local/bin/orchestrator-chromium",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
)

#: The probe's interpreter. Not a project command: the probe is this
#: repository's own program, so it is approved against the project's policy
#: *plus* this one name rather than requiring every manifest to allow it.
PROBE_EXECUTABLE = "node"


class RuntimeInfrastructureError(RuntimeError):
    """The orchestrator could not run the check, whatever the candidate does.

    Carried separately from a candidate failure all the way out: a browser that
    will not launch, a probe that could not be installed, a cleanup that could
    not be proven. These fail closed under ``WORKER_FAILURE``/``ERROR``
    semantics and never reach the coder as a defect in their code.
    """


@dataclass(frozen=True, slots=True)
class ReadinessPoll:
    """One bounded readiness attempt, as the probe reported it."""

    ready: bool
    status: int | None = None
    attempts: int = 0
    error: str = ""


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    """What one browser observation produced, or why it could not happen."""

    observation: RuntimeObservation | None = None
    infrastructure_error: str = ""
    output: str = ""


class RuntimeProbe(Protocol):
    """The mechanism, behind the smallest interface the lifecycle needs.

    Two methods, both of which only *report*. The default implementation runs
    the Node probe in the worker; a test supplies its own and exercises the
    whole lifecycle -- ordering, cleanup, classification, evidence -- without a
    browser.
    """

    def poll_readiness(self, url: str, *, budget_seconds: float) -> ReadinessPoll: ...

    def observe(self, contract: RuntimeContract) -> ProbeObservation: ...


@dataclass(frozen=True, slots=True)
class RuntimeOutcome:
    """The runtime step's verdict, ready for the pipeline to record.

    ``infrastructure`` is the field that matters: it is what turns into
    ``ERROR``/``WORKER_FAILURE`` rather than a failure the coder is asked to
    fix.
    """

    status: VerificationStatus
    detail: str = ""
    output: str = ""
    duration_ms: int = 0
    log_artifact: str | None = None
    infrastructure: bool = False
    evidence: Mapping[str, object] = field(default_factory=dict)

    @property
    def failure_reason_override(self) -> FailureReason | None:
        return FailureReason.WORKER_FAILURE if self.infrastructure else None


def verify_runtime(
    worker: Worker,
    contract: RuntimeContract,
    *,
    worktree: Path,
    deadline_monotonic: float | None = None,
    probe: RuntimeProbe | None = None,
) -> RuntimeOutcome:
    """Honour ``contract`` against the candidate in ``worker``'s worktree.

    The lifecycle, in the order it happens and with the cleanup it promises:
    install the probe, start the configured application, poll readiness while
    watching whether it is still alive, open the configured page, evaluate the
    contract, and -- in ``finally``, whatever happened above -- stop the
    browser, stop the application and remove the probe.

    Returns:
        The step's verdict. A candidate that failed its own contract is a
        ``FAILED`` outcome with evidence, not an exception.

    Raises:
        Nothing for a candidate failure. Infrastructure problems are returned
        as an ``ERROR`` outcome with ``infrastructure=True`` so the pipeline
        records them in order rather than losing the steps before them.
    """
    started = monotonic()
    evidence: dict[str, object] = {
        # The contract, with the environment reduced to its variable *names*.
        # A contract cannot declare a credential-shaped name, but nothing stops
        # a project putting a value somewhere it should not be, and the
        # orchestrator's own report is not the place that choice gets copied
        # to. Names are what diagnose a misconfiguration anyway.
        "contract": {**contract.describe(), "env": sorted(contract.env)},
        "application_command": contract.start,
        "readiness_url": contract.readiness_url,
    }
    probe_dir = worktree / PROBE_DIRECTORY
    application: BackgroundProcess | None = None
    transcript: list[str] = []

    try:
        runner = probe or _install_probe(worker, worktree, contract)
        application = _start_application(worker, contract)
        evidence["application_state"] = application.state.value

        readiness = _await_readiness(
            runner,
            application,
            contract,
            started=started,
            deadline_monotonic=deadline_monotonic,
            evidence=evidence,
            transcript=transcript,
        )
        if readiness is not None:
            return readiness

        if not contract.opens_a_browser:
            evidence["assertions"] = "none declared beyond readiness"
            return _passed(started, evidence, transcript)

        probed = runner.observe(contract)
        if probed.output:
            transcript.append(probed.output)
        if probed.infrastructure_error or probed.observation is None:
            raise RuntimeInfrastructureError(
                probed.infrastructure_error or "the runtime probe returned no observation"
            )
        observation = probed.observation
        evidence["observation"] = observation.describe()
        evidence["page"] = contract.page

        failures = evaluate(contract, observation)
        if failures:
            evidence["failed_assertions"] = [failure.describe() for failure in failures]
            evidence["application_state"] = application.state.value
            transcript.append(_application_evidence(application))
            return _failed(
                _render_assertion_failures(contract, failures),
                started,
                evidence,
                transcript,
            )
        return _passed(started, evidence, transcript)

    except RuntimeInfrastructureError as error:
        evidence["infrastructure_error"] = str(error)
        if application is not None:
            transcript.append(_application_evidence(application))
        return _error(str(error), started, evidence, transcript)
    except (
        CommandRejected,
        WorkerNotRunning,
        WorkerBackendUnavailable,
        BackgroundProcessStartFailed,
        ValueError,
    ) as error:
        # Every one of these is the orchestrator being unable to perform the
        # check: a command the policy refuses, a worker that is gone, a backend
        # with no background support, a process that could not be created.
        # None of them is evidence about the candidate's code.
        evidence["infrastructure_error"] = str(error)
        return _error(str(error), started, evidence, transcript)
    finally:
        _cleanup(worker, probe_dir, evidence)


# ------------------------------------------------------------------ lifecycle


def _start_application(worker: Worker, contract: RuntimeContract) -> BackgroundProcess:
    """Start the declared command as the worker's managed background process.

    Through the worker's primitive and through the project's own
    ``CommandPolicy``: there is no shell, nothing is backgrounded with ``&``,
    and the orchestrator owns the process rather than hoping it exits.
    """
    return worker.start_background(contract.start, environment=dict(contract.env))


def _await_readiness(
    runner: RuntimeProbe,
    application: BackgroundProcess,
    contract: RuntimeContract,
    *,
    started: float,
    deadline_monotonic: float | None,
    evidence: dict[str, object],
    transcript: list[str],
) -> RuntimeOutcome | None:
    """Poll until the application answers. ``None`` means it did.

    Bounded three ways, and the tightest wins: the contract's own timeout, the
    run's remaining worker deadline, and the liveness check between chunks.
    There is no unbounded retry and no sleep-only readiness -- an HTTP answer
    is the only thing that ends this loop successfully.
    """
    budget = float(contract.readiness_timeout_seconds)
    if deadline_monotonic is not None:
        budget = min(budget, max(0.0, deadline_monotonic - monotonic()))
    deadline = monotonic() + budget
    attempts = 0
    last = ReadinessPoll(ready=False, error="no readiness attempt was made")

    while True:
        if not application.is_running():
            # The one case that must not wait out the timeout: the application
            # is gone, so no amount of polling will ever succeed, and its exit
            # code and output are the diagnosis.
            evidence["application_state"] = application.state.value
            evidence["application_exit_code"] = application.exit_code
            transcript.append(_application_evidence(application))
            return _failed(
                f"the application exited before it became ready (exit code "
                f"{application.exit_code}); it was started with {contract.start!r}",
                started,
                evidence,
                transcript,
            )
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        attempts += 1
        last = runner.poll_readiness(
            contract.readiness_url,
            budget_seconds=min(float(READINESS_CHUNK_SECONDS), remaining),
        )
        if last.ready:
            evidence["readiness"] = {
                "ready": True,
                "status": last.status,
                "polls": attempts,
                "seconds": round(budget - max(0.0, deadline - monotonic()), 1),
            }
            return None

    evidence["readiness"] = {
        "ready": False,
        "polls": attempts,
        "timeout_seconds": contract.readiness_timeout_seconds,
        "last_error": _clip(last.error),
    }
    evidence["application_state"] = application.state.value
    transcript.append(_application_evidence(application))
    return _failed(
        f"the application did not answer {contract.readiness_url} within "
        f"{contract.readiness_timeout_seconds}s"
        + (f" (last attempt: {last.error})" if last.error else ""),
        started,
        evidence,
        transcript,
    )


def _cleanup(worker: Worker, probe_dir: Path, evidence: dict[str, object]) -> None:
    """Stop the application and remove the probe. Runs however we got here.

    The browser is stopped by the probe itself, inside its own ``finally``, and
    again by the worker's destruction behind that. The application is stopped
    here, and again by ``worker.close``, and again by the container's removal.
    A cleanup that *cannot be proven* taints the worker and is recorded: a
    process that might still be alive is an infrastructure problem, never a
    silent pass.
    """
    try:
        worker.terminate_background()
    except BackgroundProcessCleanupFailed as error:
        evidence["cleanup_error"] = str(error)
        logger.warning(
            "runtime_cleanup_unproven", worker_id=worker.spec.worker_id, error=str(error)
        )
    except Exception as error:  # noqa: BLE001 - cleanup must not mask a verdict
        evidence["cleanup_error"] = str(error)
    shutil.rmtree(probe_dir, ignore_errors=True)


# ---------------------------------------------------------------------- probe


def _install_probe(worker: Worker, worktree: Path, contract: RuntimeContract) -> RuntimeProbe:
    """Write the probe and its configuration into the worktree.

    Raises:
        RuntimeInfrastructureError: the probe could not be installed or its
            interpreter is not permitted in this worker.
    """
    directory = worktree / PROBE_DIRECTORY
    try:
        directory.mkdir(parents=True, exist_ok=True)
        source = (
            resources.files("apps.orchestrator.assets")
            .joinpath(PROBE_ASSET)
            .read_text(encoding="utf-8")
        )
        (directory / PROBE_SCRIPT).write_text(source, encoding="utf-8")
    except (OSError, ModuleNotFoundError) as error:
        raise RuntimeInfrastructureError(
            f"the runtime probe could not be installed into the worktree: {error}"
        ) from error
    return WorkerProbe(worker=worker, worktree=worktree, contract=contract)


@dataclass(frozen=True, slots=True)
class WorkerProbe:
    """The probe, run as a foreground command in the worker.

    One command per operation, each with its own timeout, each returning a
    single JSON line. The worker redacts secrets out of what it captures before
    this ever sees it (section 36), so neither the evidence nor the log can
    carry one.
    """

    worker: Worker
    worktree: Path
    contract: RuntimeContract

    def poll_readiness(self, url: str, *, budget_seconds: float) -> ReadinessPoll:
        payload = self._run(
            "ready",
            {"readiness_url": url, "readiness_budget_seconds": max(1.0, budget_seconds)},
            timeout_seconds=int(budget_seconds) + 20,
        )
        readiness = payload.get("readiness") if isinstance(payload, Mapping) else None
        if not isinstance(readiness, Mapping):
            # A readiness poll that produced no answer is treated as "not yet",
            # not as an infrastructure failure: the loop is bounded, and a
            # genuinely broken probe shows up as a readiness timeout whose
            # evidence names the reason.
            detail = ""
            if isinstance(payload, Mapping):
                detail = str(payload.get("infrastructure_error") or "")
            return ReadinessPoll(ready=False, error=detail or "the probe returned no result")
        return ReadinessPoll(
            ready=bool(readiness.get("ready")),
            status=(
                int(readiness["status"]) if isinstance(readiness.get("status"), int) else None
            ),
            attempts=int(readiness.get("attempts") or 0),
            error=str(readiness.get("error") or ""),
        )

    def observe(self, contract: RuntimeContract) -> ProbeObservation:
        payload, output = self._run(
            "observe",
            {
                "page": contract.page,
                "chromium_candidates": list(CHROMIUM_CANDIDATES),
                "browser_start_timeout_seconds": BROWSER_START_TIMEOUT_SECONDS,
                "page_timeout_seconds": PAGE_TIMEOUT_SECONDS,
                "settle_ms": SETTLE_QUIET_MS,
                "settle_max_ms": SETTLE_MAX_MS,
                "max_responses": MAX_OBSERVED_RESPONSES,
                "max_console": MAX_EVIDENCE_CONSOLE_ENTRIES,
                "max_text_chars": MAX_PAGE_TEXT_CHARS,
            },
            timeout_seconds=OBSERVE_TIMEOUT_SECONDS,
            with_output=True,
        )
        if not isinstance(payload, Mapping):
            return ProbeObservation(
                infrastructure_error="the runtime probe produced no parseable result",
                output=output,
            )
        if payload.get("infrastructure_error"):
            return ProbeObservation(
                infrastructure_error=str(payload["infrastructure_error"]), output=output
            )
        raw = payload.get("observation")
        if not isinstance(raw, Mapping):
            return ProbeObservation(
                infrastructure_error="the runtime probe reported no observation", output=output
            )
        return ProbeObservation(
            observation=RuntimeObservation.from_mapping(raw), output=output
        )

    # -- mechanics -----------------------------------------------------------

    def _run(
        self,
        mode: str,
        config: Mapping[str, object],
        *,
        timeout_seconds: int,
        with_output: bool = False,
    ):
        directory = self.worktree / PROBE_DIRECTORY
        try:
            (directory / PROBE_CONFIG).write_text(
                json.dumps(dict(config)), encoding="utf-8"
            )
        except OSError as error:
            raise RuntimeInfrastructureError(
                f"the runtime probe configuration could not be written: {error}"
            ) from error
        result = self.worker.run(self._command(mode), timeout_seconds=timeout_seconds)
        output = result.combined_output
        if result.timed_out:
            raise RuntimeInfrastructureError(
                f"the runtime probe ({mode}) was killed at its {timeout_seconds}s ceiling"
            )
        payload = _parse_sentinel(output)
        return (payload, output) if with_output else payload

    def _command(self, mode: str) -> ApprovedCommand:
        """The probe's own command, approved like any other.

        Approved against the project's policy widened by exactly one name --
        the probe's interpreter. The probe is this repository's program, not
        the project's, so requiring every manifest to allow ``node`` would make
        a project's allow-list a statement about the orchestrator's internals.
        Everything else policy enforces still applies: a parsed argument
        vector, no shell, no metacharacters, and the never-permitted floor.
        """
        policy = self.worker.policy
        widened = CommandPolicy(
            profile=policy.profile,
            extra_allowed=policy.extra_allowed | {PROBE_EXECUTABLE},
            allow_relative_scripts=policy.allow_relative_scripts,
        )
        script = f"{PROBE_DIRECTORY}/{PROBE_SCRIPT}"
        config = f"{PROBE_DIRECTORY}/{PROBE_CONFIG}"
        try:
            return widened.approve(f"{PROBE_EXECUTABLE} {script} {mode} {config}")
        except CommandRejected as error:
            raise RuntimeInfrastructureError(
                f"the runtime probe cannot run in this worker: {error}"
            ) from error


def _parse_sentinel(output: str) -> Mapping[str, object] | None:
    """The probe's one machine-readable line, out of everything it printed."""
    for line in reversed(output.splitlines()):
        marker = line.find(PROBE_SENTINEL)
        if marker == -1:
            continue
        try:
            payload = json.loads(line[marker + len(PROBE_SENTINEL) :].strip())
        except ValueError:
            return None
        return payload if isinstance(payload, Mapping) else None
    return None


# -------------------------------------------------------------------- outcomes


def _render_assertion_failures(
    contract: RuntimeContract, failures: tuple[AssertionFailure, ...]
) -> str:
    """The detail line a coder reads first.

    Written so this concern's own regression is unmissable: the request, the
    expected media type and the observed one, in one sentence.
    """
    lines = [
        f"the running application did not satisfy the project's runtime contract "
        f"at {contract.page}:"
    ]
    lines += [f"  - {failure.summary()}" for failure in failures[:MAX_EVIDENCE_REQUESTS]]
    if len(failures) > MAX_EVIDENCE_REQUESTS:
        lines.append(f"  - [... {len(failures) - MAX_EVIDENCE_REQUESTS} more ...]")
    return "\n".join(lines)


def _application_evidence(application: BackgroundProcess) -> str:
    return (
        f"--- application ({application.command.display}) ---\n"
        f"state: {application.state.value}, exit code: {application.exit_code}\n"
        f"{application.output_tail(APPLICATION_OUTPUT_LINES)}"
    )


def _passed(started: float, evidence: dict[str, object], transcript: list[str]) -> RuntimeOutcome:
    return RuntimeOutcome(
        status=VerificationStatus.PASSED,
        detail="the running application satisfied the project's runtime contract",
        output=_transcript(transcript),
        duration_ms=_elapsed(started),
        evidence=dict(evidence),
    )


def _failed(
    detail: str, started: float, evidence: dict[str, object], transcript: list[str]
) -> RuntimeOutcome:
    return RuntimeOutcome(
        status=VerificationStatus.FAILED,
        detail=detail,
        output=_transcript(transcript),
        duration_ms=_elapsed(started),
        evidence=dict(evidence),
    )


def _error(
    detail: str, started: float, evidence: dict[str, object], transcript: list[str]
) -> RuntimeOutcome:
    return RuntimeOutcome(
        status=VerificationStatus.ERROR,
        detail=f"runtime verification could not be performed: {detail}",
        output=_transcript(transcript),
        duration_ms=_elapsed(started),
        infrastructure=True,
        evidence=dict(evidence),
    )


def store_runtime_log(
    session: Session,
    task_run_id: UUID,
    outcome: RuntimeOutcome,
    *,
    prefix: str = "",
    settings: Settings | None = None,
) -> RuntimeOutcome:
    """Write the runtime step's transcript into the run directory.

    The same convention as every command log (section 9): the step carries a
    bounded tail for the prompt, the full transcript is on disk and referenced
    by path. No parallel reporting system.
    """
    body = "\n\n".join(
        part
        for part in (
            f"detail: {outcome.detail}",
            json.dumps(dict(outcome.evidence), indent=2, default=str),
            outcome.output,
        )
        if part
    )
    stored = artifact_store.write_text(
        session,
        task_run_id,
        f"{prefix}runtime/runtime.log",
        body,
        kind="runtime-log",
        settings=settings,
    )
    return replace(outcome, log_artifact=stored.relative_path)


def _transcript(parts: list[str]) -> str:
    return _clip("\n\n".join(part for part in parts if part.strip()), MAX_EVIDENCE_CHARS * 4)


def _elapsed(started: float) -> int:
    return int((monotonic() - started) * 1000)


def _clip(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


__all__ = [
    "APPLICATION_OUTPUT_LINES",
    "CHROMIUM_CANDIDATES",
    "PROBE_ASSET",
    "PROBE_DIRECTORY",
    "PROBE_SCRIPT",
    "PROBE_SENTINEL",
    "READINESS_CHUNK_SECONDS",
    "ProbeObservation",
    "ReadinessPoll",
    "RuntimeInfrastructureError",
    "RuntimeOutcome",
    "RuntimeProbe",
    "WorkerProbe",
    "store_runtime_log",
    "verify_runtime",
]
