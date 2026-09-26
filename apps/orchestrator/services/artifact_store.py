"""Run artifact store (build.md section 9).

Every run gets a stable identifier and a directory beneath ``ARTIFACT_ROOT``.
Large objects -- prompts, responses, diffs, command logs, the context manifest
-- are files; the database holds a path, a size and a hash, never the bytes.

The store is deliberately dumb: it writes, hashes and records. What each file
means is the caller's business, which is why ``kind`` is a plain string rather
than an enum the whole system has to agree on before a new artifact can exist.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..db.models import TaskRunRow
from ..domain.models import Artifact, TaskRun
from ..repositories import ArtifactRepository, TaskRunRepository
from .errors import EntityNotFound

logger = get_logger(__name__)

#: ``RUN-20260919-000042`` (section 9): sortable, human-quotable, and unique
#: without needing a UUID in every log line and directory name.
RUN_ID_PREFIX = "RUN"
_RUN_ID_RE = re.compile(rf"^{RUN_ID_PREFIX}-(\d{{8}})-(\d{{6}})$")
_SEQUENCE_WIDTH = 6

#: Refuse a path that would escape the run directory. Artifact names come from
#: our own code today, but this is the boundary a later phase's "write the
#: worker's log file" call will be crossing on a model's behalf. ``.`` and
#: ``..`` are excluded as whole segments: ``a/../../b.txt`` matches a naive
#: character class and lands two directories above the run.
_SAFE_SEGMENT_RE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]+$")


class ArtifactPathRejected(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"Artifact name {name!r} must be a relative path of plain name segments"
        )
        self.name = name


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """A file on disk and the record that points at it."""

    kind: str
    #: Relative to ``ARTIFACT_ROOT``, which is what the database stores: an
    #: absolute path would break the moment the root moves or the orchestrator
    #: runs in a container with a different mount point.
    relative_path: str
    absolute_path: Path
    sha256: str
    size_bytes: int

    def read_text(self) -> str:
        return self.absolute_path.read_text(encoding="utf-8")


def _is_safe_name(name: str) -> bool:
    segments = name.split("/")
    return bool(segments) and all(_SAFE_SEGMENT_RE.match(segment) for segment in segments)


def allocate_external_run_id(session: Session, *, when: datetime | None = None) -> str:
    """The next ``RUN-YYYYMMDD-NNNNNN`` for today.

    Derived from the ids already issued today rather than from a row count, so
    deleting an old run cannot make the store reissue an identifier that a log
    line or a directory name still refers to.
    """
    stamp = (when or datetime.now(UTC)).strftime("%Y%m%d")
    prefix = f"{RUN_ID_PREFIX}-{stamp}-"
    issued = session.scalars(
        select(TaskRunRow.external_run_id).where(
            TaskRunRow.external_run_id.is_not(None),
            TaskRunRow.external_run_id.startswith(prefix),
        )
    ).all()
    highest = 0
    for candidate in issued:
        match = _RUN_ID_RE.match(candidate or "")
        if match and match.group(1) == stamp:
            highest = max(highest, int(match.group(2)))
    return f"{prefix}{highest + 1:0{_SEQUENCE_WIDTH}d}"


def ensure_run_id(session: Session, task_run_id: UUID) -> str:
    """The run's external id, allocating and persisting one on first use.

    Raises:
        EntityNotFound: no such run.
    """
    runs = TaskRunRepository(session)
    run = runs.get(task_run_id)
    if run is None:
        raise EntityNotFound("Run", task_run_id)
    if run.external_run_id:
        return run.external_run_id
    external_run_id = allocate_external_run_id(session)
    runs.update_fields(run.id, external_run_id=external_run_id)
    logger.info("run_id_allocated", run_id=str(run.id), external_run_id=external_run_id)
    return external_run_id


def attempt_prefix(run: TaskRun, *, cycle: int | None = None) -> str:
    """Directory prefix keeping one run's attempts from overwriting each other.

    The first attempt of a run writes section 9's names unprefixed, so the
    common case has the layout the specification describes; a retry or a
    review cycle writes beneath ``attempt-N-cycle-M/``. Shared by everything
    that writes a run artifact, so the coder's prompt, the verification log
    and the review from one turn of the fix loop land together.

    ``M`` is the cycle this work *belongs to* -- the review cycle whose verdict
    will judge it -- which is one ahead of ``run.review_cycle`` until that
    verdict has actually been returned (``review_cycle`` counts cycles
    completed, and is deliberately not incremented before the reviewer
    answers). Inferring the label from the row instead filed a cycle's
    artifacts under the previous cycle's name (concern 33), so the default
    here is the cycle in progress and a caller that knows better passes it.

    Collision-free without the caller having to think about it: every coding
    attempt increments ``attempt_number``, so two turns of the loop cannot
    share a directory even when a verification failure means no review cycle
    was spent between them.
    """
    label = run.review_cycle + 1 if cycle is None else cycle
    if run.attempt_number <= 1 and label <= 1:
        return ""
    return f"attempt-{run.attempt_number}-cycle-{label}/"


def run_directory(external_run_id: str, *, settings: Settings | None = None) -> Path:
    """Absolute directory for a run's artifacts, created if absent."""
    config = settings or get_settings()
    if not _RUN_ID_RE.match(external_run_id) and not _is_safe_name(external_run_id):
        raise ArtifactPathRejected(external_run_id)
    directory = config.runs_dir / external_run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_text(
    session: Session,
    task_run_id: UUID,
    name: str,
    text: str,
    *,
    kind: str | None = None,
    settings: Settings | None = None,
) -> StoredArtifact:
    """Write ``text`` into the run's directory and record it.

    Raises:
        EntityNotFound: no such run.
        ArtifactPathRejected: ``name`` is not a plain relative path.
    """
    return _write(
        session,
        task_run_id,
        name,
        text.encode("utf-8"),
        kind=kind or name,
        settings=settings,
    )


def write_json(
    session: Session,
    task_run_id: UUID,
    name: str,
    payload: Any,
    *,
    kind: str | None = None,
    settings: Settings | None = None,
) -> StoredArtifact:
    """Write ``payload`` as stable, diffable JSON.

    Keys are sorted and the file ends in a newline: two runs that built the
    same context produce byte-identical files, so a hash comparison means
    something.
    """
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return write_text(session, task_run_id, name, text, kind=kind, settings=settings)


def _write(
    session: Session,
    task_run_id: UUID,
    name: str,
    data: bytes,
    *,
    kind: str,
    settings: Settings | None,
) -> StoredArtifact:
    config = settings or get_settings()
    if not _is_safe_name(name):
        raise ArtifactPathRejected(name)

    external_run_id = ensure_run_id(session, task_run_id)
    directory = run_directory(external_run_id, settings=config)
    target = directory / name
    # Belt and braces: the name check above should make this unreachable, and
    # a run directory is not the place to find out that it did not.
    if directory not in target.resolve().parents:
        raise ArtifactPathRejected(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    digest = hashlib.sha256(data).hexdigest()
    relative = target.relative_to(config.artifact_root).as_posix()
    ArtifactRepository(session).record(
        Artifact(
            task_run_id=task_run_id,
            kind=kind,
            path=relative,
            sha256=digest,
            size_bytes=len(data),
        )
    )
    _ensure_run_artifact_path(session, task_run_id, directory, config)
    logger.info(
        "artifact_written",
        run_id=str(task_run_id),
        external_run_id=external_run_id,
        kind=kind,
        path=relative,
        size_bytes=len(data),
        sha256=digest,
    )
    return StoredArtifact(
        kind=kind,
        relative_path=relative,
        absolute_path=target,
        sha256=digest,
        size_bytes=len(data),
    )


def _ensure_run_artifact_path(
    session: Session, task_run_id: UUID, directory: Path, settings: Settings
) -> None:
    runs = TaskRunRepository(session)
    run: TaskRun | None = runs.get(task_run_id)
    relative = directory.relative_to(settings.artifact_root).as_posix()
    if run is not None and run.artifact_path != relative:
        runs.update_fields(task_run_id, artifact_path=relative)
