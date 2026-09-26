"""The ``ReviewProvider`` boundary (build.md sections 2, 13 and 21).

Section 21's requirement is replaceability: *the workflow must consume the
same structured ``ReviewResult`` regardless of provider.* These tests hold the
boundary to that — the model-backed adapter satisfies the protocol, and so
does an implementation that is not a language model at all.
"""

from __future__ import annotations

import pytest

from apps.orchestrator.agents.review_prompts import (
    REVIEWER_SYSTEM_PROMPT,
    render_review_instructions,
)
from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.enums import ModelRole, ReviewDecision
from apps.orchestrator.domain.review import ReviewResult
from apps.orchestrator.domain.review_package import ReviewPackage
from apps.orchestrator.providers.base import (
    ConnectionReport,
    ProviderConfig,
)
from apps.orchestrator.providers.errors import ProviderNotConfigured
from apps.orchestrator.providers.registry import build_review_provider
from apps.orchestrator.providers.review import (
    ModelReviewProvider,
    ReviewCall,
    ReviewProvider,
    ReviewRequest,
)


def package() -> ReviewPackage:
    return ReviewPackage(
        external_task_id="TS-004",
        attempt=1,
        cycle=1,
        task_specification="Task: TS-004",
        starting_commit="abc123",
        diff_text="+ something",
    )


class StaticAnalyser:
    """A reviewer that is not a language model, to prove the boundary holds."""

    config = ProviderConfig(
        provider_id="static-analyser",
        base_url="local",
        model_name="none",
        role=ModelRole.REVIEWER,
    )

    async def review(self, request: ReviewRequest) -> ReviewCall:
        return ReviewCall(
            result=ReviewResult(
                decision=ReviewDecision.APPROVED,
                summary="no findings",
                provider_id=self.config.provider_id,
            )
        )

    async def check_connection(self) -> ConnectionReport:
        return ConnectionReport(provider_id=self.config.provider_id, reachable=True)

    async def aclose(self) -> None:
        return None


def test_a_non_model_reviewer_satisfies_the_contract():
    assert isinstance(StaticAnalyser(), ReviewProvider)


@pytest.mark.asyncio
async def test_a_non_model_reviewer_returns_a_usable_result():
    """Its raw fields are empty and nothing downstream needs them."""
    call = await StaticAnalyser().review(ReviewRequest(package=package()))

    assert call.result.decision is ReviewDecision.APPROVED
    assert call.prompt_text == ""
    assert call.raw_response == ""


def test_the_registry_builds_a_reviewer_from_the_environment():
    settings = Settings(
        _env_file=None,
        review_base_url="http://reviewer.local/v1",
        review_model="reviewer-xl",
    )
    provider = build_review_provider(settings=settings)

    assert isinstance(provider, ModelReviewProvider)
    assert isinstance(provider, ReviewProvider)
    assert provider.config.model_name == "reviewer-xl"
    assert provider.is_reviewer_role


def test_an_unconfigured_reviewer_raises_rather_than_substituting_the_coder():
    """Principle 10: a provider change must be explicit in configuration."""
    with pytest.raises(ProviderNotConfigured):
        build_review_provider(settings=Settings(_env_file=None, review_base_url=""))


def test_the_prompt_lives_in_agents_and_not_in_the_provider():
    """``providers`` is a boundary; a reviewer's instructions are injected."""
    provider = ModelReviewProvider(
        StaticAnalyser(),
        system_prompt=REVIEWER_SYSTEM_PROMPT,
        instruction_renderer=render_review_instructions,
    )
    request = provider._build_request(ReviewRequest(package=package()))  # noqa: SLF001

    assert "senior reviewer" in request.system_instructions
    assert request.schema is not None
    assert request.schema.name == "code_review"
    # A verdict should not move between two runs over the same diff.
    assert request.temperature == 0.0
