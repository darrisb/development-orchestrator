"""Concern 78, stage 1: the coder tests its own work, the orchestrator certifies it.

The defect these tests pin is not a wrong edit but a wrong division of labour.
A coding session that believes it must prove the project green will run the
whole regression suite, stash its own work to measure a baseline, re-run the
suite and then spend the rest of its budget explaining failures it did not
cause. None of that changes the candidate, and all of it is work the
orchestrator does deterministically after the candidate is returned.

So the contract is split in two, and each half is asserted here:

* test creation and targeted feedback belong to the coder;
* authoritative project verification belongs to the orchestrator.

The assertions are deliberately about the *prompt text*, because stage 1 is a
prompt-contract change: no runtime enforcement was added, and the verification
runner is untouched.
"""

from __future__ import annotations

from apps.orchestrator.agents.prompts import (
    CODER_PROMPT_VERSION,
    CODER_SYSTEM_PROMPT,
    render_coding_instructions,
)
from apps.orchestrator.domain.enums import Complexity
from apps.orchestrator.domain.models import Task


def _task() -> Task:
    return Task(
        id=78,
        project_id=1,
        external_task_id="TS-078",
        title="Bound the coder's verification",
        instructions="Split test creation from project certification.",
        complexity=Complexity.LOW,
    )


def _flat(text: str) -> str:
    """One long lower-cased line.

    The prompt is hard-wrapped for readability, so a phrase the contract
    states as a sentence is split across two lines in the string. Asserting
    against the wrapped text would make every assertion depend on where the
    wrap happens to fall, and a reflow would read as a lost rule.
    """
    return " ".join(text.split()).lower()


def _code_prompt() -> str:
    """Both halves of what a first coding attempt is served, flattened."""
    return _flat(CODER_SYSTEM_PROMPT + "\n\n" + render_coding_instructions(_task()))


def _fix_prompt() -> str:
    """Both halves of what a correction attempt is served, flattened."""
    return _flat(
        CODER_SYSTEM_PROMPT
        + "\n\n"
        + render_coding_instructions(_task(), is_fix_attempt=True)
    )


# --------------------------------------------------------------- CODE contract


def test_the_code_prompt_still_asks_for_the_implementation() -> None:
    assert "implement the task described in the repository context below" in _code_prompt()


def test_the_code_prompt_still_makes_the_coder_write_the_tests() -> None:
    lowered = _code_prompt()

    assert "writing tests is yours" in lowered
    assert "add or update the tests that cover the behaviour you changed" in lowered
    assert "the orchestrator verifies your candidate after you return it" in lowered
    # The explicit refusal of the lazy reading of this contract.
    assert '"the orchestrator will test it" is not a reason to leave a change untested' in lowered


def test_the_code_prompt_allows_targeted_testing_of_the_coders_own_change() -> None:
    lowered = _code_prompt()

    assert "targeted testing of your own change is allowed" in lowered
    assert "you may run the specific tests that cover the code and tests" in lowered
    assert "you just touched" in lowered
    assert "a single test file, a single test case" in lowered


def test_the_code_prompt_delegates_authoritative_verification_to_the_orchestrator() -> None:
    lowered = _code_prompt()

    assert "authoritative verification is not yours" in lowered
    assert "against your candidate after you return" in lowered
    assert "do not try to reproduce it, and do not wait for it" in lowered


def test_the_code_prompt_forbids_full_suite_and_baseline_comparison() -> None:
    lowered = _code_prompt()

    assert "do not run the project's full verification workflow" in lowered
    assert "or its complete regression suite" in lowered
    assert "do not run a baseline-versus-candidate comparison" in lowered
    assert "do not stash, revert or re-run the repository to establish" in lowered
    assert "what was already failing" in lowered


def test_the_code_prompt_disowns_unrelated_and_pre_existing_failures() -> None:
    lowered = _code_prompt()

    assert "failures you did not cause are not yours" in lowered
    assert "the project has pre-existing and unrelated failures" in lowered
    assert "investigating them, explaining them or fixing them is not part of this task" in lowered


def test_the_code_prompt_says_returning_the_candidate_is_finishing() -> None:
    lowered = _code_prompt()

    assert "return your edits as soon as the implementation and its targeted tests are" in lowered
    assert "finishing is returning the candidate, not proving the project green" in lowered


def test_the_shared_rules_no_longer_assume_a_coder_without_any_shell() -> None:
    """The bounded contract only means something if a command may be possible.

    The old rules stated flatly that the coder never runs commands, which is
    true of this installation's structured-edit sessions and false of a coding
    session that does have a shell -- and it is the second kind that burned
    its budget on the full suite. The rule that matters either way is kept:
    packages, dependencies and Git are still never the coder's.
    """
    flat = _flat(CODER_SYSTEM_PROMPT)

    assert "never run commands" not in flat
    assert "never install packages, manage dependencies, or touch git" in flat
    assert "may give you no way to run commands at all" in flat


# ---------------------------------------------------------- FIX/repair contract


def test_the_fix_prompt_carries_the_same_bounded_testing_contract() -> None:
    """Word for word the same block, so the two attempts cannot drift apart."""
    code = _flat(render_coding_instructions(_task()))
    fix = _flat(render_coding_instructions(_task(), is_fix_attempt=True))

    for phrase in (
        "writing tests is yours.",
        "targeted testing of your own change is allowed.",
        "authoritative verification is not yours.",
        "do not run the project's full verification workflow",
        "failures you did not cause are not yours.",
    ):
        assert phrase in code
        assert phrase in fix


def test_the_fix_prompt_adds_the_repair_contract() -> None:
    lowered = _fix_prompt()

    assert "this is a repair request" in lowered
    assert "repair exactly the issues you were shown" in lowered
    assert "keep or add the tests that cover the repaired behaviour" in lowered
    assert "you may run only the specific tests directly affected" in lowered
    assert "do not re-run the project's full verification workflow" in lowered
    assert "not yours to chase" in lowered
    assert "return the corrected edits" in lowered
    assert "the orchestrator re-runs authoritative" in lowered


def test_the_repair_contract_is_only_served_to_a_repair_request() -> None:
    assert "this is a repair request" not in _code_prompt()


# -------------------------------------------------------------------- version


def test_the_coder_prompt_version_records_the_contract_change() -> None:
    """Stage 1 changes the coder's contract, so the version it ran under moves.

    ``coder-prompt/4`` was the whole-file-update preference; this is the
    bounded-verification split.
    """
    assert CODER_PROMPT_VERSION == "coder-prompt/6"
