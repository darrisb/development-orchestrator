"""Task dependency graph (build.md section 26).

Tasks form a DAG keyed by external task id. This module is pure graph logic:
it never touches the database, so both the manifest parser (validating a file
before import) and the scheduler (deciding what may run now) share it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .enums import TaskStatus
from .errors import DependencyCycleError, UnknownDependencyError

#: A dependency in one of these states cannot be satisfied without further
#: intervention, so the dependent task is parked in ``BLOCKED`` rather than
#: left to look as if it were merely waiting its turn. Both states can recover
#: (see ``state_machine.ALLOWED_TRANSITIONS``), so blocking is not permanent.
UNSATISFIABLE_DEPENDENCY_STATES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.FAILED, TaskStatus.BLOCKED}
)

#: The only states whose next move follows from the dependency graph. Every
#: other state is owned by a run in flight or by an explicit decision (a retry
#: out of FAILED, a resume out of PAUSED), so readiness leaves them alone.
GRAPH_MANAGED_STATES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.BLOCKED}
)

#: Graph shape shared by every function here: task id -> its dependency ids.
Graph = Mapping[str, Sequence[str]]


def validate_graph(graph: Graph) -> None:
    """Raise if the graph references unknown tasks or contains a cycle.

    Raises:
        UnknownDependencyError: a task depends on an id absent from the graph.
        DependencyCycleError: the dependencies are not acyclic.
    """
    for task_id, dependencies in graph.items():
        for dependency in dependencies:
            if dependency not in graph:
                raise UnknownDependencyError(task_id, dependency)
    _assert_acyclic(graph)


def _assert_acyclic(graph: Graph) -> None:
    """Depth-first search with an explicit stack, reporting the cycle found.

    Recursion is avoided so that a long dependency chain cannot exhaust the
    interpreter stack during import of a large manifest.
    """
    visited: set[str] = set()
    on_path: set[str] = set()

    for root in graph:
        if root in visited:
            continue
        # Each frame is (task, iterator over its remaining dependencies).
        path: list[str] = []
        stack: list[tuple[str, Iterable[str]]] = [(root, iter(graph[root]))]
        on_path.add(root)
        path.append(root)
        while stack:
            task_id, pending = stack[-1]
            dependency = next(pending, None)
            if dependency is None:
                stack.pop()
                on_path.discard(task_id)
                path.pop()
                visited.add(task_id)
                continue
            if dependency in on_path:
                cycle = path[path.index(dependency) :] + [dependency]
                raise DependencyCycleError(cycle)
            if dependency in visited:
                continue
            on_path.add(dependency)
            path.append(dependency)
            stack.append((dependency, iter(graph[dependency])))


def topological_order(graph: Graph) -> list[str]:
    """Return task ids in dependency-first order.

    Ties are broken by task id so the order is reproducible across runs; the
    orchestrator must not depend on dictionary insertion order for scheduling.

    Raises:
        UnknownDependencyError: a task depends on an id absent from the graph.
        DependencyCycleError: the dependencies are not acyclic.
    """
    validate_graph(graph)
    remaining = {task_id: set(dependencies) for task_id, dependencies in graph.items()}
    ordered: list[str] = []
    while remaining:
        available = sorted(
            task_id for task_id, dependencies in remaining.items() if not dependencies
        )
        # validate_graph proved the graph acyclic, so a layer is always available.
        for task_id in available:
            ordered.append(task_id)
            del remaining[task_id]
        for dependencies in remaining.values():
            dependencies.difference_update(available)
    return ordered


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Which tasks may start now, and what the rest are waiting on."""

    #: Dependencies all COMPLETE: eligible for READY.
    ready: tuple[str, ...] = ()
    #: task id -> dependencies that are FAILED or BLOCKED.
    blocked: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: task id -> dependencies that are simply not COMPLETE yet.
    waiting: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def is_ready(self, task_id: str) -> bool:
        return task_id in self.ready


def evaluate_readiness(graph: Graph, statuses: Mapping[str, TaskStatus]) -> ReadinessReport:
    """Classify every task whose next move the dependency graph decides.

    A task is ready only when all of its dependencies are ``COMPLETE``
    (section 26). Only ``GRAPH_MANAGED_STATES`` are classified: a task that is
    complete, in flight, failed, paused, or awaiting a human is owned by its run
    or by an operator decision, and readiness must never quietly re-queue it.
    """
    ready: list[str] = []
    blocked: dict[str, tuple[str, ...]] = {}
    waiting: dict[str, tuple[str, ...]] = {}

    for task_id in sorted(graph):
        if statuses.get(task_id, TaskStatus.PENDING) not in GRAPH_MANAGED_STATES:
            continue
        dependencies = graph[task_id]
        unsatisfiable = tuple(
            dependency
            for dependency in dependencies
            if statuses.get(dependency, TaskStatus.PENDING) in UNSATISFIABLE_DEPENDENCY_STATES
        )
        if unsatisfiable:
            blocked[task_id] = unsatisfiable
            continue
        outstanding = tuple(
            dependency
            for dependency in dependencies
            if statuses.get(dependency, TaskStatus.PENDING) is not TaskStatus.COMPLETE
        )
        if outstanding:
            waiting[task_id] = outstanding
        else:
            ready.append(task_id)

    return ReadinessReport(ready=tuple(ready), blocked=blocked, waiting=waiting)
