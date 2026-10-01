"""Dependency path copy, bootstrap and publication.

Declared dependency paths are intentionally outside Git's tracked tree: they
exist so networkless workers can receive things like package directories without
letting generated output become source. Bootstrap is the narrow exception. It
may create those ignored paths in a Docker worker with an explicit network, and
only those ignored paths may later be published back to the managed repository.

Three decisions shape everything here.

**Validity is keyed on a dependency-input fingerprint, not the integration
commit.** The commit is the wrong key in both directions: it goes stale on every
advance that cannot possibly have changed what a package manager would install,
and -- on the human integration path, which advances the ref without
republishing -- it stays fresh across an advance that *did* change the
manifests. The fingerprint covers what bootstrap actually reads: the tracked
manifests and lockfiles, the declared paths, the bootstrap commands, and the
worker identity that runs them. The commit id is kept as provenance only.

**Markers live in Git's administrative directory, not inside the tree they
describe.** A marker beside or within a dependency path is a file the
orchestrator itself created in the working tree, and it then has to be excluded
from hashes, hidden from status checks and kept out of publication. Under
``.git`` none of that is necessary: Git never reports it as a working-tree
change, it cannot be mistaken for source, it is not payload, and a per-worktree
marker dies with its worktree -- which is exactly the right lifetime.

**There is no content hashing.** Publication is a staged copy followed by one
``os.replace``, so a published tree is either entirely the old one or entirely
the new one; there is no torn state for a content hash to detect. The marker is
written only *after* the tree it describes is in place, so a crash leaves a
missing marker and a rebuild, never a false claim. Dropping the hash is what
lets a dependency path be a single file, lets declared paths nest, and keeps a
``node_modules`` out of the orchestrator's memory.
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from ..config.settings import Settings, WorkerBackend
from ..domain.models import Project, TaskRun
from ..repositories import TaskRunRepository
from .git_service import GitService
from .worker_service import worker_image_for, worker_session

BOOTSTRAP_NETWORK = "bridge"
NETWORKLESS_VERIFICATION_NETWORK = "none"

#: Marker store, relative to a Git administrative directory. Under the
#: per-worktree ``--absolute-git-dir`` it describes that worktree's dependency
#: set; under the shared ``--git-common-dir`` it describes the published set in
#: the managed repository.
MARKER_STORE_DIRNAME = "orchestrator-dependencies"
MARKER_VERSION = 3

#: Tracked files whose contents decide what a dependency bootstrap installs.
#: Matched on the basename of every tracked path, so a manifest in a
#: subdirectory of a monorepo counts too. Deliberately a fixed list rather than
#: project configuration: the fingerprint has to mean the same thing for every
#: reader of a published tree, and a per-project list would make "valid" a
#: matter of who is asking.
DEPENDENCY_MANIFEST_PATTERNS: tuple[str, ...] = (
    ".nvmrc",
    ".python-version",
    ".tool-versions",
    "Cargo.lock",
    "Cargo.toml",
    "Gemfile",
    "Gemfile.lock",
    "Pipfile",
    "Pipfile.lock",
    "composer.json",
    "composer.lock",
    "constraints*.txt",
    "go.mod",
    "go.sum",
    "npm-shrinkwrap.json",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "poetry.lock",
    "pyproject.toml",
    "requirements*.txt",
    "setup.cfg",
    "setup.py",
    "uv.lock",
    "yarn.lock",
)


class DependencyBootstrapError(RuntimeError):
    """Dependency preparation, bootstrap or publication failed.

    Every caller that owns an outcome catches this *and* the ``ValueError`` a
    malformed project configuration raises: on the task path they become a
    failed verification step, on the integration path a blocked integration.
    Neither ever reaches the supervisor as an unhandled exception.
    """


#: What the boundaries catch. ``ValueError`` is a project-configuration fault (a
#: dependency path that is unsafe, escapes the tree, or is no longer
#: Git-ignored); ``OSError`` is converted to ``DependencyBootstrapError`` inside
#: this module, and is listed as a backstop for anything not yet routed.
DEPENDENCY_FAILURES: tuple[type[Exception], ...] = (
    DependencyBootstrapError,
    ValueError,
    OSError,
)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    executions: tuple[object, ...] = ()
    skipped: bool = False

    @property
    def succeeded(self) -> bool:
        """Whether every command that ran succeeded.

        A skipped result has nothing that failed, so it is ``True``; callers
        distinguish "nothing needed doing" from "work succeeded" with
        ``skipped``.
        """
        return all(execution.succeeded for execution in self.executions)

    @property
    def failed_commands(self) -> tuple[str, ...]:
        return tuple(
            execution.result.command.display
            for execution in self.executions
            if not execution.succeeded
        )


def has_bootstrap(project: Project) -> bool:
    return bool(project.dependency_bootstrap_commands)


def dependency_fingerprint(
    project: Project, worktree_git: GitService, *, settings: Settings
) -> str:
    """Hash everything a bootstrap run would read, as seen in this worktree.

    Manifests are read from the working tree rather than from a commit on
    purpose: the working tree is what bootstrap will actually be handed, so a
    candidate that edits ``requirements.txt`` without committing it still gets a
    rebuilt dependency tree instead of a tree installed from the old manifest.
    """
    with _operational("reading dependency manifests"):
        manifests = {
            path: hashlib.sha256(_manifest_bytes(worktree_git, path)).hexdigest()
            for path in _manifest_paths(worktree_git)
        }
    payload = {
        "version": MARKER_VERSION,
        "dependency_paths": [relative.as_posix() for _, relative in _declared(project)],
        "bootstrap_commands": list(project.dependency_bootstrap_commands),
        "worker_profile": project.worker_profile.value,
        "worker_image": worker_image_for(project.worker_profile, settings),
        "manifests": dict(sorted(manifests.items())),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def prepopulate_dependencies(
    project: Project,
    worktree: Path,
    worktree_git: GitService,
    *,
    settings: Settings,
) -> None:
    """Give a worktree a usable dependency set, or leave it for bootstrap.

    Without bootstrap this preserves the original strict behaviour: every
    declared path must exist in the managed repository and must be Git-ignored,
    and a missing one is a configuration error.

    With bootstrap configured the declared paths are treated as **one set**,
    which is what makes nesting and partial staleness tractable. The set in the
    worktree is either wholly certified for the current inputs -- in which case
    nothing happens, however much the project's own commands have since written
    inside it -- or it is discarded and rebuilt from the published set, and only
    if *that* is wholly certified too. Anything else is left to bootstrap.
    """
    repository = Path(project.repository_path).resolve()
    worktree_root = worktree.resolve()
    declared = _declared(project)
    if not declared:
        return

    if not has_bootstrap(project):
        _prepopulate_strictly(
            declared,
            worktree_git,
            repository=repository,
            worktree_root=worktree_root,
            project=project,
            settings=settings,
        )
        return

    for label, relative in declared:
        _assert_ignored(worktree_git, relative, label)
    fingerprint = dependency_fingerprint(project, worktree_git, settings=settings)
    worktree_store = _marker_store(worktree_git)
    published_store = _published_marker_store(worktree_git)

    with _dependency_lock(project, settings=settings), _operational(
        "preparing the dependency set"
    ):
        if _set_is_certified(declared, worktree_root, worktree_store, fingerprint):
            return
        _discard_set(declared, worktree_root, worktree_store)
        if not _set_is_certified(declared, repository, published_store, fingerprint):
            return
        provenance = _marker_payload(published_store, declared[0][1].as_posix())
        integration_sha = str((provenance or {}).get("integration_sha", ""))
        for label, relative in declared:
            target = _contained(worktree_root, relative, label=label)
            if target.exists() or target.is_symlink():
                # A declared path nested inside another arrived with its parent.
                continue
            _copy_path(_contained(repository, relative, label=label), target)
        for _, relative in declared:
            _write_marker(
                worktree_store,
                declared=relative.as_posix(),
                fingerprint=fingerprint,
                integration_sha=integration_sha,
            )


def bootstrap_dependencies(
    session: Session,
    project: Project,
    run: TaskRun,
    *,
    worktree_path: Path,
    worktree_git: GitService,
    integration_sha: str,
    settings: Settings,
    prefix: str,
    secrets: Mapping[str, str] | None = None,
) -> BootstrapResult:
    """Run dependency bootstrap *if needed*, then prove it changed only ignored paths.

    "If needed" is the point of the fingerprint: a worktree whose declared set
    is already certified for the current inputs starts no container at all.

    The markers are written **last**, after the commands succeeded, after every
    declared path was confirmed to exist, and after the visible-source check
    passed. A bootstrap that mutated tracked source therefore leaves nothing
    behind that a retry would mistake for a certified tree -- the rejection
    repeats until the cause is fixed.
    """
    commands = tuple(project.dependency_bootstrap_commands)
    declared = _declared(project)
    if not commands:
        return BootstrapResult(skipped=True)

    root = worktree_path.resolve()
    for label, relative in declared:
        _assert_ignored(worktree_git, relative, label)

    fingerprint = dependency_fingerprint(project, worktree_git, settings=settings)
    store = _marker_store(worktree_git)
    if declared and _set_is_certified(declared, root, store, fingerprint):
        return BootstrapResult(skipped=True)

    if settings.worker_backend is not WorkerBackend.DOCKER:
        raise DependencyBootstrapError(
            "Dependency bootstrap requires the Docker worker backend; "
            f"{settings.worker_backend.value} cannot run networked bootstrap safely"
        )

    # Discard the whole set before installing over it. Leaving a tree built from
    # different inputs in place would let a package manager treat it as a warm
    # cache and "satisfy" requirements it never read.
    with _operational("discarding the stale dependency set"):
        _discard_set(declared, root, store)

    before = _visible_snapshot(worktree_git)
    with worker_session(
        worktree_path,
        profile=project.worker_profile,
        settings=settings,
        secrets=secrets,
        worker_network=BOOTSTRAP_NETWORK,
    ) as worker:
        TaskRunRepository(session).update_fields(
            run.id, worker_image=worker.spec.runtime_description
        )
        from .command_execution import execute_commands

        executions = execute_commands(
            session,
            worker,
            run.id,
            commands,
            category="dependency-bootstrap",
            prefix=prefix,
            settings=settings,
            stop_on_failure=True,
        )

    result = BootstrapResult(executions=executions)
    if not result.succeeded:
        return result

    for label, relative in declared:
        if not _contained(root, relative, label=label).exists():
            raise DependencyBootstrapError(
                f"Dependency bootstrap completed but did not create {label!r}"
            )

    after = _visible_snapshot(worktree_git)
    if after != before:
        changed = ", ".join(_status_paths(after, before))
        raise DependencyBootstrapError(
            "Dependency bootstrap changed tracked or unignored source paths: "
            f"{changed or 'unknown'}"
        )

    with _operational("recording the dependency set"):
        for _, relative in declared:
            _write_marker(
                store,
                declared=relative.as_posix(),
                fingerprint=fingerprint,
                integration_sha=integration_sha,
            )
    return result


def publish_dependencies(
    project: Project,
    *,
    source_worktree: Path,
    source_worktree_git: GitService,
    integration_sha: str,
    settings: Settings,
) -> None:
    """Publish a verified worktree's dependency set to the managed repository.

    Ordinary verification runs the project's real commands, and real commands
    write inside dependency paths -- bytecode caches, tool state, downloaded
    wheels. None of that is a reason to distrust the tree, and none of it is
    examined here: what is published is certified by the fingerprint of the
    inputs it was installed from, which that churn cannot change.

    The published markers are cleared *before* the trees are touched and written
    *after* they all land, so an interrupted publication leaves an uncertified
    set that the next task rebuilds, never a marker that outlives the tree it
    described.
    """
    declared = _declared(project)
    if not has_bootstrap(project) or not declared:
        return
    repository = Path(project.repository_path).resolve()
    source_root = source_worktree.resolve()
    worktree_store = _marker_store(source_worktree_git)
    published_store = _published_marker_store(source_worktree_git)

    # The fingerprint is *read* from the set's own certification rather than
    # recomputed here, and that is the whole of requirement 3. Recomputing would
    # measure the working tree after verification has run in it, and a tool that
    # rewrites a tracked manifest mid-run would decertify a tree that is
    # perfectly good -- while the commit actually being published does not
    # contain that scribble at all. What the tree was installed for was settled
    # before verification started; nothing verification does can change it.
    fingerprint = _certified_fingerprint(declared, source_root, worktree_store)
    if fingerprint is None:
        raise DependencyBootstrapError(
            "Refusing to publish a dependency set that is not certified: the "
            "declared paths are missing, unmarked, or disagree about which "
            "dependency inputs they were installed for"
        )

    with _dependency_lock(project, settings=settings), _operational(
        "publishing the dependency set"
    ):
        for _, relative in declared:
            _clear_marker(published_store, relative.as_posix())
        for label, relative in declared:
            source = _contained(source_root, relative, label=label)
            target = _contained(repository, relative, label=label)
            _publish_one(source, target)
        for _, relative in declared:
            _write_marker(
                published_store,
                declared=relative.as_posix(),
                fingerprint=fingerprint,
                integration_sha=integration_sha,
            )


# ------------------------------------------------------------------ internals


def _declared(project: Project) -> tuple[tuple[str, Path], ...]:
    """Declared paths as ``(label, relative)``, outermost first.

    The order is what makes nesting work: a parent is copied, published and
    discarded before any declared path inside it, so the child's turn finds the
    work already done rather than undoing it.
    """
    pairs = [(label, _safe_relative_path(label)) for label in project.dependency_paths]
    return tuple(sorted(pairs, key=lambda pair: pair[1].parts))


def _prepopulate_strictly(
    declared: Sequence[tuple[str, Path]],
    worktree_git: GitService,
    *,
    repository: Path,
    worktree_root: Path,
    project: Project,
    settings: Settings,
) -> None:
    """The no-bootstrap contract: the repository must already hold everything."""
    with _dependency_lock(project, settings=settings):
        for label, relative in declared:
            _assert_ignored(worktree_git, relative, label)
            source = _contained(repository, relative, label=label)
            if not source.exists():
                raise ValueError(f"Declared dependency path does not exist: {label!r}")
            target = _contained(worktree_root, relative, label=label)
            if target.exists() or target.is_symlink():
                continue
            with _operational(f"copying dependency path {label!r}"):
                _copy_path(source, target)


def _certified_fingerprint(
    declared: Sequence[tuple[str, Path]], root: Path, store: Path
) -> str | None:
    """The one fingerprint every declared path is present and marked for.

    ``None`` when any path is missing or unmarked, or when the markers disagree
    -- a set that was certified piecemeal is not a certified set.
    """
    fingerprints: set[str] = set()
    for label, relative in declared:
        if not _contained(root, relative, label=label).exists():
            return None
        payload = _marker_payload(store, relative.as_posix())
        if payload is None:
            return None
        fingerprints.add(str(payload.get("fingerprint", "")))
    if len(fingerprints) != 1:
        return None
    return fingerprints.pop()


def _set_is_certified(
    declared: Sequence[tuple[str, Path]], root: Path, store: Path, fingerprint: str
) -> bool:
    """Is the whole declared set present and marked for exactly these inputs?

    All or nothing on purpose. The bootstrap commands are a project-level set,
    so rebuilding one path runs the commands that build all of them; treating
    the set as divisible would buy nothing and would make a declared path nested
    inside another ambiguous.
    """
    return bool(declared) and _certified_fingerprint(declared, root, store) == fingerprint


def _discard_set(
    declared: Sequence[tuple[str, Path]], root: Path, store: Path
) -> None:
    """Remove the trees and revoke their markers, outermost path first."""
    for _, relative in declared:
        _clear_marker(store, relative.as_posix())
    for label, relative in declared:
        target = _contained(root, relative, label=label)
        if target.exists() or target.is_symlink():
            _remove_any(target)


def _manifest_paths(git: GitService) -> tuple[str, ...]:
    return tuple(
        sorted(
            path
            for path in git.list_tracked_files()
            if any(
                fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern)
                for pattern in DEPENDENCY_MANIFEST_PATTERNS
            )
        )
    )


def _manifest_bytes(git: GitService, path: str) -> bytes:
    """The manifest as bootstrap would read it, or a stand-in if it is gone.

    A tracked path missing from the working tree is a real, fingerprintable
    state (a candidate that deletes a lockfile), so it gets its own value rather
    than being treated as empty -- which an unreadable file would otherwise be
    indistinguishable from.

    Only *absence* is fingerprintable. Every other filesystem failure -- a
    permission fault, an I/O error, a path component that is not a directory --
    is an operational failure and is allowed to escape to the surrounding
    ``_operational`` boundary, which turns it into a deterministic
    ``DependencyBootstrapError`` rather than a fingerprint that silently claims
    the manifest was deleted.
    """
    try:
        return (git.path / path).read_bytes()
    except FileNotFoundError:
        return b"\0absent\0"


def _safe_relative_path(declared: str) -> Path:
    relative = Path(declared)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"Unsafe dependency path {declared!r}")
    return relative


def _contained(root: Path, relative: Path, *, label: str) -> Path:
    """Resolve ``root/relative`` and prove it is still inside ``root``.

    Applied to every path this module reads, writes, copies or deletes,
    including the copy *target* in a worktree: an intermediate component that is
    a symlink would otherwise redirect the write out of the tree.
    """
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"Dependency path escapes its root: {label!r}")
    return target


def _assert_ignored(git: GitService, relative: Path, declared: str) -> None:
    normalized = relative.as_posix()
    candidates = (normalized, f"{normalized}/")
    if not any(git.is_ignored(form) for form in candidates):
        raise ValueError(
            f"Dependency path {declared!r} must be ignored by Git before it "
            "can be used as a dependency path"
        )


def _copy_path(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def _publish_one(source: Path, target: Path) -> None:
    """Replace ``target`` with a copy of ``source``, atomically.

    The copy is staged beside the target and swapped in with one ``os.replace``,
    so a reader under the publication lock sees either the whole old tree or the
    whole new one.

    The backup of the previous tree is deleted only once it is certainly
    redundant -- either it was put back, or the target is in place without it.
    If restoration itself fails, the backup is the last good copy of a published
    tree and is **kept**, and the error names it so an operator can recover it.
    ``BaseException`` rather than ``Exception`` because a signal arriving
    between the two swaps must not take the backup with it.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.orchestrator-staging-{uuid.uuid4().hex}")
    backup = target.with_name(f".{target.name}.orchestrator-replaced-{uuid.uuid4().hex}")
    displaced = False
    try:
        _copy_path(source, staging)
        if target.exists() or target.is_symlink():
            os.replace(target, backup)
            displaced = True
        os.replace(staging, target)
    except BaseException as error:
        _discard_quietly(staging)
        if not displaced:
            raise
        if target.exists() or target.is_symlink():
            # The target never went away, or something put it back; the backup
            # is redundant.
            _discard_quietly(backup)
            raise
        try:
            os.replace(backup, target)
        except OSError as restore_failure:
            raise DependencyBootstrapError(
                f"Failed to publish {target} and could not restore the previous "
                f"tree; it has been kept at {backup} for recovery "
                f"({restore_failure})"
            ) from error
        raise
    _discard_quietly(staging)
    if displaced:
        _discard_quietly(backup)


def _discard_quietly(path: Path) -> None:
    """Remove a staging or backup path, never masking the error in flight."""
    with contextlib.suppress(OSError):
        if path.exists() or path.is_symlink():
            _remove_any(path)


def _remove_any(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _marker_store(git: GitService) -> Path:
    """Where this worktree's own dependency markers live."""
    return git.git_dir() / MARKER_STORE_DIRNAME


def _published_marker_store(git: GitService) -> Path:
    """Where the markers for the repository's published set live.

    The shared administrative directory, so that every worktree of the project
    reads the same answer about what has been published.
    """
    return git.git_common_dir() / MARKER_STORE_DIRNAME


def _marker_file(store: Path, declared: str) -> Path:
    """One file per declared path, named by a digest of it.

    A digest rather than the path itself because a declared path contains
    separators and a nested one would otherwise collide with its parent's name.
    The readable path is recorded inside the file and is checked on read.
    """
    return store / f"{hashlib.sha256(declared.encode()).hexdigest()[:16]}.json"


def _write_marker(
    store: Path, *, declared: str, fingerprint: str, integration_sha: str
) -> None:
    """Record that the tree at ``declared`` was installed for ``fingerprint``.

    Written through a temporary file and one ``os.replace`` so a reader never
    sees a half-written marker, and a crash leaves either the previous marker or
    none at all.
    """
    store.mkdir(parents=True, exist_ok=True)
    marker = _marker_file(store, declared)
    payload = {
        "version": MARKER_VERSION,
        "dependency_path": declared,
        "fingerprint": fingerprint,
        # Provenance, not a validity key. Answers "which baseline produced this
        # tree" for an operator; never compared.
        "integration_sha": integration_sha,
    }
    staging = marker.with_name(f"{marker.name}.tmp-{uuid.uuid4().hex}")
    staging.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(staging, marker)


def _clear_marker(store: Path, declared: str) -> None:
    _marker_file(store, declared).unlink(missing_ok=True)


def _marker_payload(store: Path, declared: str) -> dict[str, object] | None:
    try:
        payload = json.loads(_marker_file(store, declared).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != MARKER_VERSION:
        return None
    if payload.get("dependency_path") != declared:
        return None
    return payload


def _visible_snapshot(
    git: GitService,
) -> tuple[tuple[tuple[str, str, str, str | None], ...], str]:
    """What Git can see of the working tree -- read-only.

    ``include_untracked=False`` is load-bearing: the default would stage the
    whole worktree intent-to-add to make untracked files visible to ``git
    diff``, and a function that exists to *audit* a bootstrap must not mutate
    the index to do it. Nothing is lost, because ``get_status`` already asks
    with ``--untracked-files=all`` and so lists untracked paths individually
    rather than collapsing them into a directory entry.
    """
    status = tuple(
        sorted(
            (
                entry.path,
                entry.index_status,
                entry.worktree_status,
                entry.original_path,
            )
            for entry in git.get_status()
        )
    )
    return status, git.get_diff("HEAD", include_untracked=False)


def _status_paths(
    after: tuple[tuple[tuple[str, str, str, str | None], ...], str],
    before: tuple[tuple[tuple[str, str, str, str | None], ...], str],
) -> tuple[str, ...]:
    after_status, _ = after
    before_status, _ = before
    entries = {entry[0] for entry in after_status}.symmetric_difference(
        {entry[0] for entry in before_status}
    )
    if entries:
        return tuple(sorted(entries))
    changed = tuple(sorted(entry[0] for entry in after_status if entry not in before_status))
    return changed or ("visible diff changed",)


@contextlib.contextmanager
def _operational(action: str) -> Iterator[None]:
    """Turn a filesystem failure into this module's own error type.

    So that a full disk or a permission fault during bootstrap or publication
    becomes a deterministic blocked integration or failed verification step,
    rather than an ``OSError`` escaping into a call path documented as never
    raising for an outcome.
    """
    try:
        yield
    except DependencyBootstrapError:
        raise
    except OSError as error:
        raise DependencyBootstrapError(f"{action} failed: {error}") from error


def _lock_directory(repository: Path, *, settings: Settings) -> Path:
    """Where publication locks for ``repository`` live.

    Shared by every orchestrator operating on the repository, whatever its own
    ``worktree_root``. ``.git`` is a directory for a normal clone and a file for
    a linked worktree; only the directory form is usable here, and anything else
    keeps the previous worktree-local location so that locking never itself
    becomes the failure.
    """
    git_dir = repository / ".git"
    if git_dir.is_dir():
        return git_dir / "orchestrator-locks"
    return settings.worktree_root / "locks"


@contextlib.contextmanager
def _dependency_lock(project: Project, *, settings: Settings) -> Iterator[None]:
    """Serialise publication and copying for one project.

    Linux-only, deliberately and currently: ``fcntl.flock`` is imported at
    module scope, so this module -- and everything that imports it -- requires a
    platform that has it. The orchestrator ships as a Linux container, and no
    Windows support is claimed anywhere; if that changes, this is the thing that
    has to change with it.

    The lock is keyed on the repository being published to, not on the worktree
    root, so two orchestrators with different worktree roots over one repository
    still exclude each other. For that to hold, the lock file itself has to live
    somewhere *shared* by those orchestrators, so it goes in the repository's own
    Git administrative area -- the one directory every orchestrator working on
    this repository necessarily agrees on -- falling back to the worktree root
    only when that area is not an available directory.
    """
    repository = Path(project.repository_path).resolve()
    digest = hashlib.sha256(str(repository).encode()).hexdigest()[:16]
    directory = _lock_directory(repository, settings=settings)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"dependency-publication-{digest}.lock"
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
