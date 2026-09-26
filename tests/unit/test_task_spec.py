"""The rendered task specification (build.md sections 6 and 52)."""

from __future__ import annotations

from uuid import uuid4

from apps.orchestrator.domain.models import Task, TaskLimits
from apps.orchestrator.domain.task_spec import TASK_SPEC_VERSION, render_task_specification


def _task(**overrides) -> Task:
    defaults = {
        "project_id": uuid4(),
        "external_task_id": "TS-004",
        "title": "Implement navigation tree",
        "instructions": "Render the tree in the sidebar.",
        "depends_on": ["TS-001"],
        "verify_commands": ["npm run compile", "npm test"],
        "files_to_modify": ["src/navigation.ts"],
        "files_to_create": ["src/navigationTree.ts"],
        "limits": TaskLimits(max_files_changed=4, max_diff_lines=200),
    }
    return Task(**{**defaults, **overrides})


def test_the_block_states_every_boundary_the_model_must_not_have_to_remember():
    text = render_task_specification(_task(), dependency_titles={"TS-001": "Scaffold"})

    assert "TS-004 — Implement navigation tree" in text
    assert "Render the tree in the sidebar." in text
    assert "- TS-001: Scaffold" in text
    assert "`src/navigation.ts`" in text
    assert "`src/navigationTree.ts`" in text
    assert "Change at most 4 files and 200 diff lines." in text
    assert "Do not commit" in text
    assert "`npm test`" in text
    assert "Implement TS-004 and nothing after it." in text
    assert "At most 3 attempts and 3 review cycles" in text


def test_verification_is_described_as_something_the_orchestrator_runs():
    """Section 52: the model is never responsible for proving its own work."""
    text = render_task_specification(_task())

    assert "run by the orchestrator, not by you" in text


def test_a_task_without_a_file_list_is_told_to_stay_small():
    text = render_task_specification(_task(files_to_modify=[], files_to_create=[]))

    assert "declares no file list" in text
    assert "as small as the goal allows" in text


def test_a_task_without_verification_commands_says_so_rather_than_implying_none_are_needed():
    text = render_task_specification(_task(verify_commands=[]))

    assert "declares no verification commands" in text


def test_an_unnamed_dependency_still_appears():
    text = render_task_specification(_task(depends_on=["TS-009"]), dependency_titles={})

    assert "- TS-009" in text


def test_rendering_is_deterministic_and_versioned():
    task = _task()

    assert render_task_specification(task) == render_task_specification(task)
    assert TASK_SPEC_VERSION == "task-spec/1"
