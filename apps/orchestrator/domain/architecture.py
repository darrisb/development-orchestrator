"""Project memory and architecture decisions (build.md section 16).

Explicit project memory, not conversational memory: a repository keeps its own
``.ai/`` directory, the Context Builder retrieves the decisions that apply to
the current task, and the reviewer later checks the diff for drift against the
same records.

Parsing here is tolerant, unlike manifest parsing. A manifest is a contract
between the orchestrator and the repository, so a typo must stop an import; an
ADR is a human note, and refusing to run a task because someone wrote
``Status :`` would be a worse failure than working from a record whose status
is unknown. What could not be understood is reported in ``warnings`` rather
than raised.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath

from .relevance import extract_keywords

#: Where a managed repository keeps its project memory.
MEMORY_DIRECTORY = ".ai"
DECISIONS_DIRECTORY = f"{MEMORY_DIRECTORY}/decisions"
PROJECT_OVERVIEW = f"{MEMORY_DIRECTORY}/project.md"
ARCHITECTURE_OVERVIEW = f"{MEMORY_DIRECTORY}/architecture.md"
POLICIES_DIRECTORY = f"{MEMORY_DIRECTORY}/policies"


class DecisionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    DEPRECATED = "DEPRECATED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_binding(self) -> bool:
        """Whether a coder must still obey this decision.

        An unknown status counts as binding: a record someone wrote down and
        nobody retired is more likely to be live than dead, and the cost of
        being wrong is a few hundred tokens, not a wrong architecture.
        """
        return self in _BINDING_STATUSES


_BINDING_STATUSES = frozenset(
    {DecisionStatus.ACCEPTED, DecisionStatus.PROPOSED, DecisionStatus.UNKNOWN}
)

_SECTION_NAMES = ("context", "decision", "consequences")
_HEADING_RE = re.compile(r"(?m)^(#{1,6})\s*(.+?)\s*$")
_FIELD_RE = re.compile(
    r"(?mi)^\s*(?:[-*]\s*)?(?:\*\*)?(id|title|status|date)(?:\*\*)?\s*[:=]\s*(.+?)\s*$"
)
_TITLE_HEADING_RE = re.compile(r"(?i)^\s*(?:ADR[-\s]?(\d+)\s*[:.\-]?\s*)?(.*)$")


@dataclass(frozen=True, slots=True)
class ArchitectureDecision:
    """One ADR (section 16).

    ``body`` is the record as written; the parsed sections exist for scoring
    and for rendering a short form when the full text will not fit.
    """

    identifier: str
    title: str
    body: str
    path: str
    status: DecisionStatus = DecisionStatus.UNKNOWN
    date: str | None = None
    context: str = ""
    decision: str = ""
    consequences: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_binding(self) -> bool:
        return self.status.is_binding

    @property
    def keywords(self) -> frozenset[str]:
        return extract_keywords(
            self.identifier, self.title, self.context, self.decision, PurePosixPath(self.path).stem
        )

    def summary(self) -> str:
        """The decision without its surrounding prose, for a tight budget."""
        parts = [f"{self.identifier}: {self.title}", f"Status: {self.status}"]
        if self.decision:
            parts.append(f"Decision: {_collapse(self.decision)}")
        if self.consequences:
            parts.append(f"Consequences: {_collapse(self.consequences)}")
        return "\n".join(parts)

    def relevance(self, keywords: Iterable[str]) -> int:
        wanted = {keyword.casefold() for keyword in keywords}
        return len(self.keywords & wanted) if wanted else 0


def parse_decision(text: str, *, path: str) -> ArchitectureDecision:
    """Read an ADR. Never raises: an unreadable record is reported, not fatal."""
    warnings: list[str] = []
    fields = {
        match.group(1).casefold(): match.group(2).strip()
        for match in _FIELD_RE.finditer(text)
    }
    sections = _split_sections(text)
    heading = _first_heading(text)

    identifier, title = _identify(fields, heading, path)
    if not title:
        title = identifier
        warnings.append("no title found; using the record's identifier")

    status_text = fields.get("status", "")
    status = _parse_status(status_text)
    if status is DecisionStatus.UNKNOWN:
        warnings.append(
            f"unrecognised status {status_text!r}; treated as binding" if status_text
            else "no status recorded; treated as binding"
        )

    for name in _SECTION_NAMES:
        if not sections.get(name):
            warnings.append(f"no '{name}' section")

    return ArchitectureDecision(
        identifier=identifier,
        title=title,
        body=text.strip(),
        path=path,
        status=status,
        date=fields.get("date"),
        context=sections.get("context", ""),
        decision=sections.get("decision", ""),
        consequences=sections.get("consequences", ""),
        warnings=tuple(warnings),
    )


def select_decisions(
    decisions: Iterable[ArchitectureDecision],
    keywords: Iterable[str],
    *,
    limit: int = 5,
    include_unmatched: bool = False,
) -> list[ArchitectureDecision]:
    """Binding decisions relevant to ``keywords``, most relevant first.

    Ties break on identifier so the selection is stable across runs. With
    ``include_unmatched`` the remaining binding decisions fill any spare
    slots -- useful for a small ``.ai/`` directory where every record is
    likely to apply.
    """
    wanted = frozenset(keyword.casefold() for keyword in keywords)
    binding = [decision for decision in decisions if decision.is_binding]
    scored = sorted(
        ((decision.relevance(wanted), decision) for decision in binding),
        key=lambda entry: (-entry[0], entry[1].identifier),
    )
    selected = [decision for score, decision in scored if score > 0][:limit]
    if include_unmatched and len(selected) < limit:
        chosen = {decision.identifier for decision in selected}
        selected.extend(
            decision
            for score, decision in scored
            if score == 0 and decision.identifier not in chosen
        )
    return selected[:limit]


def _identify(
    fields: dict[str, str], heading: str | None, path: str
) -> tuple[str, str]:
    stem = PurePosixPath(path).stem
    identifier = fields.get("id") or ""
    title = fields.get("title") or ""

    if heading:
        match = _TITLE_HEADING_RE.match(heading)
        if match:
            number, remainder = match.group(1), match.group(2).strip()
            if not identifier and number:
                identifier = f"ADR-{number}"
            if not title and remainder:
                title = remainder
    if not identifier:
        identifier = stem
    return identifier, title


def _parse_status(value: str) -> DecisionStatus:
    folded = value.strip().casefold()
    for member in DecisionStatus:
        if member is DecisionStatus.UNKNOWN:
            continue
        if folded.startswith(member.value.casefold()):
            return member
    return DecisionStatus.UNKNOWN


def _first_heading(text: str) -> str | None:
    match = _HEADING_RE.search(text)
    return match.group(2) if match else None


def _split_sections(text: str) -> dict[str, str]:
    """Body text under each ``## Context`` / ``## Decision`` / ... heading."""
    headings = list(_HEADING_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(headings):
        name = match.group(2).strip().casefold().rstrip(":")
        if name not in _SECTION_NAMES:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        sections[name] = text[match.end() : end].strip()
    return sections


def _collapse(text: str, *, limit: int = 400) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"
