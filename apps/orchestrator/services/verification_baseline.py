"""Baseline failure evidence, and classifying a candidate against it (concern 78, stage 2).

Stage 1 took baseline measurement away from the coder, on the grounds that it
is deterministic work the orchestrator does better. This module is where that
work actually happens, and it has two halves that must not be confused:

* **recording** what a verification command did against a *known tree*, which
  is how a baseline comes to exist at all;
* **classifying** a candidate's failures against a recorded baseline, which is
  how a non-zero exit code becomes either "you broke these three tests" or "the
  tree you started from was already failing these forty-two".

Three decisions, each of which the obvious alternative gets wrong.

**The baseline is never produced by disturbing the candidate.** Nothing here
stashes, reverts, checks out or otherwise touches the worktree being verified,
and nothing runs a second suite per candidate. Evidence comes from trees the
orchestrator was going to run commands against anyway -- the integration
worktree at the cumulative baseline (``services.integration``) -- and is keyed
by the commit, so it is measured once per baseline and reused by every
candidate that starts from it. That is the expensive manual behaviour stage 2
exists to delete, not to re-implement in the orchestrator.

**Provenance is matched exactly or not at all.** ``classify`` asks for the row
for this project, this ``baseline_sha``, this category and this command text.
A baseline for the previous commit is not an approximate baseline; it describes
a different tree, and using it would attribute someone else's failures to this
candidate or hide this candidate's own. A miss is ``UNCLASSIFIED_FAILURE``.

**Only evidence can make a failure known.** Every path that cannot produce a
complete set of candidate failure identities -- a timeout, a clipped log, a
category with no notion of a test identity, a runner no adapter reads --
returns an unavailable comparison. The candidate then behaves exactly as it did
before stage 2: a deterministic failure, sent back to the coder with the
command's own output.

What this module does **not** do: decide anything. It produces a
``FailureComparison``; what a classification means for review, retry or
delivery stays with the pipeline and the fix loop, and no model is consulted at
any point here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.enums import VerificationStatus, VerificationType, WorkerProfile
from ..domain.failure_identity import (
    FailureComparison,
    compare,
    extract_failure_identities,
    unavailable,
)
from ..domain.models import Project, TaskRun, VerificationBaseline
from ..domain.verification import COMMAND_CATEGORIES, VerificationProfile
from ..repositories import VerificationBaselineRepository
from .command_execution import CommandExecution, execute_commands
from .dependency_bootstrap import NETWORKLESS_VERIFICATION_NETWORK
from .worker_service import worker_session

logger = get_logger(__name__)

#: Categories whose output can carry stable per-failure identities.
#:
#: Only ``TESTS``. A failing build or a failing linter has no equivalent of a
#: test node id that survives into the next run, and inventing one from a file
#: and line number would make a shifted line look like a new failure and a
#: moved error look like a resolved one. A candidate whose build breaks is
#: therefore ``UNCLASSIFIED_FAILURE``, which is correct: the orchestrator does
#: not know that the baseline's build broke in the same way, and a broken build
#: is not a condition to wave through on a resemblance.
IDENTIFIABLE_CATEGORIES: frozenset[VerificationType] = frozenset(
    {VerificationType.TESTS}
)

#: Where a deliberate baseline capture files its logs under the run it borrowed.
BASELINE_LOG_PREFIX = "baseline/"


# ------------------------------------------------------------------ recording


def record_executions(
    session: Session,
    *,
    project_id: UUID,
    baseline_sha: str,
    worker_profile: WorkerProfile,
    entries: Sequence[tuple[VerificationType, CommandExecution]],
    source_task_run_id: UUID | None = None,
) -> tuple[VerificationBaseline, ...]:
    """Store what these commands did against the tree at ``baseline_sha``.

    Called with the executions of a run the orchestrator performed for its own
    reasons, so a baseline costs nothing beyond the row. Each entry is recorded
    whether it passed or failed; a passing command is as much a fact about the
    tree as a failing one, and is the evidence that later turns *any* failure of
    that command into a regression.

    A failing command whose output no adapter can read is recorded with
    ``failures_available=False`` rather than dropped. The distinction matters at
    lookup time: a missing row and an unreadable row both classify as
    unclassified, but only the stored one tells a later reader that the tree was
    measured and the measurement was not usable.
    """
    repository = VerificationBaselineRepository(session)
    recorded: list[VerificationBaseline] = []
    for category, execution in entries:
        result = execution.result
        identities: frozenset[str] = frozenset()
        extractor: str | None = None
        available = True
        if not execution.succeeded:
            read = (
                None
                if result.timed_out or result.truncated
                else extract_failure_identities(result.combined_output)
            )
            if read is None:
                available = False
            else:
                extractor, identities = read
        recorded.append(
            repository.record(
                VerificationBaseline(
                    project_id=project_id,
                    baseline_sha=baseline_sha,
                    verification_type=category,
                    command=result.command.source,
                    worker_profile=worker_profile,
                    status=(
                        VerificationStatus.PASSED
                        if execution.succeeded
                        else VerificationStatus.FAILED
                    ),
                    failure_identities=sorted(identities),
                    failures_available=available,
                    extractor=extractor,
                    exit_code=result.exit_code,
                    stdout_artifact=execution.log_artifact,
                    source_task_run_id=source_task_run_id,
                )
            )
        )
    logger.info(
        "verification_baseline_recorded",
        project_id=str(project_id),
        baseline_sha=baseline_sha,
        commands=len(recorded),
        unusable=sum(1 for entry in recorded if not entry.usable),
    )
    return tuple(recorded)


def is_recorded(
    session: Session,
    project: Project,
    baseline_sha: str,
    *,
    profile: VerificationProfile | None = None,
) -> bool:
    """Has this tree already been measured for every command the project declares?

    The reuse predicate, and the reason it asks about *presence* rather than
    usability. Certification answers "has this tree been measured"; the
    classification in ``classify`` separately answers "is the measurement good
    enough to rely on". Conflating them would make a tree whose suite nobody
    can parse be re-measured at the start of every single task -- a full suite
    per candidate, forever, to reach the same unusable answer. So an unreadable
    row still counts as measured here, and still fails closed there.

    A missing row for any declared command means not certified, which is how
    every invalidation works without an invalidation rule of its own: the
    baseline advanced (new ``baseline_sha``), the manifest changed a command
    (new ``command``), or the project changed image (new ``worker_profile``) --
    each one simply misses, and the tree is measured again.
    """
    resolved = profile if profile is not None else project.verification
    if resolved.is_empty:
        return True
    repository = VerificationBaselineRepository(session)
    return all(
        repository.find(
            project.id, baseline_sha, category, command, project.worker_profile
        )
        is not None
        for category in COMMAND_CATEGORIES
        for command in resolved.commands_for(category)
    )


def capture_baseline(
    session: Session,
    project: Project,
    run: TaskRun,
    *,
    worktree_path: Path,
    baseline_sha: str,
    profile: VerificationProfile | None = None,
    settings: Settings | None = None,
    secrets: Mapping[str, str] | None = None,
) -> tuple[VerificationBaseline, ...]:
    """Measure a tree the orchestrator already has, and record it.

    The deliberate entry point, for establishing a baseline for a project whose
    tree is not green -- which is the normal case, and the one the opportunistic
    recording in ``services.integration`` cannot cover, because a tree with
    pre-existing failures never passes the cumulative gate and so never becomes
    a baseline through it.

    Two things differ from every other command run in this codebase, and both
    are deliberate:

    * ``stop_on_failure=False``. Everywhere else, stopping at the first failure
      is right: a candidate that does not compile has nothing to say about its
      tests. Here the *whole point* is the complete failure set, and a run that
      stopped at the build would record a baseline with no test evidence in it.
    * the categories are not short-circuited. Same reason.

    ``worktree_path`` must already be checked out at ``baseline_sha``; this
    function does not move Git and will not reconcile a mismatch. Callers hold
    a detached, disposable worktree at a known commit -- the integration
    worktree is exactly that -- and the commit they name is the provenance every
    later classification is matched against.
    """
    config = settings or get_settings()
    resolved = profile if profile is not None else project.verification
    if resolved.is_empty:
        logger.warning(
            "verification_baseline_skipped",
            project_id=str(project.id),
            baseline_sha=baseline_sha,
            detail="the project declares no verification commands",
        )
        return ()

    entries: list[tuple[VerificationType, CommandExecution]] = []
    with worker_session(
        worktree_path,
        profile=project.worker_profile,
        settings=config,
        secrets=secrets,
        worker_network=NETWORKLESS_VERIFICATION_NETWORK,
    ) as worker:
        for category in COMMAND_CATEGORIES:
            commands = resolved.commands_for(category)
            if not commands:
                continue
            executions = execute_commands(
                session,
                worker,
                run.id,
                commands,
                category=f"baseline-{category.value.casefold()}",
                prefix=BASELINE_LOG_PREFIX,
                settings=config,
                stop_on_failure=False,
            )
            entries.extend((category, execution) for execution in executions)
    return record_executions(
        session,
        project_id=project.id,
        baseline_sha=baseline_sha,
        worker_profile=project.worker_profile,
        entries=entries,
        source_task_run_id=run.id,
    )


# -------------------------------------------------------------- classification


def classify(
    session: Session,
    *,
    project_id: UUID,
    baseline_sha: str | None,
    worker_profile: WorkerProfile,
    executions: Sequence[tuple[VerificationType, CommandExecution]],
) -> FailureComparison:
    """Candidate failures against the recorded baseline, or an honest refusal.

    ``executions`` is every command the pipeline ran in a command category,
    passing ones included: a command that passed here and failed in the
    baseline is how a *resolved* pre-existing failure is detected, and leaving
    it out would make the comparison read as though the baseline failure had
    simply vanished from both sides.

    Every refusal path returns ``unavailable`` with the reason in it. There is
    no path that returns a comparison built from a partial candidate set, which
    is the one bug that would matter: a set missing one failure classifies a
    real regression as known.
    """
    failing = [
        (category, execution)
        for category, execution in executions
        if not execution.succeeded
    ]
    if not failing:
        return unavailable("no command failed; nothing to compare", baseline_sha=baseline_sha)
    if not baseline_sha:
        return unavailable("the run records no starting commit")

    repository = VerificationBaselineRepository(session)
    baseline: set[str] = set()
    candidate: set[str] = set()
    compared: list[str] = []
    extractors: list[str] = []

    for category, execution in failing:
        result = execution.result
        if category not in IDENTIFIABLE_CATEGORIES:
            return unavailable(
                f"{category.value.casefold()} failures have no stable failure "
                "identities to compare",
                baseline_sha=baseline_sha,
            )
        if result.timed_out:
            return unavailable(
                f"`{result.command.source}` was killed at its timeout and "
                "reported no failures to compare",
                baseline_sha=baseline_sha,
            )
        if result.truncated:
            return unavailable(
                f"the output of `{result.command.source}` was clipped, so its "
                "failure list cannot be treated as complete",
                baseline_sha=baseline_sha,
            )
        recorded = repository.find(
            project_id, baseline_sha, category, result.command.source, worker_profile
        )
        if recorded is None:
            return unavailable(
                f"no baseline evidence for `{result.command.source}` at "
                f"{baseline_sha}",
                baseline_sha=baseline_sha,
            )
        if not recorded.usable:
            return unavailable(
                f"the baseline for `{result.command.source}` at {baseline_sha} "
                "failed without identifiable failures",
                baseline_sha=baseline_sha,
            )
        read = extract_failure_identities(result.combined_output)
        if read is None:
            return unavailable(
                f"the failures of `{result.command.source}` could not be "
                "identified from its output",
                baseline_sha=baseline_sha,
            )
        extractor, identities = read
        extractors.append(extractor)
        candidate |= identities
        baseline |= recorded.identities
        compared.append(f"{category.value}:{result.command.source}")

    # A command that passed here but failed in the baseline: its baseline
    # failures are resolved, not missing. Only a row that exists and is usable
    # contributes -- a passing command needs no evidence to be believed, so a
    # missing row here makes the resolved set less complete rather than making
    # the whole comparison unavailable.
    for category, execution in executions:
        if not execution.succeeded or category not in IDENTIFIABLE_CATEGORIES:
            continue
        recorded = repository.find(
            project_id,
            baseline_sha,
            category,
            execution.result.command.source,
            worker_profile,
        )
        if recorded is not None and recorded.usable:
            baseline |= recorded.identities
            compared.append(f"{category.value}:{execution.result.command.source}")

    return compare(
        baseline=baseline,
        candidate=candidate,
        baseline_sha=baseline_sha,
        commands_compared=tuple(dict.fromkeys(compared)),
        extractors=extractors,
        detail=f"compared against the baseline recorded for {baseline_sha}",
    )


__all__ = [
    "BASELINE_LOG_PREFIX",
    "IDENTIFIABLE_CATEGORIES",
    "capture_baseline",
    "classify",
    "is_recorded",
    "record_executions",
]
