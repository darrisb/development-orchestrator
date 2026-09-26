"""The context package and its budget (build.md section 15).

The Context Builder's output is a *package*: an ordered set of items, a record
of what was left out and why, and a hash over exactly the text the coder will
see. This module owns the package; ``services.context_builder`` owns the
gathering, because deciding what fits must be testable without a repository.

Two properties matter more than cleverness here:

* **Determinism.** The same repository state and the same task produce the
  same rendered text and therefore the same ``content_hash``. Ordering is by
  priority then path, never by set iteration or filesystem order.
* **Honesty.** Nothing is dropped or clipped silently. Every omission is a
  ``DroppedItem`` with a reason and every clip carries a visible marker, so a
  coder that failed for lack of context can be shown why.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum

from .tokens import characters_for_tokens, estimate_tokens

#: Appended where an item was clipped. A coder must never mistake a partial
#: file for a whole one -- that is how a "fix" silently deletes the rest.
TRUNCATION_MARKER = "\n... [truncated by orchestrator: {shown} of {total} lines shown]\n"


class ContextPriority(IntEnum):
    """Inclusion order. Lower wins; ties are broken by path.

    Ranks 1-8 are section 15's priority list verbatim. ``REPOSITORY_MAP`` is
    ranked after them because it is an orientation aid: when the budget is
    tight the list of file names is the right thing to lose first, and losing
    it must never push out a file the task actually named.
    """

    TASK_INSTRUCTIONS = 1
    DECLARED_FILE = 2
    INTERFACE = 3
    RELEVANT_TEST = 4
    CONFIGURATION = 5
    ARCHITECTURE_DECISION = 6
    RECENT_CHANGE = 7
    LESSON = 8
    REPOSITORY_MAP = 9


#: Items that are not repository content and so are never charged a file slot
#: against ``max_files``.
_NON_FILE_PRIORITIES = frozenset(
    {
        ContextPriority.TASK_INSTRUCTIONS,
        ContextPriority.RECENT_CHANGE,
        ContextPriority.LESSON,
        ContextPriority.REPOSITORY_MAP,
    }
)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Configurable bounds (section 15: "implement configurable context budgets").

    Attributes:
        max_tokens: ceiling for the whole package, estimated with
            ``domain.tokens``. Derived from the *served* context window minus
            room for the answer, never from the model's trained maximum.
        max_item_tokens: per-item ceiling; a larger item is clipped, not
            dropped, because the head of a file is usually the useful part.
        max_files: how many repository files may be included at all.
    """

    max_tokens: int = 8000
    max_item_tokens: int = 2000
    max_files: int = 40

    def __post_init__(self) -> None:
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        if self.max_item_tokens < 1:
            raise ValueError("max_item_tokens must be >= 1")
        if self.max_files < 0:
            raise ValueError("max_files must be >= 0")

    def describe(self) -> dict[str, int]:
        return {
            "max_tokens": self.max_tokens,
            "max_item_tokens": self.max_item_tokens,
            "max_files": self.max_files,
        }


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One candidate slice of context.

    Attributes:
        reason: why this was considered, recorded verbatim in the manifest.
            Written for a human reading back a failed run, not for a model.
        sha256: hash of the file's full contents as read, so a manifest can be
            checked against the repository even after the file has changed.
        source_lines: line count before any clipping.
    """

    priority: ContextPriority
    label: str
    content: str
    reason: str
    path: str | None = None
    sha256: str | None = None
    source_bytes: int = 0
    source_lines: int = 0
    truncated: bool = False
    shown_lines: int | None = None
    #: Fence language for file content. The fence is added by ``render``, not
    #: stored in ``content``, so clipping can never eat the closing fence and
    #: leave the rest of the package inside a code block.
    language: str | None = None

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.render())

    @property
    def is_file(self) -> bool:
        return self.path is not None and self.priority not in _NON_FILE_PRIORITIES

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (int(self.priority), self.path or "", self.label)

    def render(self) -> str:
        """The item as it appears in the prompt, heading included."""
        body = self.content.rstrip()
        if self.language is not None:
            body = f"```{self.language}\n{body}\n```"
        return f"## {self.label}\n\n{body}\n"

    def clipped(self, max_tokens: int) -> ContextItem:
        """A copy clipped to ``max_tokens``, marked as truncated.

        Clipping is at a line boundary: half a statement is worse than one
        fewer function, and a coder asked to edit a file needs whole lines.
        """
        limit = characters_for_tokens(max_tokens)
        if len(self.content) <= limit:
            return self
        lines = self.content.splitlines()
        kept: list[str] = []
        used = 0
        for line in lines:
            if used + len(line) + 1 > limit:
                break
            kept.append(line)
            used += len(line) + 1
        marker = TRUNCATION_MARKER.format(shown=len(kept), total=len(lines))
        return ContextItem(
            priority=self.priority,
            label=self.label,
            content="\n".join(kept) + marker,
            reason=self.reason,
            path=self.path,
            sha256=self.sha256,
            source_bytes=self.source_bytes,
            source_lines=self.source_lines or len(lines),
            truncated=True,
            shown_lines=len(kept),
            language=self.language,
        )

    def manifest_entry(self) -> dict[str, object]:
        return {
            "priority": int(self.priority),
            "priority_name": self.priority.name,
            "label": self.label,
            "path": self.path,
            "reason": self.reason,
            "sha256": self.sha256,
            "source_bytes": self.source_bytes,
            "source_lines": self.source_lines,
            "truncated": self.truncated,
            "shown_lines": self.shown_lines,
            "estimated_tokens": self.estimated_tokens,
        }


@dataclass(frozen=True, slots=True)
class DroppedItem:
    """Something the builder wanted to send and could not."""

    label: str
    reason: str
    priority: ContextPriority
    path: str | None = None
    estimated_tokens: int = 0

    def manifest_entry(self) -> dict[str, object]:
        return {
            "priority": int(self.priority),
            "priority_name": self.priority.name,
            "label": self.label,
            "path": self.path,
            "reason": self.reason,
            "estimated_tokens": self.estimated_tokens,
        }


@dataclass(frozen=True, slots=True)
class ContextPackage:
    """What the coding agent will be given, plus the record of how it was cut."""

    items: tuple[ContextItem, ...]
    budget: ContextBudget
    dropped: tuple[DroppedItem, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.render())

    @property
    def file_count(self) -> int:
        return sum(1 for item in self.items if item.is_file)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.items if item.path is not None)

    @property
    def truncated_paths(self) -> tuple[str, ...]:
        return tuple(item.path or item.label for item in self.items if item.truncated)

    @property
    def complete(self) -> bool:
        """True when nothing was dropped and nothing was clipped."""
        return not self.dropped and not any(item.truncated for item in self.items)

    def render(self) -> str:
        """The exact text handed to the coder. This is what ``content_hash`` covers."""
        return "\n".join(item.render() for item in self.items)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.render().encode()).hexdigest()

    def manifest(self) -> dict[str, object]:
        """``context-manifest.json`` (section 15).

        Deliberately free of timestamps and absolute paths: two builds of the
        same task against the same commit must produce byte-identical
        manifests, or the hash proves nothing.
        """
        return {
            "context_hash": self.content_hash,
            "estimated_tokens": self.estimated_tokens,
            "file_count": self.file_count,
            "complete": self.complete,
            "budget": self.budget.describe(),
            "items": [item.manifest_entry() for item in self.items],
            "dropped": [item.manifest_entry() for item in self.dropped],
            **dict(self.metadata),
        }


def assemble(
    candidates: Iterable[ContextItem],
    budget: ContextBudget,
    *,
    metadata: Mapping[str, object] | None = None,
) -> ContextPackage:
    """Fill ``budget`` with the highest-priority candidates that fit.

    Strictly greedy in priority order, with no backfilling of a smaller
    lower-priority item once something has been dropped. Backfilling would
    make the package depend on file sizes in a way that is hard to predict and
    harder to explain, and section 15's ranking exists precisely so that the
    answer to "why is this here and not that?" is "it ranks higher".
    """
    ordered = sorted(_deduplicate(candidates), key=lambda item: item.sort_key)
    accepted: list[ContextItem] = []
    dropped: list[DroppedItem] = []
    used_tokens = 0
    used_files = 0
    exhausted = False

    for candidate in ordered:
        item = candidate.clipped(budget.max_item_tokens)
        if item.is_file and used_files >= budget.max_files:
            dropped.append(_drop(item, f"max_files={budget.max_files} reached"))
            continue
        cost = item.estimated_tokens
        if exhausted or used_tokens + cost > budget.max_tokens:
            # The first rank-1 item is the task itself; a budget that cannot
            # hold it is a misconfiguration, not a selection outcome.
            if item.priority is ContextPriority.TASK_INSTRUCTIONS and not accepted:
                raise ContextBudgetTooSmall(cost, budget.max_tokens)
            exhausted = True
            dropped.append(_drop(item, f"max_tokens={budget.max_tokens} reached"))
            continue
        accepted.append(item)
        used_tokens += cost
        if item.is_file:
            used_files += 1

    return ContextPackage(
        items=tuple(accepted),
        budget=budget,
        dropped=tuple(dropped),
        metadata=dict(metadata or {}),
    )


class ContextBudgetTooSmall(ValueError):
    """The budget cannot hold even the task instructions."""

    def __init__(self, required_tokens: int, max_tokens: int) -> None:
        super().__init__(
            f"Context budget of {max_tokens} tokens cannot hold the task instructions "
            f"({required_tokens} tokens); raise CONTEXT_MAX_TOKENS or the served "
            "context window"
        )
        self.required_tokens = required_tokens
        self.max_tokens = max_tokens


def _deduplicate(candidates: Iterable[ContextItem]) -> list[ContextItem]:
    """Keep the highest-priority copy of each path.

    A file can be both task-declared and a likely interface; sending it twice
    would spend the budget twice and invite the coder to edit the copy.
    """
    best: dict[str, ContextItem] = {}
    passthrough: list[ContextItem] = []
    for candidate in candidates:
        if candidate.path is None:
            passthrough.append(candidate)
            continue
        current = best.get(candidate.path)
        if current is None or candidate.priority < current.priority:
            best[candidate.path] = candidate
    return [*passthrough, *best.values()]


def _drop(item: ContextItem, reason: str) -> DroppedItem:
    return DroppedItem(
        label=item.label,
        reason=reason,
        priority=item.priority,
        path=item.path,
        estimated_tokens=item.estimated_tokens,
    )


def render_bullet_list(heading: str, entries: Sequence[str]) -> str:
    """Small shared helper for the builder's synthesised (non-file) items."""
    if not entries:
        return f"{heading}: none"
    lines = "\n".join(f"- {entry}" for entry in entries)
    return f"{heading}:\n{lines}"
