"""A project's declared model preferences (build.md section 31).

The rule under test: the policy decides, and an undeclared role declares
nothing. Resolution itself -- and its refusal to fall back -- belongs to
``providers.registry`` and is tested in ``test_model_registry``.
"""

from __future__ import annotations

from apps.orchestrator.domain.enums import Complexity
from apps.orchestrator.domain.model_policy import ModelPolicy


def test_an_undeclared_policy_expresses_no_preference() -> None:
    policy = ModelPolicy()

    assert policy.is_empty
    assert policy.coder_for(Complexity.LOW) is None
    assert policy.coder_for(Complexity.HIGH) is None
    assert policy.reviewer is None


def test_ordinary_work_runs_on_the_default_coder() -> None:
    policy = ModelPolicy(default_coder="qwen-14b", high_complexity_coder="qwen-30b")

    assert policy.coder_for(Complexity.LOW) == "qwen-14b"
    assert policy.coder_for(Complexity.MEDIUM) == "qwen-14b"


def test_a_high_complexity_task_escalates_to_the_stronger_coder() -> None:
    policy = ModelPolicy(default_coder="qwen-14b", high_complexity_coder="qwen-30b")

    assert policy.coder_for(Complexity.HIGH) == "qwen-30b"


def test_without_a_stronger_coder_a_hard_task_still_uses_the_default() -> None:
    """The stronger model is an escalation, not a separate requirement."""
    policy = ModelPolicy(default_coder="qwen-14b")

    assert policy.coder_for(Complexity.HIGH) == "qwen-14b"


def test_a_stronger_coder_alone_leaves_ordinary_work_unrouted() -> None:
    policy = ModelPolicy(high_complexity_coder="qwen-30b")

    assert policy.coder_for(Complexity.MEDIUM) is None
    assert policy.coder_for(Complexity.HIGH) == "qwen-30b"
    assert not policy.is_empty


def test_only_the_declared_roles_are_stored() -> None:
    """An empty policy must store what an unmigrated row already holds."""
    assert ModelPolicy().describe() == {}
    assert ModelPolicy(reviewer="gpt-reviewer").describe() == {"reviewer": "gpt-reviewer"}


def test_a_stored_policy_round_trips() -> None:
    policy = ModelPolicy(
        default_coder="qwen-14b", high_complexity_coder="qwen-30b", reviewer="gpt-reviewer"
    )

    assert ModelPolicy.from_mapping(policy.describe()) == policy


def test_an_absent_or_blank_entry_reads_back_as_no_preference() -> None:
    assert ModelPolicy.from_mapping(None) == ModelPolicy()
    assert ModelPolicy.from_mapping({}) == ModelPolicy()
    assert ModelPolicy.from_mapping({"default_coder": "  "}) == ModelPolicy()
    assert ModelPolicy.from_mapping({"default_coder": 7}) == ModelPolicy()
