"""The task specification, as the coder is shown it (build.md sections 6, 15).

Section 15 ranks "exact task instructions" first, and section 52 says the
model must never be responsible for remembering the workflow. Both point the
same way: the task block is generated from the database record, in a fixed
order, with the boundaries stated explicitly -- what to change, what not to
change, how the work will be checked, and when to stop.

Pure rendering. No I/O, no repository access, no model involved.
"""

from __future__ import annotations

from .models import Task
from .tokens import estimate_tokens

#: Bumped whenever this rendering changes. Recorded against the run so an
#: outcome can be attributed to the prompt that produced it (section 34) --
#: a silent change to the task block would otherwise look like a model
#: regression.
TASK_SPEC_VERSION = "task-spec/1"


def render_task_specification(
    task: Task, *, dependency_titles: dict[str, str] | None = None
) -> str:
    """The task contract as prompt text.

    Args:
        dependency_titles: external task id -> title, for naming prerequisites
            in words rather than as bare identifiers. Missing entries are
            rendered as the identifier alone.
    """
    titles = dependency_titles or {}
    lines: list[str] = [
        f"Task: {task.external_task_id} — {task.title}",
        f"Complexity: {task.complexity} | Risk: {task.risk_level}",
    ]
    if task.section is not None:
        lines.append(f"Specification section: {task.section}")

    lines.append("")
    lines.append("### Goal")
    lines.append(task.instructions.strip() if task.instructions else task.title)

    if task.depends_on:
        lines.extend(["", "### Prerequisites (already complete)"])
        lines.extend(
            f"- {dependency}: {titles[dependency]}" if dependency in titles else f"- {dependency}"
            for dependency in task.depends_on
        )

    lines.extend(["", "### Files"])
    lines.append(_path_line("Inspect (read, do not change)", task.files_to_inspect))
    lines.append(_path_line("May modify", task.files_to_modify))
    lines.append(_path_line("May create", task.files_to_create))
    if not task.declared_paths:
        lines.append(
            "- This task declares no file list. Keep the change as small as the goal allows."
        )

    lines.extend(["", "### Scope boundary"])
    lines.extend(
        [
            f"- Change at most {task.limits.max_files_changed} files and "
            f"{task.limits.max_diff_lines} diff lines.",
            "- Do not change files outside the lists above.",
            "- Do not refactor unrelated code, upgrade dependencies, or edit CI "
            "configuration unless this task says to.",
            "- Do not commit: the orchestrator commits work that passes.",
        ]
    )

    lines.extend(["", "### How this work will be checked"])
    if task.verify_commands:
        lines.extend(f"- `{command}`" for command in task.verify_commands)
        lines.append(
            "- These commands are run by the orchestrator, not by you. Their output, "
            "not your description of it, decides whether the task passed."
        )
    else:
        lines.append(
            "- This task declares no verification commands; a reviewer will read the diff."
        )

    lines.extend(
        [
            "",
            "### Stop boundary",
            f"- Implement {task.external_task_id} and nothing after it.",
            "- Stop when the goal is met and the checks above would pass.",
            f"- At most {task.limits.max_attempts} attempts and "
            f"{task.limits.max_review_cycles} review cycles are available.",
        ]
    )
    return "\n".join(lines)


def estimated_specification_tokens(task: Task) -> int:
    return estimate_tokens(render_task_specification(task))


def _path_line(heading: str, paths: list[str]) -> str:
    if not paths:
        return f"- {heading}: none declared"
    return f"- {heading}: " + ", ".join(f"`{path}`" for path in paths)
