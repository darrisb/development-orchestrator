"""Dependency graph rules (build.md section 26)."""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.dependencies import (
    evaluate_readiness,
    topological_order,
    validate_graph,
)
from apps.orchestrator.domain.enums import TaskStatus
from apps.orchestrator.domain.errors import DependencyCycleError, UnknownDependencyError


def test_valid_graph_passes_validation():
    validate_graph({"A": [], "B": ["A"], "C": ["A", "B"]})


def test_dependency_on_unknown_task_is_rejected():
    with pytest.raises(UnknownDependencyError) as excinfo:
        validate_graph({"A": ["Z"]})
    assert excinfo.value.task_id == "A"
    assert excinfo.value.missing == "Z"


def test_self_dependency_is_a_cycle():
    with pytest.raises(DependencyCycleError) as excinfo:
        validate_graph({"A": ["A"]})
    assert excinfo.value.cycle == ["A", "A"]


def test_cycle_is_reported_with_its_path():
    with pytest.raises(DependencyCycleError) as excinfo:
        validate_graph({"A": ["B"], "B": ["C"], "C": ["A"]})
    cycle = excinfo.value.cycle
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"A", "B", "C"}


def test_diamond_dependencies_are_not_a_cycle():
    validate_graph({"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]})


def test_deep_chain_does_not_exhaust_the_stack():
    """Import of a large manifest must not depend on the recursion limit."""
    graph = {f"T-{i:04d}": ([f"T-{i - 1:04d}"] if i else []) for i in range(3000)}
    assert topological_order(graph)[0] == "T-0000"


def test_topological_order_is_dependency_first_and_deterministic():
    graph = {"C": ["A"], "B": ["A"], "A": [], "D": ["B", "C"]}
    assert topological_order(graph) == ["A", "B", "C", "D"]


def test_a_task_is_ready_only_when_every_dependency_is_complete():
    graph = {"A": [], "B": ["A"], "C": ["A", "B"]}
    statuses = {"A": TaskStatus.COMPLETE, "B": TaskStatus.PENDING, "C": TaskStatus.PENDING}
    report = evaluate_readiness(graph, statuses)
    assert report.ready == ("B",)
    assert report.waiting == {"C": ("B",)}
    assert report.blocked == {}


def test_a_failed_dependency_blocks_its_dependents():
    graph = {"A": [], "B": ["A"]}
    report = evaluate_readiness(graph, {"A": TaskStatus.FAILED, "B": TaskStatus.PENDING})
    assert report.blocked == {"B": ("A",)}
    # A itself is FAILED: retrying it is a decision, not a graph consequence.
    assert report.ready == ()


def test_tasks_the_graph_does_not_own_are_left_out_of_the_report():
    """Complete, in flight, failed or paused: not the graph's call to make."""
    graph = {"A": [], "B": [], "C": [], "D": [], "E": []}
    report = evaluate_readiness(
        graph,
        {
            "A": TaskStatus.COMPLETE,
            "B": TaskStatus.CODING,
            "C": TaskStatus.HUMAN_REVIEW,
            "D": TaskStatus.FAILED,
            "E": TaskStatus.PAUSED,
        },
    )
    assert report.ready == ()
    assert report.blocked == {}
    assert report.waiting == {}


def test_a_blocked_task_becomes_ready_again_once_dependencies_complete():
    graph = {"A": [], "B": ["A"]}
    report = evaluate_readiness(graph, {"A": TaskStatus.COMPLETE, "B": TaskStatus.BLOCKED})
    assert report.is_ready("B")


def test_readiness_ignores_dictionary_order():
    graph = {"B": [], "A": []}
    assert evaluate_readiness(graph, {}).ready == ("A", "B")
