"""The code-edit contract (build.md section 14, phase G item 4).

The coder does not run commands, does not use a shell and does not call Git.
It returns a structured set of file edits, and the orchestrator decides which
of them are permitted and writes those. That is section 52 applied to editing:
deciding what may be written is the workflow's job, and a model that could
execute its own edits would own that decision by default.

**Why whole-file content rather than a patch.** A unified diff from a local
model fails often and fails silently-ish: a wrong hunk header or a
miscounted line applies cleanly in the wrong place or not at all, and the
model is then debugging a patch format instead of the task. Whole-file content
either parses or does not, costs output tokens in proportion to the files the
task declared, and makes the resulting Git diff -- which is what the verifier
and the reviewer read -- a consequence of the file state rather than of the
model's arithmetic. The cost is real: a small change to a large file rewrites
the whole file, which is why ``MAX_EDIT_BYTES`` exists and why a task that
declares its files gets a much cheaper attempt than one that does not.

Pure: parsing and validation only. Writing files is ``services.code_edits``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .relevance import normalise_path
from .scope import is_within_repository
from .tokens import characters_for_tokens

#: Bumped when the edit contract changes (section 34 attribution).
EDIT_SCHEMA_VERSION = "code-edits/1"

#: Ceiling on one file's new content when nothing derives one. A model that
#: returns a megabyte for one file has either pasted the wrong thing or is
#: generating, not editing. Callers that know the context budget should derive
#: the ceiling from it with ``max_edit_bytes_for_context`` instead.
MAX_EDIT_BYTES = 8_000

#: How much larger a rewritten file may be than the version the context budget
#: was able to show (concern 11). Some headroom is necessary -- adding a guard
#: clause makes a file longer -- but the two numbers describe the same file at
#: two moments, so the ceiling on the way out is derived from the ceiling on the
#: way in rather than chosen independently. Two independent numbers can disagree
#: by orders of magnitude, and the failure that follows is a file clipped on the
#: way in and rewritten in full on the way out: see concern 1.
EDIT_SIZE_HEADROOM = 1.25


def max_edit_bytes_for_context(max_item_tokens: int) -> int:
    """The output ceiling implied by an input ceiling of ``max_item_tokens``.

    Shares ``domain.tokens``' one ratio, so the estimate that decided a file was
    too large to show is the estimate that decides how large its replacement may
    be.
    """
    return max(1, int(characters_for_tokens(max_item_tokens) * EDIT_SIZE_HEADROOM))


class EditOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"

    @property
    def needs_content(self) -> bool:
        return self is not EditOperation.DELETE


#: The JSON schema sent to the endpoint. ``content`` is required at the object
#: level even for a delete, where it is the empty string: local endpoints with
#: constrained decoding handle a uniformly shaped object far more reliably
#: than a conditionally shaped one.
EDIT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "edits"],
    "properties": {
        "summary": {"type": "string"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "operation", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "operation": {"type": "string", "enum": [op.value for op in EditOperation]},
                    "content": {"type": "string"},
                },
            },
        },
        "requirementsMet": {"type": "array", "items": {"type": "string"}},
        "testsAdded": {"type": "array", "items": {"type": "string"}},
        "followUps": {"type": "array", "items": {"type": "string"}},
        "deviationsFromPlan": {"type": "array", "items": {"type": "string"}},
    },
}


class MalformedChangeSet(ValueError):
    """The response was valid JSON of the right shape but not usable as edits."""


@dataclass(frozen=True, slots=True)
class FileEdit:
    """One file the coder wants written, created or removed."""

    path: str
    operation: EditOperation
    content: str | None = None

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode()) if self.content else 0

    def describe(self) -> dict[str, object]:
        """Metadata only. The content itself is on disk after application, and
        repeating it in an event payload or a log line would put a file's
        worth of source into the database."""
        return {
            "path": self.path,
            "operation": self.operation.value,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class CodeChangeSet:
    """A coder's complete answer: the edits, plus what it claims about them.

    The claims are recorded, never trusted. They become the *claimed* half of
    the completion report (``domain.completion``); the measured half comes from
    the diff the orchestrator takes afterwards.
    """

    summary: str
    edits: tuple[FileEdit, ...] = ()
    requirements_met: tuple[str, ...] = ()
    tests_added: tuple[str, ...] = ()
    follow_ups: tuple[str, ...] = ()
    deviations_from_plan: tuple[str, ...] = ()
    #: Problems found while parsing that did not invalidate the whole answer:
    #: a dropped edit with an unusable path, a delete carrying content.
    warnings: tuple[str, ...] = field(default=())

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(edit.path for edit in self.edits)

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object], *, max_edit_bytes: int = MAX_EDIT_BYTES
    ) -> CodeChangeSet:
        """Read a change set out of a parsed model response.

        Raises:
            MalformedChangeSet: ``edits`` is missing, is not a list, or every
                entry in it was unusable. A response that proposes no usable
                edit at all is not a formatting slip to work around; it is an
                attempt that did nothing, and the caller must see that.
        """
        raw = payload.get("edits")
        if not isinstance(raw, Sequence) or isinstance(raw, str):
            raise MalformedChangeSet("The response's 'edits' field is not a list")

        edits: list[FileEdit] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for index, entry in enumerate(raw):
            edit, warning = _parse_edit(entry, index, max_edit_bytes=max_edit_bytes)
            if warning:
                warnings.append(warning)
            if edit is None:
                continue
            if edit.path in seen:
                # Two edits to one path: the later one is the model's second
                # thought and there is no way to merge them safely.
                warnings.append(
                    f"edit {index}: {edit.path} is edited more than once; "
                    f"only the first edit was kept"
                )
                continue
            seen.add(edit.path)
            edits.append(edit)

        if not edits:
            detail = "; ".join(warnings) if warnings else "the list was empty"
            raise MalformedChangeSet(f"The response contains no usable edits ({detail})")

        summary = payload.get("summary")
        return cls(
            summary=summary.strip() if isinstance(summary, str) else "",
            edits=tuple(edits),
            requirements_met=_strings(payload.get("requirementsMet")),
            tests_added=_strings(payload.get("testsAdded")),
            follow_ups=_strings(payload.get("followUps")),
            deviations_from_plan=_strings(payload.get("deviationsFromPlan")),
            warnings=tuple(warnings),
        )

    def describe(self) -> dict[str, object]:
        return {
            "schema_version": EDIT_SCHEMA_VERSION,
            "summary": self.summary,
            "edits": [edit.describe() for edit in self.edits],
            "requirements_met": list(self.requirements_met),
            "tests_added": list(self.tests_added),
            "follow_ups": list(self.follow_ups),
            "deviations_from_plan": list(self.deviations_from_plan),
            "warnings": list(self.warnings),
        }


def _parse_edit(
    entry: object, index: int, *, max_edit_bytes: int
) -> tuple[FileEdit | None, str | None]:
    """One entry of ``edits``, or ``None`` with the reason it was dropped."""
    if not isinstance(entry, Mapping):
        return None, f"edit {index}: not an object"

    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, f"edit {index}: no path"
    if not is_within_repository(raw_path):
        return None, (
            f"edit {index}: path {raw_path!r} is not repository-relative "
            f"(absolute paths, '~' and '..' are refused)"
        )
    path = normalise_path(raw_path)

    raw_operation = entry.get("operation")
    if not isinstance(raw_operation, str):
        return None, f"edit {index} ({path}): no operation"
    try:
        operation = EditOperation(raw_operation.strip().casefold())
    except ValueError:
        allowed = ", ".join(op.value for op in EditOperation)
        return None, (
            f"edit {index} ({path}): operation {raw_operation!r} is not one of: {allowed}"
        )

    raw_content = entry.get("content")
    content = raw_content if isinstance(raw_content, str) else None
    if operation.needs_content:
        if content is None:
            return None, f"edit {index} ({path}): a {operation.value} needs content"
        if len(content.encode()) > max_edit_bytes:
            return None, (
                f"edit {index} ({path}): content is {len(content.encode())} bytes, "
                f"over the {max_edit_bytes}-byte limit for one file"
            )
        return FileEdit(path=path, operation=operation, content=content), None

    warning = (
        f"edit {index} ({path}): content sent with a delete was ignored"
        if content
        else None
    )
    return FileEdit(path=path, operation=operation, content=None), warning


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(entry.strip() for entry in value if isinstance(entry, str) and entry.strip())


__all__ = [
    "EDIT_SCHEMA",
    "EDIT_SCHEMA_VERSION",
    "EDIT_SIZE_HEADROOM",
    "MAX_EDIT_BYTES",
    "CodeChangeSet",
    "EditOperation",
    "FileEdit",
    "MalformedChangeSet",
    "max_edit_bytes_for_context",
]
