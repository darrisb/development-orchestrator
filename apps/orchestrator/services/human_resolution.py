"""Operator-assisted human conflict resolution (concern 73 follow-up).

Concern 73 made a human commit reach the cumulative baseline. It assumed the
commit would *merge* into that baseline. It did not ask what happens when it
does not, and the answer arrived as a surviving-Git fact: the historical human
commit ``cbff2c4`` is not a descendant of the canonical integration lineage
``fc6abc5``. It is a **sibling** of it, both descending from the pre-TS-101
imported commit ``06a0697``. Both sides appended methods to the same class and
blocks to the same test file, so ``git merge`` reports real content conflicts
and concern 73's contract fails closed. That is the correct outcome and it is
not a defect: the Git history was never lost, and this is simply what the
repository's real topology was.

So the missing thing is not a better merge. It is a **supported way for a
person to resolve the conflict and for the system to prove the resolution is
the human's work carried forward** -- with the human's original commit left
exactly as it is.

The provenance model
--------------------

Two different commits, two different meanings, and conflating them is the
defect this module exists to remove:

* ``HumanEscalation.human_commit`` keeps its meaning and gains none: the
  **historical human source commit**. ``cbff2c4`` forever, exactly as a person
  wrote it, never rebased and never re-signed.
* ``HumanEscalation.integration_resolution_commit`` is new: the **distinct
  commit** that actually carries that work onto the integration baseline.

The resolution is a genuine merge commit whose first parent is the pre-
reconciliation baseline and whose second parent is the human source commit.
That second parent is the whole point: it is what makes ``cbff2c4`` an
ancestor of ``agent/integration``, which is the property concern 73 exists to
guarantee. A resolution that merely *looked* like the human's work, or that
carried it without the ancestry, would satisfy neither the contract nor the
dependency guard that reads the baseline.

What is validated, and why it is not a patch-equality check
-----------------------------------------------------------

A conflict resolution legitimately relocates code. In the real case the human
appended ``filterBySource`` immediately after ``clear()``; the baseline had
appended ``find``/``contains``/``getMaxSize`` in that same position, so the
correct resolution puts ``filterBySource`` somewhere else in the file entirely.
Requiring the human's patch to apply byte-identically would therefore reject
the *right* answer.

What is enforceable -- and is checked here, from Git alone, with no operator
assertion -- is **line-level content equivalence within the human commit's own
file scope**. For every file ``F`` the human commit touched:

1. *carry-over* -- every significant line the human commit added to ``F`` is
   present in the resolved ``F``. This is what proves the resolution actually
   contains the intended implementation and tests, and it is what fails when a
   resolution silently drops part of the human's work.
2. *no injection* -- every significant line the resolution adds relative to the
   baseline was added by the human commit. Nothing unrelated can be smuggled in.
3. *no removal* -- every significant line the resolution drops relative to the
   baseline was dropped by the human commit. Accepted work cannot be deleted
   under cover of a merge.

Plus scope: the resolution may only differ from the baseline inside the files
the human commit touched. Plus the project's own build, lint and tests over
the resolved tree, which is not permitted to silently skip.

This is deliberately weaker than semantic equivalence of arbitrary rewrites,
and that limit is stated rather than papered over: an operator who rewrote the
human's method into different text would be rejected even if the code
behaved identically. The contract trades that false negative for a check that
is deterministic, language-agnostic, and cannot be satisfied by assertion. The
verification gate is what covers behaviour.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.enums import EscalationStatus, RunEventType, TaskStatus
from ..domain.escalation import EscalationIntent
from ..domain.git import INTEGRATION_BRANCH
from ..domain.models import HumanEscalation, Project, RunEvent, Task
from ..repositories import (
    EscalationRepository,
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
)
from .errors import EntityConflict, EntityNotFound
from .git_errors import GitError
from .git_service import GitService

# Shared with the canonical integration path on purpose. A conflict resolution
# is an advance of the same ref, so it is set up and measured by the same
# worktree preparation and the same cumulative gate; a second copy of either
# would be free to drift away from the thing it is supposed to match.
from .integration import (
    _copy_dependencies,
    _verify_cumulative_human,
    ensure_integration_branch,
)

logger = get_logger(__name__)

#: A Git commit id, unabbreviated. Provenance values are compared as strings
#: and stored in ``String(64)`` columns, so an abbreviation, a ref name, or an
#: expression like ``master~1`` is refused rather than resolved: this contract
#: is about naming specific objects a person can look up, not about accepting
#: whatever a revision happens to mean today.
FULL_COMMIT_SHA = re.compile(r"\A[0-9a-f]{40}\Z")

#: Where an escalation's resolution workspace lives, relative to the project.
#: Derived rather than stored, for the reason ``integration_worktree_path`` is.
RESOLUTION_WORKTREE_PREFIX = "_human-resolution"


def resolution_worktree_path(
    project_id: UUID, escalation_id: UUID, *, settings: Settings | None = None
) -> Path:
    """Where one escalation's conflict-resolution workspace lives."""
    config = settings or get_settings()
    return (
        config.worktree_root
        / str(project_id)
        / f"{RESOLUTION_WORKTREE_PREFIX}-{escalation_id}"
    )


@dataclass(frozen=True, slots=True)
class ResolutionVerdict:
    """What the content invariant found, per file and overall."""

    #: Paths the human commit touched, and therefore the only paths a
    #: resolution is allowed to differ from the baseline within.
    allowed_paths: tuple[str, ...]
    #: Human-readable reasons the resolution is not faithful. Empty means it is.
    violations: tuple[str, ...] = ()
    #: Per-file detail, for the operator and the log.
    carried_paths: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.violations

    def describe(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "allowed_paths": list(self.allowed_paths),
            "carried_paths": list(self.carried_paths),
            "violations": list(self.violations),
        }


@dataclass(frozen=True, slots=True)
class ResolutionWorkspace:
    """What an operator needs in order to resolve the conflict."""

    escalation_id: UUID
    task_external_id: str
    #: Absolute path of the workspace the operator edits.
    path: Path
    #: The baseline the workspace is built on and the resolution must descend
    #: from. The operator states it again at authorization; a mismatch means
    #: they resolved against something other than the current baseline.
    expected_integration_sha: str
    #: The human commit whose work is being carried forward. Read-only.
    human_source_commit: str
    #: The only files that may be modified.
    allowed_paths: tuple[str, ...]
    #: The human's own parent, so an operator can read the original patch.
    human_source_parent: str

    def describe(self) -> dict[str, object]:
        return {
            "escalation_id": str(self.escalation_id),
            "task": self.task_external_id,
            "path": str(self.path),
            "expected_integration_sha": self.expected_integration_sha,
            "human_source_commit": self.human_source_commit,
            "human_source_parent": self.human_source_parent,
            "allowed_paths": list(self.allowed_paths),
        }


# --------------------------------------------------------------------- content


def significant_lines(text: str | None) -> Counter[str]:
    """A file's content as a counted multiset of significant lines.

    Leading and trailing whitespace is dropped and blank lines are discarded,
    so the comparison answers "did this text survive" rather than "is this byte
    sequence in the same place". Placement and indentation are exactly the
    things a conflict resolution is allowed to change; wording is not.
    """
    if text is None:
        return Counter()
    return Counter(line.strip() for line in text.splitlines() if line.strip())


def check_resolution_content(
    *,
    allowed_paths: Sequence[str],
    human_added: Mapping[str, Sequence[str]],
    baseline_texts: Mapping[str, str | None],
    resolution_texts: Mapping[str, str | None],
    human_removed: Mapping[str, Counter[str]],
) -> ResolutionVerdict:
    """The resolution content invariant, as a pure function of four tree reads.

    Split out from the Git plumbing so the rule itself is testable without a
    repository, and so what is being asserted is legible in one place.
    """
    violations: list[str] = []
    carried: list[str] = []
    for path in allowed_paths:
        added = significant_lines("\n".join(human_added.get(path, ())))
        baseline = significant_lines(baseline_texts.get(path))
        resolved = significant_lines(resolution_texts.get(path))
        removed_by_human = human_removed.get(path, Counter())

        # 1. carry-over: the human's lines survived into the resolution.
        dropped = added - resolved
        if dropped:
            violations.append(
                f"{path}: the resolution omits {sum(dropped.values())} line(s) the "
                f"human commit added, first: {sorted(dropped)[0]!r}"
            )
        # 2. no injection: the resolution introduced nothing the human wrote.
        injected = (resolved - baseline) - added
        if injected:
            violations.append(
                f"{path}: the resolution adds {sum(injected.values())} line(s) the "
                f"human commit did not add, first: {sorted(injected)[0]!r}"
            )
        # 3. no removal: the resolution deleted nothing the human kept.
        lost = (baseline - resolved) - removed_by_human
        if lost:
            violations.append(
                f"{path}: the resolution removes {sum(lost.values())} baseline "
                f"line(s) the human commit kept, first: {sorted(lost)[0]!r}"
            )
        if not (dropped or injected or lost):
            carried.append(path)
    return ResolutionVerdict(
        allowed_paths=tuple(allowed_paths),
        violations=tuple(violations),
        carried_paths=tuple(carried),
    )


@dataclass(frozen=True, slots=True)
class ResolutionFacts:
    """The four tree reads the content invariant compares.

    Bundled rather than returned as a five-tuple so a caller cannot silently
    transpose two of the dictionaries, and so every field is named at the call
    site.
    """

    #: Paths the human commit touched: the only paths a resolution may differ
    #: from the baseline within.
    scope: tuple[str, ...]
    #: Significant lines the human commit added, per path.
    human_added: dict[str, tuple[str, ...]]
    #: File content at the baseline, per path. ``None`` means absent.
    baseline_texts: dict[str, str | None]
    #: File content in the candidate resolution tree, per path. ``None`` means
    #: the resolution deleted it, which is itself a violation.
    resolution_texts: dict[str, str | None]
    #: Significant lines the human commit removed, per path.
    human_removed: dict[str, Counter[str]]


def collect_resolution_facts(
    repository: GitService,
    *,
    baseline: str,
    human_source: str,
    tree: str,
) -> ResolutionFacts:
    """Read the four trees the invariant compares.

    *tree* is the candidate resolution's tree; *baseline* and *human_source*
    are commits. Every value is a pure function of Git objects: no operator
    input reaches this function, which is the point -- the invariant is
    evidence, not testimony.
    """
    parents = repository.commit_parents(human_source)
    if not parents:
        raise EntityConflict(
            f"Human source commit {human_source} is a root commit, so it carries "
            "no patch to carry forward"
        )
    human_parent = parents[0]
    scope = repository.list_changed_paths(human_parent, human_source)
    return ResolutionFacts(
        scope=scope,
        human_added={
            path: repository.added_lines_between(human_parent, human_source, path)
            for path in scope
        },
        baseline_texts={path: repository.read_file_at(baseline, path) for path in scope},
        resolution_texts={path: repository.read_file_at(tree, path) for path in scope},
        human_removed={
            path: significant_lines(repository.read_file_at(human_parent, path))
            - significant_lines(repository.read_file_at(human_source, path))
            for path in scope
        },
    )


# ------------------------------------------------------------------- workflow


def prepare_resolution_workspace(
    session: Session,
    escalation_id: UUID,
    *,
    human_source_commit: str,
    expected_integration_sha: str,
    settings: Settings | None = None,
) -> ResolutionWorkspace:
    """Build the workspace an operator resolves the merge conflict in.

    Step B of the workflow. Deterministic and side-effect-light: it creates (or
    resets) a detached worktree at the expected baseline, copies the project's
    declared dependencies, and reports the exact paths that may be touched. It
    does **not** attempt the merge, does not guess a resolution, and does not
    touch the integration ref -- the operator's edits are the input this
    exists to collect, and the orchestrator will not manufacture them.

    The historical human commit is only ever *read* here. Nothing in this
    module can rewrite, rebase, re-sign or delete it.
    """
    config = settings or get_settings()
    escalation, task, project = _load_reconciliation_context(session, escalation_id)

    _require_full_sha(human_source_commit, label="human source commit")
    _require_full_sha(expected_integration_sha, label="expected integration SHA")
    _assert_not_already_reconciled(escalation)

    repository = GitService(
        project.repository_path,
        default_branch=project.default_branch,
        settings=config,
    )
    observed = ensure_integration_branch(repository, project)
    if observed != expected_integration_sha:
        raise EntityConflict(
            f"agent/integration is at {observed}, not the expected baseline "
            f"{expected_integration_sha}; a resolution must be built on the "
            "current baseline"
        )
    human_source = _resolve_human_source(repository, human_source_commit)
    if human_source == observed:
        raise EntityConflict(
            f"Human source commit {human_source} is already the integration "
            "baseline; there is nothing to resolve"
        )

    scope = repository.list_changed_paths(
        repository.commit_parents(human_source)[0], human_source
    )
    if not scope:
        raise EntityConflict(
            f"Human source commit {human_source} changed no files, so there is "
            "no work to carry onto the baseline"
        )

    path = resolution_worktree_path(project.id, escalation_id, settings=config)
    _open_resolution_worktree(repository, project, observed, path, config=config, reset=True)
    workspace = ResolutionWorkspace(
        escalation_id=escalation_id,
        task_external_id=task.external_task_id,
        path=path,
        expected_integration_sha=observed,
        human_source_commit=human_source,
        allowed_paths=scope,
        human_source_parent=repository.commit_parents(human_source)[0],
    )
    logger.info(
        "human_resolution_workspace_prepared",
        escalation_id=str(escalation_id),
        task=task.external_task_id,
        path=str(path),
        baseline_sha=observed,
        human_source_commit=human_source,
        allowed_paths=list(scope),
    )
    return workspace


def authorize_human_resolution(
    session: Session,
    escalation_id: UUID,
    *,
    human_source_commit: str,
    expected_integration_sha: str,
    settings: Settings | None = None,
) -> HumanEscalation:
    """Validate the operator's resolution and advance the baseline onto it.

    Steps E through I. The order is the contract:

    * Every question is asked of Git, not of the operator. Ancestry, scope,
      content and the build itself are re-derived here.
    * The resolution commit is created only after the content invariant passes
      and before the ref moves, and it is created with
      :meth:`GitService.commit_tree`, which touches no ref. A failure anywhere
      before :func:`force_branch` therefore leaves ``agent/integration`` exactly
      where it was.
    * The durable provenance is written and flushed **before** the ref moves.
      The residual risk is deliberately the benign direction: the database may
      record a reconciliation whose ref move then fails, which is visible and
      retryable. The dangerous direction -- a baseline that advanced with
      nothing recording why -- is unreachable.
    * The ref move is read back and verified. An integration that did not
      happen is reported as a failure, not as a success.
    """
    config = settings or get_settings()
    escalation, task, project = _load_reconciliation_context(session, escalation_id)

    _require_full_sha(human_source_commit, label="human source commit")
    _require_full_sha(expected_integration_sha, label="expected integration SHA")
    _assert_not_already_reconciled(escalation)

    repository = GitService(
        project.repository_path,
        default_branch=project.default_branch,
        settings=config,
    )

    # --- the baseline is still the one the operator resolved against --------
    observed = ensure_integration_branch(repository, project)
    if observed != expected_integration_sha:
        raise EntityConflict(
            f"agent/integration moved to {observed} since the resolution was "
            f"prepared against {expected_integration_sha}; refusing to advance "
            "onto a resolution built on a superseded baseline"
        )

    human_source = _resolve_human_source(repository, human_source_commit)
    if human_source == observed:
        raise EntityConflict(
            f"Human source commit {human_source} is already the integration "
            "baseline; there is nothing to resolve"
        )

    worktree_path = resolution_worktree_path(project.id, escalation_id, settings=config)
    worktree = _open_resolution_worktree(
        repository, project, observed, worktree_path, config=config, reset=False
    )

    # --- the operator's edits are the only input, and they are bounded -------
    head = worktree.get_head_sha()
    if head != observed:
        raise EntityConflict(
            f"Resolution workspace is at {head}, not the baseline {observed}; the "
            "resolution commit is created by the orchestrator, not by hand"
        )
    conflicted = tuple(entry.path for entry in worktree.get_status() if entry.is_unmerged)
    if conflicted:
        raise EntityConflict(
            f"Resolution workspace still has unresolved conflict markers in: "
            f"{', '.join(sorted(conflicted))}"
        )
    _assert_no_conflict_markers(worktree)

    scope = repository.list_changed_paths(
        repository.commit_parents(human_source)[0], human_source
    )
    if not scope:
        raise EntityConflict(
            f"Human source commit {human_source} changed no files, so there is "
            "no work to carry onto the baseline"
        )
    edited = tuple(sorted(entry.path for entry in worktree.get_status()))
    outside = sorted(set(edited) - set(scope))
    if outside:
        raise EntityConflict(
            f"Resolution workspace changes files outside the human commit's "
            f"scope: {', '.join(outside)}; only {', '.join(scope)} may be resolved"
        )

    # --- assemble the candidate tree, then prove it before it exists --------
    worktree.stage_all()
    tree = worktree.write_tree()
    facts = collect_resolution_facts(
        repository,
        baseline=observed,
        human_source=human_source,
        tree=tree,
    )
    if set(facts.scope) != set(scope):
        raise EntityConflict(
            f"Human commit scope changed while resolving: {sorted(facts.scope)} "
            f"!= {sorted(scope)}"
        )
    verdict = check_resolution_content(
        allowed_paths=facts.scope,
        human_added=facts.human_added,
        baseline_texts=facts.baseline_texts,
        resolution_texts=facts.resolution_texts,
        human_removed=facts.human_removed,
    )
    if not verdict.accepted:
        raise EntityConflict(
            "The resolution does not faithfully carry the human commit "
            f"{human_source} onto baseline {observed}: " + "; ".join(verdict.violations)
        )

    resolution = worktree.commit_tree(
        tree,
        message=(
            f"Resolve human integration for {task.external_task_id}: {task.title}\n\n"
            f"Operator-resolved conflict carrying human commit {human_source}.\n"
            f"Baseline: {observed}\n"
            f"Resolution scope: {', '.join(facts.scope)}\n\n"
            "This commit is the orchestrator's integration_resolution_commit for "
            f"escalation {escalation_id}; the human source commit {human_source} "
            "is preserved unchanged as its second parent."
        ),
        parents=(observed, human_source),
    )
    if resolution == human_source:
        raise EntityConflict(
            "The resolution commit is the human source commit itself; a "
            "resolution must be a distinct commit"
        )
    _assert_resolution_ancestry(
        repository, resolution, baseline=observed, human_source=human_source
    )

    # --- the project's own gate, over the resolved tree ---------------------
    failed, ran = _verify_resolution(
        session, project, task, worktree.path, escalation.task_run_id, settings=config
    )
    if failed:
        raise EntityConflict(
            f"Resolution of human commit {human_source} failed cumulative "
            f"verification: {', '.join(failed)}"
        )

    # --- durable provenance first, then the ref, then the event ------------
    escalations = EscalationRepository(session)
    updated = escalations.reconcile_human_commit(
        escalation_id,
        human_source,
        integration_resolution_commit=resolution,
    )
    session.flush()

    advanced = repository.force_branch(INTEGRATION_BRANCH, resolution)
    if repository.resolve_sha(INTEGRATION_BRANCH) != advanced:
        raise EntityConflict(
            f"agent/integration did not advance to {resolution}; refusing to "
            "record a reconciliation that did not happen"
        )
    TaskRepository(session).record_integration(task.id, unintegrated_commit=None)
    _record_event(
        session,
        project=project,
        task=task,
        task_run_id=escalation.task_run_id,
        previous_sha=observed,
        resolution_sha=advanced,
        human_source_commit=human_source,
        commands_run=ran,
        scope=facts.scope,
    )
    session.flush()

    logger.info(
        "human_conflict_resolution_integrated",
        escalation_id=str(escalation_id),
        task=task.external_task_id,
        human_source_commit=human_source,
        integration_resolution_commit=advanced,
        previous_sha=observed,
        allowed_paths=list(facts.scope),
        commands_run=ran,
    )
    return updated


# ------------------------------------------------------------------- internals


def _load_reconciliation_context(
    session: Session, escalation_id: UUID
) -> tuple[HumanEscalation, Task, Project]:
    """The escalation, task and project a reconciliation operates on.

    Deliberately the same preconditions the existing canonical path enforces,
    so the two routes into a reconciled baseline cannot diverge on who is
    eligible.
    """
    escalations = EscalationRepository(session)
    existing = escalations.get(escalation_id)
    if existing is None:
        raise EntityNotFound("Escalation", escalation_id)
    if existing.status is not EscalationStatus.RESOLVED:
        raise EntityConflict(
            f"Escalation {escalation_id} is {existing.status.value}, not RESOLVED"
        )
    if existing.resolution_intent is not EscalationIntent.COMPLETED_BY_HAND:
        raise EntityConflict(
            f"Escalation {escalation_id} was resolved with "
            f"{existing.resolution_intent}, not COMPLETED_BY_HAND"
        )
    task = TaskRepository(session).get(existing.task_id)
    if task is None:
        raise EntityNotFound("Task", existing.task_id)
    if task.status is not TaskStatus.COMPLETE:
        raise EntityConflict(
            f"Task {task.external_task_id} is {task.status.value}, not COMPLETE"
        )
    project = ProjectRepository(session).get(task.project_id)
    if project is None:
        raise EntityNotFound("Project", task.project_id)
    return existing, task, project


def _assert_not_already_reconciled(escalation: HumanEscalation) -> None:
    """Replay gate.

    Two columns rather than one because they can disagree after a partial
    failure, and either disagreement is a replay. Recording the source without
    the resolution, or the resolution without the source, means some earlier
    attempt got part way and its outcome is unknown; proceeding would decide
    that by overwriting it.
    """
    if escalation.human_commit is not None and escalation.integration_resolution_commit is not None:
        raise EntityConflict(
            f"Escalation {escalation.id} is already reconciled: source "
            f"{escalation.human_commit}, resolution "
            f"{escalation.integration_resolution_commit}"
        )
    if escalation.human_commit is not None:
        raise EntityConflict(
            f"Escalation {escalation.id} already records human commit "
            f"{escalation.human_commit} with no resolution commit; the state is "
            "incomplete and must be inspected before it is changed again"
        )
    if escalation.integration_resolution_commit is not None:
        raise EntityConflict(
            f"Escalation {escalation.id} already records resolution "
            f"{escalation.integration_resolution_commit} with no human source "
            "commit; the state is incomplete and must be inspected before it is "
            "changed again"
        )


def _require_full_sha(value: str, *, label: str) -> None:
    if not FULL_COMMIT_SHA.match(value or ""):
        raise EntityConflict(
            f"{label} must be a full 40-character commit SHA, got {value!r}"
        )


def _resolve_human_source(repository: GitService, human_source_commit: str) -> str:
    """Resolve the human source commit, refusing anything but itself.

    Three refusals, each of which has bitten somebody: a commit that does not
    exist, one that resolves to a *different* object than was named (an
    abbreviated or ref-shaped value that happened to resolve), and one that has
    already been rewritten into the baseline -- in which case there is no
    conflict left to resolve and pretending otherwise would manufacture a
    resolution commit over identical trees.
    """
    try:
        resolved = repository.resolve_sha(human_source_commit)
    except GitError as exc:
        raise EntityNotFound(
            "Commit", f"{human_source_commit} in repository {repository.path}"
        ) from exc
    if resolved != human_source_commit:
        raise EntityConflict(
            f"{human_source_commit} resolves to {resolved}; supply the full commit "
            "SHA so the object that gets recorded is the object that was named"
        )
    return resolved


def _assert_resolution_ancestry(
    repository: GitService, resolution: str, *, baseline: str, human_source: str
) -> None:
    """The two ancestry properties concern 73 actually depends on.

    ``baseline`` being an ancestor means the resolution is built on the
    integration state it claims to extend. ``human_source`` being an ancestor
    is the contract itself: it is what makes the historical human commit part
    of the cumulative baseline, and therefore what makes the dependency guard
    truthful for every task that follows.
    """
    if not repository.contains_commit(baseline, ref=resolution):
        raise EntityConflict(
            f"Resolution {resolution} does not descend from the expected baseline "
            f"{baseline}"
        )
    if not repository.contains_commit(human_source, ref=resolution):
        raise EntityConflict(
            f"Resolution {resolution} does not carry human source commit "
            f"{human_source} in its history; a resolution that is not a merge of "
            "that commit does not put the human work in the baseline"
        )


def _assert_no_conflict_markers(worktree: GitService) -> None:
    """Refuse a resolution that still contains ``<<<<<<<``.

    Git's own unmerged-index check does not cover this: an operator who
    resolves by editing the file and staging it has cleared the index without
    necessarily having deleted the markers. The build would catch it for most
    languages, but "most" is not a contract, and this is a one-line check
    against a string that has no legitimate place in a source file.
    """
    for entry in worktree.list_tracked_files():
        candidate = worktree.path / entry
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "<<<<<<< " in content or "\n>>>>>>> " in content:
            raise EntityConflict(
                f"Resolution workspace still contains conflict markers in {entry}"
            )


def _open_resolution_worktree(
    repository: GitService,
    project: Project,
    baseline: str,
    path: Path,
    *,
    config: Settings,
    reset: bool,
) -> GitService:
    """Open the resolution workspace, creating it if it is not there yet.

    ``reset`` is the difference between the two callers and the reason this is
    not one function used twice. Preparing hands the operator a *clean* tree at
    the baseline, so it discards whatever was there. Authorizing must do the
    opposite: the operator's edits are the input, and resetting here would
    destroy the work being authorized and then cheerfully report that the
    human's implementation was missing.
    """
    if (path / ".git").exists():
        worktree = repository.for_worktree(path)
        if reset:
            worktree.reset_hard_to_sha(baseline)
        worktree.checkout_detached(baseline)
    else:
        if path.exists():
            shutil.rmtree(path)
        repository.prune_worktrees()
        worktree = repository.create_detached_worktree(path, baseline)
    _copy_dependencies(project, path, worktree, settings=config)
    return worktree


def _verify_resolution(
    session: Session,
    project: Project,
    task: Task,
    worktree_path: Path,
    task_run_id: UUID | None,
    *,
    settings: Settings,
) -> tuple[tuple[str, ...], int]:
    """Run the project's own build, lint and tests over the resolved tree.

    Shares ``_verify_cumulative_human`` with the plain human-commit path, and
    differs from it in exactly one respect, which is the point of this
    function: **an empty verification profile is a refusal, not a pass.** The
    canonical path may treat "the project declares no commands" as nothing to
    do, because there the commit is the human's own and Git already proved the
    merge. Here the system is being asked to certify that an operator's
    resolution is faithful, and with no commands there is no evidence at all --
    the check would pass vacuously and the invariant would be unenforceable.
    """
    if project.verification.is_empty:
        raise EntityConflict(
            f"Project {project.external_project_id} declares no verification "
            "commands, so a conflict resolution cannot be validated; refusing to "
            "integrate an unverified resolution"
        )
    return _verify_cumulative_human(
        session,
        project,
        task,
        task_run_id,
        worktree_path=worktree_path,
        settings=settings,
    )


def _record_event(
    session: Session,
    *,
    project: Project,
    task: Task,
    task_run_id: UUID | None,
    previous_sha: str,
    resolution_sha: str,
    human_source_commit: str,
    commands_run: int,
    scope: Sequence[str],
) -> None:
    """The durable, queryable half of what happened.

    Both SHAs are in the payload, and they are named apart:
    ``human_source_commit`` is the historical human-authored commit and
    ``integration_resolution_commit`` is the new commit that carries it. The
    columns on the escalation are the record; this event is what a reader of
    run history sees without having to join anything.
    """
    if task_run_id is None:
        return
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=task_run_id,
            project_id=project.id,
            task_id=task.id,
            event_type=RunEventType.INTEGRATION_ADVANCED,
            attempt=1,
            payload={
                "branch": INTEGRATION_BRANCH,
                "previous_sha": previous_sha,
                "baseline_sha": resolution_sha,
                "integrated_sha": resolution_sha,
                "human_source_commit": human_source_commit,
                "integration_resolution_commit": resolution_sha,
                "resolution": "operator_conflict_resolution",
                "resolved_by": "operator",
                "provenance": "human",
                "resolution_scope": list(scope),
                "commands_run": commands_run,
            },
        )
    )


__all__ = [
    "FULL_COMMIT_SHA",
    "ResolutionVerdict",
    "ResolutionWorkspace",
    "authorize_human_resolution",
    "check_resolution_content",
    "collect_resolution_facts",
    "prepare_resolution_workspace",
    "resolution_worktree_path",
    "significant_lines",
]
