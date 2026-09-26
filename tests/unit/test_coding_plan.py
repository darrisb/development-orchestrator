"""Plan mode and plan validation (build.md section 14, phase G items 2 and 3).

Section 14's own example of a plan worth refusing -- "a small task proposing
dozens of unrelated file changes" -- is the last test here.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from apps.orchestrator.domain.enums import Complexity, ScopePolicyDecision
from apps.orchestrator.domain.models import Task, TaskLimits
from apps.orchestrator.domain.plan import (
    CodingPlan,
    requires_plan,
    validate_plan,
)
from apps.orchestrator.domain.scope import ScopePolicy


def _task(**overrides) -> Task:
    fields: dict[str, object] = {
        "project_id": uuid4(),
        "external_task_id": "TS-004",
        "title": "Implement navigation tree",
        "complexity": Complexity.MEDIUM,
        "files_to_inspect": ["src/widgets/tree.ts"],
        "files_to_modify": ["src/navigation.ts"],
        "files_to_create": ["src/navigationTree.ts"],
        "limits": TaskLimits(max_files_changed=3),
    }
    fields.update(overrides)
    return Task(**fields)  # type: ignore[arg-type]


def _plan(**overrides) -> CodingPlan:
    fields: dict[str, object] = {
        "files_to_inspect": ("src/widgets/tree.ts",),
        "files_to_modify": ("src/navigation.ts",),
        "files_to_create": ("src/navigationTree.ts",),
        "approach": ("Read the tree widget", "Render each node"),
    }
    fields.update(overrides)
    return CodingPlan(**fields)  # type: ignore[arg-type]


# --- when a plan is required -------------------------------------------------


@pytest.mark.parametrize(
    ("complexity", "expected"),
    [(Complexity.LOW, False), (Complexity.MEDIUM, True), (Complexity.HIGH, True)],
)
def test_medium_and_high_complexity_tasks_must_plan_first(
    complexity: Complexity, expected: bool
):
    assert requires_plan(_task(complexity=complexity)) is expected


# --- parsing -----------------------------------------------------------------


def test_a_plan_is_read_from_the_schema_field_names():
    plan = CodingPlan.from_payload(
        {
            "filesToInspect": ["./src/widgets/tree.ts"],
            "filesToModify": ["src/navigation.ts", "src/navigation.ts"],
            "filesToCreate": [],
            "approach": ["step one", "  ", "step two"],
            "risks": "deep nesting",
            "expectedTests": ["tests/navigation.test.ts"],
        }
    )

    assert plan.files_to_inspect == ("src/widgets/tree.ts",)
    # Duplicates collapse; a single string where a list was asked for is read
    # as a one-item list; blank entries are dropped.
    assert plan.files_to_modify == ("src/navigation.ts",)
    assert plan.approach == ("step one", "step two")
    assert plan.risks == ("deep nesting",)


def test_an_unusable_path_is_kept_so_the_validator_can_object_to_it_by_name():
    plan = CodingPlan.from_payload({"filesToModify": ["../../etc/passwd"], "approach": ["go"]})

    assert plan.files_to_modify == ("../../etc/passwd",)
    assessment = validate_plan(plan, _task(), ScopePolicy.for_task(_task()))
    assert assessment.decision is ScopePolicyDecision.BLOCK
    assert "not a repository-relative path" in assessment.summary()


# --- validation --------------------------------------------------------------


def test_a_plan_within_the_declared_allowance_is_approved():
    task = _task()
    assessment = validate_plan(_plan(), task, ScopePolicy.for_task(task))

    assert assessment.decision is ScopePolicyDecision.ALLOW
    assert assessment.approved
    assert not assessment.needs_human


def test_a_plan_that_would_write_outside_the_allowance_is_blocked_before_any_code():
    task = _task()
    plan = _plan(files_to_modify=("src/navigation.ts", "src/billing.ts"))

    assessment = validate_plan(plan, task, ScopePolicy.for_task(task))

    assert assessment.decision is ScopePolicyDecision.BLOCK
    assert "src/billing.ts" in assessment.summary()
    # The refusal is expressed as feedback, because the coder gets another go.
    assert "src/billing.ts" in assessment.feedback()


def test_a_plan_that_would_write_a_file_declared_for_inspection_is_blocked():
    task = _task()
    plan = _plan(files_to_modify=("src/widgets/tree.ts",))

    assert validate_plan(plan, task, ScopePolicy.for_task(task)).decision is (
        ScopePolicyDecision.BLOCK
    )


def test_a_plan_with_no_approach_is_blocked():
    task = _task()

    assessment = validate_plan(_plan(approach=()), task, ScopePolicy.for_task(task))

    assert assessment.decision is ScopePolicyDecision.BLOCK
    assert "no approach" in assessment.summary()


def test_a_plan_that_writes_nothing_cannot_implement_the_task():
    task = _task()
    plan = _plan(files_to_modify=(), files_to_create=())

    assessment = validate_plan(plan, task, ScopePolicy.for_task(task))

    assert assessment.decision is ScopePolicyDecision.BLOCK
    assert "cannot implement TS-004" in assessment.summary()


def test_a_plan_over_the_task_file_limit_is_blocked():
    task = _task(files_to_modify=[], files_to_create=[], limits=TaskLimits(max_files_changed=2))
    plan = _plan(files_to_modify=("a.ts", "b.ts", "c.ts"), files_to_create=())

    assessment = validate_plan(plan, task, ScopePolicy.for_task(task))

    assert assessment.decision is ScopePolicyDecision.BLOCK
    assert "the task allows 2" in assessment.summary()


def test_a_small_task_proposing_many_unrelated_changes_is_escalated():
    """Section 14's example. The task declared no files, so nothing is out of
    bounds -- but a low-complexity task rewriting nine of them is more likely a
    misread task than an ambitious one, and only a human can say which."""
    task = _task(
        complexity=Complexity.LOW,
        files_to_inspect=[],
        files_to_modify=[],
        files_to_create=[],
        limits=TaskLimits(max_files_changed=20),
    )
    plan = _plan(
        files_to_modify=tuple(f"src/module{index}.ts" for index in range(9)),
        files_to_create=(),
    )

    assessment = validate_plan(plan, task, ScopePolicy.for_task(task))

    assert assessment.decision is ScopePolicyDecision.REQUIRE_REVIEW
    assert assessment.needs_human
    assert assessment.approved  # executable, but not without a human looking


def test_an_undeclared_sensitive_area_in_a_plan_asks_for_a_human():
    task = _task(files_to_inspect=[], files_to_modify=[], files_to_create=[])
    plan = _plan(files_to_modify=("src/auth/session.ts",), files_to_create=())

    assessment = validate_plan(plan, task, ScopePolicy.for_task(task))

    assert assessment.decision is ScopePolicyDecision.REQUIRE_REVIEW
    assert "security file the task did not declare" in assessment.summary()


def test_a_narrower_plan_than_the_manifest_expected_is_recorded_but_allowed():
    """A conservative coder is not a suspicious one; the note exists for the
    reviewer, not to spend a human's attention."""
    task = _task()
    plan = _plan(files_to_create=())

    assessment = validate_plan(plan, task, ScopePolicy.for_task(task))

    assert assessment.decision is ScopePolicyDecision.ALLOW
    assert "src/navigationTree.ts" in assessment.summary()


# --- rendering ---------------------------------------------------------------


def test_an_approved_plan_renders_back_into_the_coding_prompt():
    text = _plan().render()

    assert "Approved plan — approach" in text
    assert "src/navigation.ts" in text
