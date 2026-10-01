"""Dependency path copy, bootstrap and publication.

Declared dependency paths are intentionally outside Git's tracked tree: they
exist so networkless workers can receive things like package directories without
letting generated output become source. Bootstrap is the narrow exception. It
may create those ignored paths in a Docker worker with an explicit network, and
only those ignored paths may later be published back to the managed repository.

Validity of a dependency tree is keyed on a **dependency-input fingerprint**,
not on the integration commit. The commit is the wrong key in both directions:
it goes stale on every advance that cannot possibly have changed what a package
manager would install, and -- on the human integration path, which advances the
ref without republishing -- it can stay fresh across an advance that *did*
change the manifests. The fingerprint covers the things bootstrap actually
reads: the tracked manifests and lockfiles, the declared paths themselves, the
bootstrap commands, and the worker identity that runs them. The commit id is
kept in the marker as provenance only.
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
from collections.abc import Iterator, Mapping
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
MARKER_FILENAME = ".orchestrator-dependency-bootstrap.json"
MARKER_VERSION = 2

#: Tracked files whose contents decide what a dependency bootstrap installs.
#: Matched on the basename of every tracked path, so a manifest in a
#: subdirectory of a monorepo counts too. Deliberately a fixed list rather than
#: project configuration: the fingerprint has to mean the same thing for every
#: reader of a published tree, including one running an older revision of this
#: file, and a per-project list would make "valid" a matter of who is asking.
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
    """Bootstrap, finalisation or publication failed deterministically.

    Always caught by the caller that owns the outcome -- a verification step on
    the task path, a blocked integration on the integration path -- so it never
    reaches the supervisor as an unhandled exception.
    """


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    executions: tuple[object, ...] = ()
    skipped: bool = False

    @property
    def succeeded(self) -> bool:
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
    manifests = {
        path: hashlib.sha256(_manifest_bytes(worktree_git, path)).hexdigest()
        for path in _manifest_paths(worktree_git)
    }
    payload = {
        "version": MARKER_VERSION,
        "dependency_paths": sorted(
            _safe_relative_path(declared).as_posix() for declared in project.dependency_paths
        ),
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
    """Copy usable published dependencies into a task or integration worktree.

    Without bootstrap this preserves the original strict behaviour: every
    declared path must exist in the managed repository and must be ignored by
    Git. With bootstrap configured, a published tree is only copied when it is
    *usable* -- its fingerprint matches the inputs in this worktree and its
    recorded content hash still describes it, which is what rules out a tree
    left half-written by an interrupted publication. Anything else is skipped so
    the later bootstrap phase can rebuild it.
    """
    repository = Path(project.repository_path).resolve()
    bootstrap = has_bootstrap(project)
    fingerprint = (
        dependency_fingerprint(project, worktree_git, settings=settings) if bootstrap else ""
    )
    with _dependency_lock(project, settings=settings):
        for declared in project.dependency_paths:
            relative = _safe_relative_path(declared)
            _assert_ignored(worktree_git, relative, declared)
            target = worktree / relative
            if bootstrap and target.exists() and not _fingerprint_matches(
                target, declared=relative.as_posix(), fingerprint=fingerprint
            ):
                _remove_any(target)
            source = _contained(repository, relative, label=declared)
            if not source.exists():
                if bootstrap:
                    continue
                raise ValueError(f"Declared dependency path does not exist: {declared!r}")
            if bootstrap and not _published_is_usable(
                source, declared=relative.as_posix(), fingerprint=fingerprint
            ):
                continue
            if target.exists():
                continue
            _copy_path(source, target)


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

    "If needed" is the whole point of the fingerprint. A worktree that already
    holds every declared path with a marker for the current dependency inputs
    needs nothing: no networked container is started at all. Bootstrap runs only
    for trees that are missing, unmarked, or marked for different inputs.
    """
    commands = tuple(project.dependency_bootstrap_commands)
    if not commands:
        return BootstrapResult(skipped=True)

    root = worktree_path.resolve()
    for declared in project.dependency_paths:
        _assert_ignored(worktree_git, _safe_relative_path(declared), declared)

    fingerprint = dependency_fingerprint(project, worktree_git, settings=settings)
    stale = _stale_dependency_paths(project, root, fingerprint=fingerprint)
    if not stale:
        return BootstrapResult(skipped=True)

    if settings.worker_backend is not WorkerBackend.DOCKER:
        raise DependencyBootstrapError(
            "Dependency bootstrap requires the Docker worker backend; "
            f"{settings.worker_backend.value} cannot run networked bootstrap safely"
        )

    # Drop the unusable trees before installing over them. Leaving a tree built
    # from different inputs in place would let a package manager treat it as a
    # warm cache and "satisfy" requirements it never read.
    for relative in stale:
        _remove_any(_contained(root, relative, label=relative.as_posix()))

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

    for declared in project.dependency_paths:
        relative = _safe_relative_path(declared)
        target = _contained(root, relative, label=declared)
        if not target.exists():
            raise DependencyBootstrapError(
                f"Dependency bootstrap completed but did not create {declared!r}"
            )
        if relative in stale:
            _write_marker(
                target,
                declared=relative.as_posix(),
                fingerprint=fingerprint,
                integration_sha=integration_sha,
            )

    after = _visible_snapshot(worktree_git)
    if after != before:
        changed = ", ".join(_status_paths(after, before))
        raise DependencyBootstrapError(
            "Dependency bootstrap changed tracked or unignored source paths: "
            f"{changed or 'unknown'}"
        )
    return result


def finalize_dependency_markers(
    project: Project,
    *,
    worktree: Path,
    worktree_git: GitService,
    integration_sha: str,
    settings: Settings,
) -> None:
    """Re-stamp the markers so they describe the tree about to be published.

    Called once, after cumulative verification has passed and before
    publication. Verification runs the project's real commands, and real
    commands write inside dependency paths -- bytecode caches, tool state,
    downloaded wheels. Those writes are invisible to Git by construction, so
    they are not a reason to distrust the tree; but a content hash taken before
    them would no longer describe it, and a publication gated on that hash would
    reject a perfectly good tree. Stamping here is what makes the published
    content hash a true statement about the published bytes.

    This refreshes only marker files inside the declared, Git-ignored paths. It
    cannot launder a tracked change into accepted source: the declared paths are
    re-checked as ignored, publication copies nothing else, and a bootstrap that
    touched visible source has already been rejected before this point.
    """
    if not has_bootstrap(project) or not project.dependency_paths:
        return
    root = worktree.resolve()
    fingerprint = dependency_fingerprint(project, worktree_git, settings=settings)
    for declared in project.dependency_paths:
        relative = _safe_relative_path(declared)
        _assert_ignored(worktree_git, relative, declared)
        target = _contained(root, relative, label=declared)
        if not target.exists():
            raise DependencyBootstrapError(
                f"Cannot finalize missing dependency path {declared!r}"
            )
        _write_marker(
            target,
            declared=relative.as_posix(),
            fingerprint=fingerprint,
            integration_sha=integration_sha,
        )


def publish_dependencies(
    project: Project,
    *,
    source_worktree: Path,
    source_worktree_git: GitService,
    settings: Settings,
) -> None:
    """Publish declared dependencies from a verified integration worktree."""
    if not has_bootstrap(project) or not project.dependency_paths:
        return
    repository = Path(project.repository_path).resolve()
    source_root = source_worktree.resolve()
    fingerprint = dependency_fingerprint(project, source_worktree_git, settings=settings)
    with _dependency_lock(project, settings=settings):
        for declared in project.dependency_paths:
            relative = _safe_relative_path(declared)
            source = _contained(source_root, relative, label=declared)
            if not source.exists():
                raise DependencyBootstrapError(
                    f"Cannot publish missing dependency path {declared!r}"
                )
            if not _fingerprint_matches(
                source, declared=relative.as_posix(), fingerprint=fingerprint
            ):
                raise DependencyBootstrapError(
                    f"Cannot publish dependency path {declared!r}: its marker does "
                    "not describe the current dependency inputs"
                )
            target = _contained(repository, relative, label=declared)
            _publish_one(source, target)


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
    than being silently treated as empty -- which is what an unreadable file
    would otherwise be indistinguishable from.
    """
    candidate = git.path / path
    try:
        return candidate.read_bytes()
    except OSError:
        return b"\0absent\0"


def _stale_dependency_paths(
    project: Project, root: Path, *, fingerprint: str
) -> tuple[Path, ...]:
    stale: list[Path] = []
    for declared in project.dependency_paths:
        relative = _safe_relative_path(declared)
        target = _contained(root, relative, label=declared)
        if not target.exists() or not _fingerprint_matches(
            target, declared=relative.as_posix(), fingerprint=fingerprint
        ):
            stale.append(relative)
    return tuple(stale)


def _safe_relative_path(declared: str) -> Path:
    relative = Path(declared)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"Unsafe dependency path {declared!r}")
    return relative


def _contained(root: Path, relative: Path, *, label: str) -> Path:
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Dependency path escapes repository: {label!r}")
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
    if source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def _publish_one(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.orchestrator-staging-{uuid.uuid4().hex}")
    backup = target.with_name(f".{target.name}.orchestrator-replaced-{uuid.uuid4().hex}")
    try:
        _copy_path(source, staging)
        if target.exists() or target.is_symlink():
            os.replace(target, backup)
        os.replace(staging, target)
    except OSError:
        if not target.exists() and backup.exists():
            os.replace(backup, target)
        raise
    finally:
        if staging.exists() or staging.is_symlink():
            _remove_any(staging)
        if backup.exists() or backup.is_symlink():
            _remove_any(backup)


def _remove_any(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _marker_path(path: Path) -> Path:
    if path.is_dir():
        return path / MARKER_FILENAME
    return path.with_name(f"{path.name}.{MARKER_FILENAME}")


def _write_marker(
    path: Path,
    *,
    declared: str,
    fingerprint: str,
    integration_sha: str,
) -> None:
    marker = _marker_path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MARKER_VERSION,
        "dependency_path": declared,
        "fingerprint": fingerprint,
        # Provenance, not a validity key. Useful when an operator is asking
        # "which baseline produced this tree"; never compared.
        "integration_sha": integration_sha,
        # Hashed before the marker is written, and the marker is excluded from
        # the walk, so writing it cannot invalidate the value it carries.
        "content_sha256": _content_sha(path),
    }
    marker.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _read_marker(path: Path, *, declared: str) -> dict[str, object] | None:
    try:
        payload = json.loads(_marker_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != MARKER_VERSION or payload.get("dependency_path") != declared:
        return None
    return payload


def _fingerprint_matches(path: Path, *, declared: str, fingerprint: str) -> bool:
    """Is this tree the one the current dependency inputs call for?

    The only question asked of a tree living in a worktree. Its contents are
    expected to drift -- the project's own commands write inside it -- and that
    drift is not a reason to reinstall.
    """
    payload = _read_marker(path, declared=declared)
    return payload is not None and payload.get("fingerprint") == fingerprint


def _published_is_usable(path: Path, *, declared: str, fingerprint: str) -> bool:
    """Is this *published* tree both current and intact?

    Content is checked here and nowhere else. A tree in the managed repository
    has no legitimate writer except publication, so a content hash that no
    longer describes it means the publication did not finish, and copying it
    into a worker would pass that damage on.
    """
    payload = _read_marker(path, declared=declared)
    return (
        payload is not None
        and payload.get("fingerprint") == fingerprint
        and payload.get("content_sha256") == _content_sha(path)
    )


def _content_sha(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file() or path.is_symlink():
        digest.update(b"file\0")
        digest.update(path.read_bytes())
        return digest.hexdigest()
    marker = _marker_path(path)
    for entry in sorted(path.rglob("*")):
        if entry == marker:
            continue
        relative = entry.relative_to(path).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        if entry.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(entry).encode())
        elif entry.is_file():
            digest.update(b"file\0")
            digest.update(entry.read_bytes())
        elif entry.is_dir():
            digest.update(b"dir\0")
    return digest.hexdigest()


def _visible_snapshot(
    git: GitService,
) -> tuple[tuple[tuple[str, str, str, str | None], ...], str]:
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
    return status, git.get_diff("HEAD")


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
def _dependency_lock(project: Project, *, settings: Settings) -> Iterator[None]:
    directory = settings.worktree_root / str(project.id)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "dependency-publication.lock"
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
