"""Provider construction and selection (build.md sections 13 and 31).

Two rules shape this module:

*   **No silent fallback** (principle 10). Selection either returns the
    provider configured for a role or raises ``ProviderNotConfigured``. There
    is no "next best" branch to fall through, which is why a missing local
    coder can never quietly become a cloud one.
*   **Configuration is data.** A provider is built from a ``ProviderConfig``,
    which comes either from the environment (the default local coder and the
    reviewer) or from a ``models`` row registered by an operator. Nothing here
    decides policy; it only resolves what was configured.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

import httpx

from ..config.settings import Settings, get_settings
from ..domain.enums import ModelRole
from ..domain.models import Model
from .base import ApiMode, ModelProvider, ProviderConfig
from .errors import ProviderNotConfigured
from .openai_compatible import OpenAICompatibleProvider
from .review import ModelReviewProvider, ReviewProvider

#: ``provider`` value on a ``models`` row served by the OpenAI-compatible
#: adapter. Adding an adapter means adding a key here, not a branch elsewhere.
OPENAI_COMPATIBLE = "openai_compatible"

_BUILDERS = {OPENAI_COMPATIBLE: OpenAICompatibleProvider}

#: Identifiers for the two providers that come from the environment rather
#: than from a database row, so a fresh installation can run without one.
ENV_CODER_PROVIDER_ID = "env:local-coder"
ENV_REVIEWER_PROVIDER_ID = "env:reviewer"


def build_provider(
    config: ProviderConfig, *, client: httpx.AsyncClient | None = None
) -> ModelProvider:
    """Instantiate the adapter for ``config``.

    ``ProviderConfig`` carries no adapter name because only one adapter
    exists; which adapter serves a registered model is decided in
    ``provider_for_model`` from its ``provider`` column.

    Raises:
        ProviderNotConfigured: the provider is disabled.
    """
    if not config.enabled:
        raise ProviderNotConfigured(f"Provider '{config.provider_id}' is disabled")
    if config.api_key_env and not config.api_key:
        raise ProviderNotConfigured(
            f"Provider '{config.provider_id}' requires API key environment variable "
            f"'{config.api_key_env}', but it is not set"
        )
    return OpenAICompatibleProvider(config, client=client)


def provider_for_model(
    model: Model, *, client: httpx.AsyncClient | None = None
) -> ModelProvider:
    """Build the provider described by a registered ``models`` row.

    Raises:
        ProviderNotConfigured: the row names an unimplemented provider, or is
            disabled.
    """
    if model.provider not in _BUILDERS:
        raise ProviderNotConfigured(
            f"Model '{model.model_name}' names provider '{model.provider}', "
            f"which has no adapter. Implemented: {', '.join(sorted(_BUILDERS))}"
        )
    return build_provider(config_from_model(model), client=client)


def config_from_model(model: Model) -> ProviderConfig:
    """Translate a ``models`` row into provider configuration.

    An API key is read from the row's metadata only as an environment
    variable *name*; a key itself is never stored in the database (section 36).

    Raises:
        ProviderNotConfigured: the row declares an unsupported ``api_mode``.
    """
    metadata: Mapping[str, object] = model.metadata or {}
    extra_body = metadata.get("extra_body")
    return ProviderConfig(
        provider_id=str(model.id),
        base_url=model.endpoint,
        model_name=model.external_model_id or model.model_name,
        role=model.role,
        api_key=_api_key_from_metadata(metadata),
        api_key_env=_api_key_env_from_metadata(metadata),
        timeout_seconds=float(model.timeout_seconds),
        context_window=model.context_window,
        enabled=model.enabled,
        api_mode=_api_mode_from_metadata(metadata, model_name=model.model_name),
        max_output_tokens_parameter=_max_output_tokens_parameter_from_metadata(metadata),
        extra_body=extra_body if isinstance(extra_body, Mapping) else {},
    )


def _api_key_from_metadata(metadata: Mapping[str, object]) -> str | None:
    variable = _api_key_env_from_metadata(metadata)
    if variable:
        return os.environ.get(variable) or None
    return None


def _api_key_env_from_metadata(metadata: Mapping[str, object]) -> str | None:
    variable = metadata.get("api_key_env")
    if isinstance(variable, str) and variable:
        return variable
    return None


def _api_mode_from_metadata(
    metadata: Mapping[str, object], *, model_name: str
) -> ApiMode:
    """Read ``api_mode`` from a row's metadata.

    Absent means ``chat_completions``, which is what every provider
    registered before this option existed is already using. A *present* but
    unrecognised value is a configuration fault and fails closed: guessing a
    transport for a row that asked for a different one is exactly the silent
    substitution principle 10 forbids, and a typo that fell back would route
    the request to an endpoint the operator did not choose. The mode is never
    inferred from the model name.

    Raises:
        ProviderNotConfigured: ``api_mode`` is present and not a supported
            value.
    """
    if "api_mode" not in metadata:
        return ApiMode.CHAT_COMPLETIONS
    mode = metadata["api_mode"]
    try:
        return ApiMode(mode)
    except ValueError as exc:
        supported = ", ".join(member.value for member in ApiMode)
        raise ProviderNotConfigured(
            f"Model '{model_name}' declares api_mode {mode!r}, which is not "
            f"supported. Supported: {supported}"
        ) from exc


def _max_output_tokens_parameter_from_metadata(metadata: Mapping[str, object]) -> str:
    parameter = metadata.get("max_output_tokens_parameter")
    if parameter in {"max_tokens", "max_completion_tokens"}:
        return str(parameter)
    return "max_tokens"


def coder_config_from_settings(settings: Settings | None = None) -> ProviderConfig:
    """The default local coder from the environment (section 48).

    Raises:
        ProviderNotConfigured: no base URL or model name is configured.
    """
    settings = settings or get_settings()
    if not settings.default_local_model_base_url or not settings.default_local_model:
        raise ProviderNotConfigured(
            "No local coder configured: set DEFAULT_LOCAL_MODEL_BASE_URL and "
            "DEFAULT_LOCAL_MODEL"
        )
    return ProviderConfig(
        provider_id=ENV_CODER_PROVIDER_ID,
        base_url=settings.default_local_model_base_url,
        model_name=settings.default_local_model,
        role=ModelRole.CODER,
        timeout_seconds=float(settings.local_model_timeout_seconds),
        context_window=settings.local_model_context_window,
    )


def reviewer_config_from_settings(settings: Settings | None = None) -> ProviderConfig:
    """The reviewer from the environment (section 21).

    Raises:
        ProviderNotConfigured: no reviewer endpoint or model is configured.
    """
    settings = settings or get_settings()
    if not settings.review_base_url or not settings.review_model:
        raise ProviderNotConfigured(
            "No reviewer configured: set REVIEW_BASE_URL and REVIEW_MODEL"
        )
    if settings.review_provider not in _BUILDERS:
        raise ProviderNotConfigured(
            f"REVIEW_PROVIDER='{settings.review_provider}' has no adapter. "
            f"Implemented: {', '.join(sorted(_BUILDERS))}"
        )
    return ProviderConfig(
        provider_id=ENV_REVIEWER_PROVIDER_ID,
        base_url=settings.review_base_url,
        model_name=settings.review_model,
        role=ModelRole.REVIEWER,
        api_key=settings.review_api_key or None,
        timeout_seconds=float(settings.review_timeout_seconds),
    )


def configs_from_settings(settings: Settings | None = None) -> list[ProviderConfig]:
    """Every environment-configured provider, skipping the unconfigured ones.

    Used by ``GET /models`` and by the connection test so an operator can see
    what this installation would actually call.
    """
    settings = settings or get_settings()
    configs: list[ProviderConfig] = []
    for factory in (coder_config_from_settings, reviewer_config_from_settings):
        try:
            configs.append(factory(settings))
        except ProviderNotConfigured:
            continue
    return configs


def select_for_role(
    role: ModelRole,
    configs: Iterable[ProviderConfig],
    *,
    preferred_model: str | None = None,
) -> ProviderConfig:
    """Pick the provider for ``role`` (the routing hook of section 31).

    Args:
        role: the role the caller needs. A provider registered for another
            role is never substituted -- a reviewer is not a coder.
        configs: candidates, typically registered models plus the environment.
        preferred_model: the name a manifest's ``model_policy`` asked for. If
            it is not among the candidates this raises, rather than serving a
            different model than the project declared.

    Raises:
        ProviderNotConfigured: nothing enabled matches. Never falls back.
    """
    candidates = [config for config in configs if config.role is role and config.enabled]
    if not candidates:
        raise ProviderNotConfigured(
            f"No enabled provider is configured for role '{role.value}'"
        )
    if preferred_model is None:
        return candidates[0]
    for config in candidates:
        if preferred_model in (config.model_name, config.provider_id):
            return config
    raise ProviderNotConfigured(
        f"Role '{role.value}' requested model '{preferred_model}', which is not "
        f"among the configured providers: "
        f"{', '.join(config.model_name for config in candidates)}"
    )


def build_review_provider(
    config: ProviderConfig | None = None,
    *,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> ReviewProvider:
    """The configured reviewer, behind the ``ReviewProvider`` contract.

    The prompt is injected here rather than imported by the provider module:
    ``providers`` is a boundary and must not depend on ``agents``, while a
    reviewer with no instructions is not a reviewer. This function is where
    the two meet, and it is the only place that knows both.

    Raises:
        ProviderNotConfigured: no reviewer endpoint or model is configured.
    """
    from ..agents.review_prompts import REVIEWER_SYSTEM_PROMPT, render_review_instructions

    resolved = config or reviewer_config_from_settings(settings)
    return ModelReviewProvider(
        build_provider(resolved, client=client),
        system_prompt=REVIEWER_SYSTEM_PROMPT,
        instruction_renderer=render_review_instructions,
    )
