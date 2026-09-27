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
    #: This file may be *replaced* by the coder, and the edit contract asks for
    #: a file's complete new contents. Showing part of it and then asking for
    #: all of it is how a fix silently deletes the rest, so an item marked here
    #: is exempt from ``max_item_tokens`` and is charged against the total
    #: budget in full. It is set from the task's own write allowance, before
    #: budgeting, never inferred from size or content (concerns 55 and 56).
    requires_complete: bool = False

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
            requires_complete=self.requires_complete,
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
            "requires_complete": self.requires_complete,
        }


@dataclass(frozen=True, slots=True)
class RequiredSource:
    """Whether one file the task may replace reached the coder whole.

    The coding agent's guard reads this rather than inferring safety from
    ``truncated_paths``. The two are not the same question: a file can fail to
    arrive whole by being clipped, by being dropped for the budget, by being
    larger than ``CONTEXT_MAX_FILE_BYTES``, by being binary, or by never having
    been a selection candidate at all. Only the first of those leaves a
    truncated item behind; the rest leave nothing, and nothing is exactly what
    an inference from truncation cannot see (concern 58).

    Attributes:
        complete: the file's full original contents are in the package.
        reason: why they are not, for a human and for the refusal message.
    """

    path: str
    complete: bool
    reason: str | None = None

    def manifest_entry(self) -> dict[str, object]:
        return {"path": self.path, "complete": self.complete, "reason": self.reason}


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
    #: One entry per *existing* file the task may replace, complete or not.
    #: A file the task will create has no entry: there is nothing to supply.
    required_sources: tuple[RequiredSource, ...] = ()

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
    def incomplete_required(self) -> tuple[RequiredSource, ...]:
        """Files the task may replace that did not arrive whole.

        Non-empty means a whole-file replacement cannot safely be asked for.
        """
        return tuple(source for source in self.required_sources if not source.complete)

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
            "required_sources": [source.manifest_entry() for source in self.required_sources],
            **dict(self.metadata),
        }


def assemble(
    candidates: Iterable[ContextItem],
    budget: ContextBudget,
    *,
    metadata: Mapping[str, object] | None = None,
    required_paths: Mapping[str, str | None] | None = None,
) -> ContextPackage:
    """Fill ``budget`` with the highest-priority candidates that fit.

    Two passes, because two kinds of item are not interchangeable.

    A file the task may *replace* has to be shown whole: the edit contract asks
    for a file's complete new contents, so a coder shown the first 242 lines of
    276 and asked to rewrite it will delete the 34 it never saw. Those items
    carry ``requires_complete`` and are settled first -- exempt from
    ``max_item_tokens``, charged in full against ``max_tokens``. Everything
    else is supporting material and competes for what is left, which is what
    makes requirement 4 hold: supporting context is sacrificed before writable
    source is truncated, rather than the other way round.

    ``required_paths`` names every *existing* file the task may replace, mapped
    to the reason it could not be read where there is one. It is what makes the
    completeness record a fact about the package rather than an inference from
    it: a file that never became a candidate leaves no item and no drop, so
    only the caller can say that it should have been there (concern 58).

    The second pass is strictly greedy in priority order, with no backfilling
    of a smaller lower-priority item once something has been dropped.
    Backfilling would make the package depend on file sizes in a way that is
    hard to predict and harder to explain, and section 15's ranking exists
    precisely so that the answer to "why is this here and not that?" is "it
    ranks higher".

    When the required items cannot fit -- alongside the task specification,
    which is never displaced -- the reservation is abandoned wholesale rather
    than honoured in part. The package is then built exactly as it was before
    this pass existed: the writable file is clipped, marked ``truncated``, and
    the coding agent's own guard refuses the attempt before a model is asked.
    Half a reservation would be the one outcome worse than either, because it
    looks like the file was supplied whole.
    """
    ordered = sorted(_deduplicate(candidates), key=lambda item: item.sort_key)

    # The task specification is charged at its clipped cost because that is
    # what the greedy pass below will spend on it; reserving around anything
    # larger would leave the budget unable to hold the item it is reserving for.
    task_cost = sum(
        item.clipped(budget.max_item_tokens).estimated_tokens
        for item in ordered
        if item.priority is ContextPriority.TASK_INSTRUCTIONS
    )
    required = [item for item in ordered if item.requires_complete]
    reserved = sum(item.estimated_tokens for item in required)
    honoured = bool(required) and reserved + task_cost <= budget.max_tokens
    if not honoured:
        required, reserved = [], 0

    required_ids = {id(item) for item in required}
    accepted: list[ContextItem] = list(required)
    dropped: list[DroppedItem] = []
    used_tokens = 0
    # Required files are never dropped for ``max_files``, but they do occupy
    # slots: the cap is a statement about how many files a coder can hold at
    # once, and a file it must rewrite counts most of all.
    used_files = sum(1 for item in required if item.is_file)
    exhausted = False
    ceiling = budget.max_tokens - reserved

    for candidate in ordered:
        if id(candidate) in required_ids:
            continue
        item = candidate.clipped(budget.max_item_tokens)
        if item.is_file and used_files >= budget.max_files:
            dropped.append(_drop(item, f"max_files={budget.max_files} reached"))
            continue
        cost = item.estimated_tokens
        if exhausted or used_tokens + cost > ceiling:
            # The first rank-1 item is the task itself; a budget that cannot
            # hold it is a misconfiguration, not a selection outcome.
            if item.priority is ContextPriority.TASK_INSTRUCTIONS and not accepted:
                raise ContextBudgetTooSmall(cost, budget.max_tokens)
            exhausted = True
            dropped.append(
                _drop(item, f"max_tokens={budget.max_tokens} reached")
                if not reserved
                else _drop(
                    item,
                    f"max_tokens={budget.max_tokens} reached, of which {reserved} "
                    "are reserved for files the task may replace",
                )
            )
            continue
        accepted.append(item)
        used_tokens += cost
        if item.is_file:
            used_files += 1

    items = tuple(sorted(accepted, key=lambda item: item.sort_key))
    return ContextPackage(
        items=items,
        budget=budget,
        dropped=tuple(dropped),
        metadata=_with_reservation_record(metadata, ordered, reserved, honoured=honoured),
        required_sources=_required_sources(items, dropped, required_paths),
    )


def _required_sources(
    items: Sequence[ContextItem],
    dropped: Sequence[DroppedItem],
    required_paths: Mapping[str, str | None] | None,
) -> tuple[RequiredSource, ...]:
    """One verdict per file the task may replace, from what is in the package.

    Deliberately decided by looking at the assembled package rather than by
    listing the ways a file can go missing. New exclusion paths get added to a
    context builder over time; "is its complete text in here?" keeps answering
    correctly when they do.
    """
    if not required_paths:
        return ()
    included = {item.path: item for item in items if item.path is not None}
    discarded = {item.path: item for item in dropped if item.path is not None}
    sources: list[RequiredSource] = []
    for path in sorted(required_paths):
        item = included.get(path)
        if item is not None and not item.truncated:
            sources.append(RequiredSource(path=path, complete=True))
            continue
        if item is not None:
            sources.append(
                RequiredSource(
                    path=path,
                    complete=False,
                    reason=(
                        f"shown {item.shown_lines} of {item.source_lines} lines: "
                        "the context budget could not hold it whole"
                    ),
                )
            )
            continue
        dropped_item = discarded.get(path)
        if dropped_item is not None:
            sources.append(
                RequiredSource(path=path, complete=False, reason=dropped_item.reason)
            )
            continue
        sources.append(
            RequiredSource(
                path=path,
                complete=False,
                reason=required_paths[path] or "not included in the context package",
            )
        )
    return tuple(sources)


def _with_reservation_record(
    metadata: Mapping[str, object] | None,
    ordered: Sequence[ContextItem],
    reserved: int,
    *,
    honoured: bool,
) -> dict[str, object]:
    """Record what was held back and why, for ``context-manifest.json``.

    A reservation that could not be honoured is the interesting case and the
    one an operator has to be able to see without reading the run's prompt, so
    it is stated in the manifest *and* pushed onto the package's warnings.
    """
    required = [item for item in ordered if item.requires_complete]
    record: dict[str, object] = {
        "paths": sorted(item.path or item.label for item in required),
        "reserved_tokens": reserved,
        "honoured": honoured,
    }
    result = dict(metadata or {})
    if required and not honoured:
        record["reason"] = (
            "the files the task may replace do not fit in the context budget "
            "alongside the task specification; they are clipped as before and "
            "the attempt will be refused rather than sent incomplete"
        )
        warnings = list(result.get("warnings") or ())
        warnings.append(
            "context budget cannot hold the complete contents of "
            + ", ".join(sorted(item.path or item.label for item in required))
            + "; raise CONTEXT_MAX_TOKENS or the served context window, or "
            "split the task"
        )
        result["warnings"] = warnings
    result["required_complete"] = record
    return result


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
