"""Agents: the model-facing steps of the workflow (build.md sections 14, 21).

An agent assembles a prompt, calls a provider and turns the answer into
something the deterministic services can act on. It owns no policy: what may
be written, what a plan may propose and what a completion report must contain
all live in ``domain``.

Nothing here may import LangGraph (section 52's boundary): the workflow calls
agents, never the other way round.
"""

from .coding_agent import (
    CANDIDATE_PATCH_ARTIFACT,
    CODER_PROMPT_ARTIFACT,
    CODER_RESPONSE_ARTIFACT,
    CODING_PROMPT_CONTRACT,
    PLAN_ARTIFACT,
    CodingAttempt,
    run_coding_attempt,
)
from .fix_loop import (
    FIX_LOOP_ARTIFACT,
    FixIteration,
    FixLoopResult,
    LoopOutcome,
    durable_checkpoint,
    run_fix_loop,
)
from .prompts import (
    CODER_PROMPT_VERSION,
    CODER_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    render_coding_instructions,
    render_plan_instructions,
)
from .review_agent import (
    ESCALATION_ARTIFACT,
    REVIEW_ARTIFACT,
    REVIEW_PACKAGE_MANIFEST_ARTIFACT,
    REVIEW_PROMPT_ARTIFACT,
    REVIEW_PROMPT_CONTRACT,
    REVIEW_RESPONSE_ARTIFACT,
    ReviewOutcome,
    policy_from_settings,
    run_review,
)
from .review_prompts import (
    REVIEWER_PROMPT_VERSION,
    REVIEWER_SYSTEM_PROMPT,
    render_review_instructions,
)

__all__ = [
    "CANDIDATE_PATCH_ARTIFACT",
    "FIX_LOOP_ARTIFACT",
    "ESCALATION_ARTIFACT",
    "CODER_PROMPT_ARTIFACT",
    "CODER_PROMPT_VERSION",
    "CODER_RESPONSE_ARTIFACT",
    "CODER_SYSTEM_PROMPT",
    "CODING_PROMPT_CONTRACT",
    "PLANNER_SYSTEM_PROMPT",
    "REVIEWER_PROMPT_VERSION",
    "REVIEWER_SYSTEM_PROMPT",
    "REVIEW_ARTIFACT",
    "REVIEW_PACKAGE_MANIFEST_ARTIFACT",
    "REVIEW_PROMPT_ARTIFACT",
    "REVIEW_PROMPT_CONTRACT",
    "REVIEW_RESPONSE_ARTIFACT",
    "PLAN_ARTIFACT",
    "CodingAttempt",
    "FixIteration",
    "FixLoopResult",
    "LoopOutcome",
    "ReviewOutcome",
    "durable_checkpoint",
    "render_coding_instructions",
    "policy_from_settings",
    "render_plan_instructions",
    "render_review_instructions",
    "run_coding_attempt",
    "run_fix_loop",
    "run_review",
]
