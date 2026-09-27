"""The coding and planning prompts (build.md section 14, phase G item 1).

Three rules shaped every line of these prompts.

* **The prompt states the contract, not the workflow.** Section 52: the model
  is never responsible for remembering the workflow. So the prompt does not
  explain retries, review cycles, branches or commits -- it says what to
  return and what may be touched, because those are the only two things the
  model's answer can get wrong in a way the orchestrator cannot fix.
* **The prompt does not ask the model to prove anything.** It never says "make
  sure the tests pass"; the verification pipeline decides that. Asking a model
  to certify its own work teaches it to claim success, and the completion
  report is designed to catch exactly that claim.
* **The task specification is not repeated here.** It is the first item of the
  context package (``domain.task_spec``, section 15 priority 1). These strings
  are the output contract that wraps it, so the two cannot drift apart into
  two descriptions of the same task.

Pure text. Versioned, because an outcome must be attributable to the prompt
that produced it (section 34).
"""

from __future__ import annotations

from ..domain.models import Task
from ..domain.plan import CodingPlan

#: Bumped on any change to the strings below.
CODER_PROMPT_VERSION = "coder-prompt/1"

_SHARED_RULES = """\
You are a senior software engineer working inside an automated orchestrator on
one small, fully specified task.

How this works:
- You never run commands, install packages, use a shell, or touch Git. The
  orchestrator runs the project's build, lint and test commands after you
  answer, and it commits work that passes.
- You cannot ask questions or request more context. Everything you are given is
  everything you get. If something is genuinely missing, do the smallest
  correct thing the available context supports and say so in your answer.
- You only ever work on the task you were given. Do not implement the next
  feature, do not fix unrelated code you notice, and do not refactor,
  reformat, rename or upgrade anything the task did not ask for.
- Respect the file lists in the task specification exactly. Files outside them
  are refused by the orchestrator before they are written, which wastes the
  attempt.
- Reply with a single JSON object and nothing else: no prose before or after
  it, no Markdown fence, no explanation of the JSON."""

PLANNER_SYSTEM_PROMPT = f"""{_SHARED_RULES}

For this request you are planning only. Do not write code, and do not include
file contents. State what you will read, what you will change, and how -- in
enough detail that the plan could be handed to someone else."""

CODER_SYSTEM_PROMPT = f"""{_SHARED_RULES}

For this request you are writing the code. For every file you change you return
its complete new contents, not a diff or a patch and not an excerpt: no
placeholders, no "... rest of file unchanged", no elisions of any kind. A file
you return replaces what is in the repository, so anything you leave out is
deleted."""

_PLAN_OUTPUT_CONTRACT = """\
Return exactly this JSON object:

{
  "filesToInspect": ["repository-relative paths you need to read"],
  "filesToModify": ["existing files you will change"],
  "filesToCreate": ["new files you will add"],
  "approach": ["the ordered steps you will take, one per entry"],
  "risks": ["what could go wrong or what you are unsure about"],
  "expectedTests": ["test files you will add or change"]
}"""

_CODE_OUTPUT_CONTRACT = """\
Return exactly this JSON object:

{
  "summary": "what you changed and why, in two or three sentences",
  "edits": [
    {
      "path": "repository-relative path",
      "operation": "create" | "update" | "delete",
      "content": "the file's complete new contents ('' for a delete)"
    }
  ],
  "requirementsMet": ["each requirement of the task, and how this change meets it"],
  "testsAdded": ["test files you added or changed, exactly as they appear in 'edits'"],
  "followUps": ["work this task deliberately leaves undone"],
  "deviationsFromPlan": ["anything you did differently from the approved plan"]
}"""


def render_plan_instructions(task: Task) -> str:
    """The planning request's instructions.

    The task specification itself arrives with the context package, so this
    says only what a plan is and what shape it takes.
    """
    return "\n\n".join(
        [
            f"# Plan task {task.external_task_id}",
            (
                "Plan the implementation of the task described in the repository "
                "context below. Write no code in this reply.\n"
                "- Every path you list must be repository-relative and must appear in "
                "the task's file lists, unless the task declared no lists.\n"
                "- Do not list a file you do not actually need.\n"
                "- Your plan is validated against the task's scope before you are "
                "asked for code. A plan that would change files the task did not "
                "allow is refused."
            ),
            _PLAN_OUTPUT_CONTRACT,
        ]
    )


def render_coding_instructions(
    task: Task,
    plan: CodingPlan | None = None,
    *,
    path_output_limits: dict[str, int] | None = None,
) -> str:
    """The coding request's instructions, with the approved plan when there is one.

    When ``path_output_limits`` is provided, the effective per-path byte
    allowance for complete writable files is communicated before the first
    model call (concern 62). The prompt makes clear that edits use complete
    replacement contents and that the returned complete contents must remain
    within the stated byte limit.
    """
    sections = [
        f"# Implement task {task.external_task_id}",
        (
            "Implement the task described in the repository context below.\n"
            "- Return the complete new contents of every file you change.\n"
            "- Write or update tests when the task asks for them.\n"
            "- Keep the change as small as the task allows; stop when the goal is met."
        ),
    ]
    if path_output_limits:
        limits_lines = [
            "- The following files have byte limits on their complete replacement contents:"
        ]
        for path in sorted(path_output_limits):
            limits_lines.append(f"  - `{path}`: {path_output_limits[path]} bytes")
        limits_lines.append(
            "  Your returned complete contents for each file must not exceed its limit."
        )
        sections.append("\n".join(limits_lines))
    if plan is not None:
        sections.append(
            "Your plan was reviewed and approved. Follow it, and record in "
            "'deviationsFromPlan' anything you do differently and why.\n\n" + plan.render()
        )
    sections.append(_CODE_OUTPUT_CONTRACT)
    return "\n\n".join(sections)


__all__ = [
    "CODER_PROMPT_VERSION",
    "CODER_SYSTEM_PROMPT",
    "PLANNER_SYSTEM_PROMPT",
    "render_coding_instructions",
    "render_plan_instructions",
]
