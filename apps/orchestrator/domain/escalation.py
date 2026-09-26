"""Escalations a run writes for a person (build.md sections 24 and 25).

``domain.review`` already renders the escalation that a *review* produces, and
it can say things this module cannot: the reviewer's own concern, the blocking
findings, the confidence it reported. This module is for the other half of the
loop -- the escalation nobody reviewed. Three coding attempts whose tests never
passed, a candidate whose edits could not be applied, an attempt refused before
it started: those exhaust a task without a reviewer ever having seen it, and a
person still has to be told what happened.

Section 24 is the whole brief:

> The human should not have to reconstruct the history manually.

So the attempt-by-attempt history is on the page, in the order it happened,
with the command or the reason that ended each one. The options are stated as
choices rather than as a question, and what is said about the repository is
what is actually true of it -- which is not always "restored", because a
candidate held for a human decision has deliberately not been rolled back.

Pure: no I/O, no model, no repository, no database.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from .context import render_bullet_list
from .enums import FailureReason


class EscalationIntent(StrEnum):
    """What a person's answer *does*, as opposed to what it says.

    An escalation's options are prose, because only a person can weigh a
    project's alternatives and the orchestrator must not invent them
    (principle 7). But prose is not something the workflow can act on, and
    parsing an intention back out of a sentence would be the system guessing
    what a human decided. So every option carries one of these alongside its
    text, and the human picks an option rather than writing an instruction
    (concern 32).

    The set is deliberately small. Each member is a move the orchestrator can
    actually make on its own, and there is no member for "do something else":
    an answer that fits none of them is a dismissal plus a person doing the
    work by hand.
    """

    #: Commit what the run produced and complete the task. Only ever offered
    #: for a candidate that exists and was not rolled back.
    ACCEPT_CANDIDATE = "ACCEPT_CANDIDATE"
    #: Send the candidate back to the coder with the person's own words as the
    #: correction. The answer text becomes the feedback the next run starts on.
    REQUEST_CHANGES = "REQUEST_CHANGES"
    #: Discard this attempt and let the task be selected again from scratch.
    RETRY_TASK = "RETRY_TASK"
    #: The person did the work themselves; the task is done and the
    #: orchestrator has nothing to commit.
    COMPLETED_BY_HAND = "COMPLETED_BY_HAND"
    #: Stop working on this task. Nothing of it is delivered.
    ABANDON_TASK = "ABANDON_TASK"


class EscalationOption(str):
    """One offered decision: what it says, and what it would do.

    ``key`` is the letter a person quotes ("A"), ``text`` is the sentence they
    read, and ``intent`` is the only part the workflow looks at.
    """

    key: str
    intent: EscalationIntent
    text: str

    def __new__(
        cls, key: str, intent: EscalationIntent, text: str
    ) -> EscalationOption:
        # Being a string is intentional compatibility, not presentation
        # leakage. Older callers and rows treated options as their rendered
        # labels (and may join or slice them); Phase K adds the machine intent
        # without making those callers stop working.
        instance = super().__new__(cls, f"{key}. {text}")
        instance.key = key
        instance.intent = intent
        instance.text = text
        return instance

    @property
    def label(self) -> str:
        """The line as a person sees it in an escalation."""
        return str(self)

    def describe(self) -> dict[str, str]:
        return {"key": self.key, "intent": self.intent.value, "text": self.text}

    @classmethod
    def restore(cls, value: object) -> EscalationOption | None:
        """Rebuild a stored option, or return ``None`` if it is not one.

        Tolerant because the column is JSON and older rows hold plain
        strings: an escalation written before options carried intents is
        still readable, it just cannot be acted on programmatically.
        """
        if not isinstance(value, dict):
            return None
        try:
            return cls(
                key=str(value["key"]),
                intent=EscalationIntent(value["intent"]),
                text=str(value["text"]),
            )
        except (KeyError, ValueError):
            return None


def option_labels(options: Sequence[EscalationOption | str]) -> tuple[str, ...]:
    """The lines to render. Rendering never sees an intent."""
    return tuple(
        option.label if isinstance(option, EscalationOption) else str(option)
        for option in options
    )


def option_for_intent(
    options: Sequence[EscalationOption | str], intent: EscalationIntent
) -> EscalationOption | None:
    """The first option offering ``intent``, if it was offered at all.

    Used to refuse an answer nobody was asked for: accepting a candidate that
    was rolled back is not a decision this run put on the table.
    """
    return next(
        (
            option
            for option in options
            if isinstance(option, EscalationOption) and option.intent is intent
        ),
        None,
    )

#: How the repository stands when a person is asked to decide. Which of these
#: is true is the loop's to say, not this module's: rolling a worktree back is
#: an action with consequences and the escalation must describe the one that
#: was actually taken.
WORKTREE_PRESERVED = (
    "The managed repository is unchanged at known-good SHA {commit}. The last "
    "candidate is uncommitted in the run's worktree on its task branch, and "
    "nothing has been committed, merged or pushed. Every attempt's diff is "
    "kept as a run artifact."
)
WORKTREE_ROLLED_BACK = (
    "The managed repository is unchanged at known-good SHA {commit}, and the "
    "run's worktree has been reset back to it: the rejected candidate is no "
    "longer on disk. Every attempt's diff is kept as a run artifact."
)


def render_run_escalation(
    *,
    external_task_id: str,
    reason: str,
    requirement: str,
    blocker: str,
    attempts: Sequence[str] = (),
    options: Sequence[str] = (),
    starting_commit: str | None = None,
    rolled_back: bool = False,
) -> str:
    """An escalation in section 24's shape, for a run that had no review.

    Args:
        reason: why a person is being asked, in one line.
        requirement: what the task was asked to do, in its own words.
        blocker: the thing that actually stopped it -- the failing command, the
            refused edit, the ceiling that was reached.
        attempts: one line per attempt, oldest first. Section 24's example
            lists the approaches that were tried, and this is the part a person
            would otherwise have to reconstruct from artifacts by hand.
        starting_commit: the run's known-good SHA.
        rolled_back: whether the worktree was reset. Decides which of the two
            repository descriptions is true.
    """
    sections = [
        f"TASK {external_task_id} — HUMAN REVIEW REQUIRED",
        "",
        "Reason:",
        reason,
        "",
        "Requirement:",
        requirement or "(the task carries no instructions)",
        "",
        "Current blocker:",
        blocker,
        "",
        render_bullet_list("Attempts", list(attempts)),
        "",
        render_bullet_list("Options", list(options)),
    ]
    if starting_commit:
        template = WORKTREE_ROLLED_BACK if rolled_back else WORKTREE_PRESERVED
        sections.extend(["", "Current repository:", template.format(commit=starting_commit)])
    return "\n".join(sections)


def run_escalation_options(reason: FailureReason) -> tuple[EscalationOption, ...]:
    """Decisions to offer a person, by what ended the run.

    Deliberately generic, for the reason ``escalation_options`` gives: the
    orchestrator knows what it cannot decide, it does not know the project's
    alternatives, and inventing them would be the system inventing
    architectural decisions (principle 7). What does vary is whether accepting
    the candidate is even on the table -- it is not here, in either set: a run
    that exhausted its attempts never produced a candidate a reviewer would
    take, and one that broke its scope has had the candidate rolled back.
    """
    if reason is FailureReason.RETRY_EXHAUSTED:
        return (
            EscalationOption(
                "A",
                EscalationIntent.RETRY_TASK,
                "Reword or split the task and run it again from the known-good SHA.",
            ),
            EscalationOption(
                "B",
                EscalationIntent.COMPLETED_BY_HAND,
                "Fix the blocker by hand and mark the task complete.",
            ),
            EscalationOption(
                "C",
                EscalationIntent.ABANDON_TASK,
                "Abandon the task; nothing of it has been committed.",
            ),
        )
    if reason in {FailureReason.SCOPE_VIOLATION, FailureReason.SECURITY_FAILED}:
        return (
            EscalationOption(
                "A",
                EscalationIntent.RETRY_TASK,
                "Widen the task's declared allowance if the change was legitimate, "
                "and run it again.",
            ),
            EscalationOption(
                "B",
                EscalationIntent.RETRY_TASK,
                "Reword the task so the work falls inside its allowance, and run it again.",
            ),
            EscalationOption(
                "C",
                EscalationIntent.ABANDON_TASK,
                "Abandon the task; the candidate has been rolled back.",
            ),
        )
    return (
        EscalationOption(
            "A",
            EscalationIntent.REQUEST_CHANGES,
            "Answer the question the run could not; your answer is sent to the coder.",
        ),
        EscalationOption(
            "B",
            EscalationIntent.RETRY_TASK,
            "Change the task specification and run it again.",
        ),
        EscalationOption(
            "C",
            EscalationIntent.ABANDON_TASK,
            "Abandon the task and leave the repository as it is.",
        ),
    )


__all__ = [
    "WORKTREE_PRESERVED",
    "WORKTREE_ROLLED_BACK",
    "EscalationIntent",
    "EscalationOption",
    "option_for_intent",
    "option_labels",
    "render_run_escalation",
    "run_escalation_options",
]
