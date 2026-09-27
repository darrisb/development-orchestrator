"""Task dependency graph (build.md section 26).

Tasks form a DAG keyed by external task id. This module is pure graph logic:
it never touches the database, so both the manifest parser (validating a file
before import) and the scheduler (deciding what may run now) share it.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
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

    #: Dependencies all COMPLETE *and* integrated: eligible for READY.
    ready: tuple[str, ...] = ()
    #: task id -> dependencies that cannot currently be satisfied: FAILED,
    #: BLOCKED, or complete with output that is not in the integration baseline.
    blocked: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: task id -> dependencies that are simply not COMPLETE yet.
    waiting: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: task id -> the subset of its ``blocked`` dependencies that are complete
    #: but whose accepted output is not in the integration baseline (concern
    #: 51). Reported separately because the two cases read identically in
    #: ``blocked`` and want opposite responses: a FAILED dependency is a task to
    #: re-run, and this is a delivered task whose *integration* a person has to
    #: resolve.
    unintegrated: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def is_ready(self, task_id: str) -> bool:
        return task_id in self.ready


def evaluate_readiness(
    graph: Graph,
    statuses: Mapping[str, TaskStatus],
    *,
    unintegrated: Collection[str] = (),
) -> ReadinessReport:
    """Classify every task whose next move the dependency graph decides.

    A task is ready only when every dependency is ``COMPLETE`` (section 26)
    *and* the dependency's accepted output is in the cumulative integration
    baseline (concern 51). The second half is not a refinement of the first: a
    candidate can pass verification and review, be delivered, mark its task
    ``COMPLETE``, and then fail to merge or fail the cumulative gate, and in
    that state the tree a dependent task would start from does not contain the
    work it was told to build on. ``COMPLETE`` answers "was this task done";
    ``unintegrated`` answers "is it in the tree", and only the two together mean
    a dependency is satisfied.

    Such a dependency is reported as unsatisfiable rather than as waiting,
    because nothing the orchestrator does on its own will change it -- a person
    has to resolve the integration -- and ``BLOCKED`` is the state that says so.
    It recovers: resolving the integration clears the flag and the next
    readiness pass promotes the dependent.

    Only ``GRAPH_MANAGED_STATES`` are classified: a task that is complete, in
    flight, failed, paused, or awaiting a human is owned by its run or by an
    operator decision, and readiness must never quietly re-queue it.

    Args:
        unintegrated: ids of tasks whose accepted output is *not* in the
            baseline. Passed in rather than derived, because this module is pure
            graph logic and whether a commit is in a ref is not graph logic.
    """
    outstanding_integration = frozenset(unintegrated)
    ready: list[str] = []
    blocked: dict[str, tuple[str, ...]] = {}
    waiting: dict[str, tuple[str, ...]] = {}
    unintegrated_deps: dict[str, tuple[str, ...]] = {}

    for task_id in sorted(graph):
        if statuses.get(task_id, TaskStatus.PENDING) not in GRAPH_MANAGED_STATES:
            continue
        dependencies = graph[task_id]
        not_in_baseline = tuple(
            dependency
            for dependency in dependencies
            if dependency in outstanding_integration
        )
        unsatisfiable = tuple(
            dependency
            for dependency in dependencies
            if statuses.get(dependency, TaskStatus.PENDING) in UNSATISFIABLE_DEPENDENCY_STATES
            or dependency in outstanding_integration
        )
        if unsatisfiable:
            blocked[task_id] = unsatisfiable
            if not_in_baseline:
                unintegrated_deps[task_id] = not_in_baseline
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

    return ReadinessReport(
        ready=tuple(ready),
        blocked=blocked,
        waiting=waiting,
        unintegrated=unintegrated_deps,
    )
