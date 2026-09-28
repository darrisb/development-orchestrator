"""Domain-level errors.

Deliberately granular: build.md section 49 forbids a single generic
exception path for every failure.
"""

from __future__ import annotations

from .enums import FailureReason, TaskStatus


class DomainError(Exception):
    """Base class for all domain errors."""


class InvalidStateTransition(DomainError):
    def __init__(self, current: TaskStatus, requested: TaskStatus) -> None:
        super().__init__(f"Cannot transition task from {current} to {requested}")
        self.current = current
        self.requested = requested


class AbandonedRunError(DomainError):
    """An operator abandoned this run, so it cannot be moved to another state.

    Concern 64. ``ABANDONED`` is the one terminal status an in-flight workflow
    can still race with, because the operator writes it from a different
    transaction than the one trying to finish the run. This is the error a
    compare-and-swap on the run row raises when the abandonment won the race:
    the workflow was told to record ``SUCCEEDED``, the database found the run
    already ``ABANDONED``, and refusing is the only outcome that keeps the
    operator's decision.

    Deliberately its own type rather than ``InvalidStateTransition``: that one
    is about a *task* and carries two ``TaskStatus`` values, and reusing it for
    runs would have to lie about one of them.
    """

    def __init__(self, run_id: object) -> None:
        super().__init__(
            f"Run {run_id} was abandoned by an operator and cannot be moved "
            f"to another state"
        )
        self.run_id = run_id


class RunNotInFlightError(DomainError):
    """A run that is no longer PENDING or RUNNING is still being written to.

    Concern 64, and a different fault from :class:`AbandonedRunError`. That one
    says a person stopped this run, which is a decision to respect. This one
    says the workflow is writing to a run that has already finished on its own,
    so the workflow is later than its own run -- a lifecycle bug, and reported
    as its own type so it cannot be mistaken for a refusal an operator asked
    for.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class LimitExceeded(DomainError):
    """A configured limit (attempts, review cycles, diff size) was exceeded."""

    def __init__(self, message: str, reason: FailureReason) -> None:
        super().__init__(message)
        self.reason = reason


class ManifestError(DomainError):
    """The project manifest is structurally invalid."""


class DependencyCycleError(ManifestError):
    def __init__(self, cycle: list[str]) -> None:
        super().__init__(f"Dependency cycle detected: {' -> '.join(cycle)}")
        self.cycle = cycle


class UnknownDependencyError(ManifestError):
    def __init__(self, task_id: str, missing: str) -> None:
        super().__init__(f"Task {task_id} depends on unknown task {missing}")
        self.task_id = task_id
        self.missing = missing
