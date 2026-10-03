"""Model configuration and connection testing (build.md phase E, items 3-4).

Providers reach the orchestrator from two places: the environment, which
carries the default local coder and the reviewer so a fresh installation can
run without a database row, and the ``models`` table, which an operator uses
to register additional endpoints. This service is the one place that merges
the two, so every caller sees the same list in the same order.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.settings import Settings, get_settings
from ..domain.enums import Complexity, ModelRole
from ..domain.model_policy import ModelPolicy
from ..domain.models import Model
from ..providers import (
    ConnectionReport,
    ProviderConfig,
    ProviderNotConfigured,
    build_provider,
    config_from_model,
    configs_from_settings,
    select_for_role,
)
from ..repositories import ModelRepository
from .errors import EntityConflict, EntityNotFound

_FORBIDDEN_METADATA_CREDENTIAL_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "apiKey",
        "authorization",
        "bearer_token",
        "access_token",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class ConfiguredProvider:
    """A provider plus where it was configured, for the API and for logs."""

    config: ProviderConfig
    #: ``"environment"`` or ``"database"``. An operator debugging a wrong
    #: endpoint needs to know which file to edit.
    source: str
    model_id: UUID | None = None


def list_providers(
    session: Session, settings: Settings | None = None
) -> list[ConfiguredProvider]:
    """Every configured provider: registered models first, then the environment.

    Registered rows come first because they are the explicit choice; the
    environment defaults are the fallback an installation starts with. Order
    matters: ``select_for_role`` takes the first match for a role.
    """
    settings = settings or get_settings()
    providers = [
        ConfiguredProvider(config_from_model(model), source="database", model_id=model.id)
        for model in ModelRepository(session).list()
    ]
    providers.extend(
        ConfiguredProvider(config, source="environment")
        for config in configs_from_settings(settings)
    )
    return providers


def resolve_for_role(
    session: Session,
    role: ModelRole,
    *,
    preferred_model: str | None = None,
    settings: Settings | None = None,
) -> ProviderConfig:
    """The provider a run should use for ``role``.

    Raises:
        ProviderNotConfigured: nothing enabled matches. There is no fallback
            to a provider registered for another role or another model name
            (principle 10).
    """
    configs = [provider.config for provider in list_providers(session, settings)]
    return select_for_role(role, configs, preferred_model=preferred_model)


@dataclass(frozen=True, slots=True)
class RoleSelection:
    """The providers one task's three roles resolve to.

    ``planner`` is ``None`` when no PLANNER is registered, which is the normal
    case: the planning turns then run on ``coder``. Keeping it ``None`` rather
    than copying ``coder`` in is what lets the caller tell a planner it owns a
    transport for from one that shares the coder's (see
    ``WorkflowRunner.aclose``).
    """

    coder: ProviderConfig
    planner: ProviderConfig | None
    reviewer: ProviderConfig


def resolve_roles(
    session: Session,
    policy: ModelPolicy,
    complexity: Complexity,
    *,
    settings: Settings | None = None,
) -> RoleSelection:
    """Resolve every role a task needs under the project's ``policy``.

    The one place role resolution happens, so a task's coder and the planner
    that falls back to it cannot be chosen by different rules.

    Raises:
        ProviderNotConfigured: a role has no enabled provider, or the policy
            named a model that is not registered for the role it was named
            for. Explicit configuration fails closed: a declared model is
            never substituted for another one (principle 10).
    """
    coder = resolve_for_role(
        session,
        ModelRole.CODER,
        preferred_model=policy.coder_for(complexity),
        settings=settings,
    )
    try:
        # No manifest field selects a planner, so there is no preference to
        # pass: a separately registered PLANNER is used, and otherwise the
        # caller runs planning on the coder resolved above.
        planner: ProviderConfig | None = resolve_for_role(
            session, ModelRole.PLANNER, settings=settings
        )
    except ProviderNotConfigured:
        planner = None
    reviewer = resolve_for_role(
        session,
        ModelRole.REVIEWER,
        preferred_model=policy.reviewer,
        settings=settings,
    )
    return RoleSelection(coder=coder, planner=planner, reviewer=reviewer)


def register_model(
    session: Session,
    *,
    provider: str,
    model_name: str,
    role: ModelRole,
    endpoint: str,
    external_model_id: str | None = None,
    timeout_seconds: int = 600,
    context_window: int | None = None,
    enabled: bool = True,
    metadata: dict[str, object] | None = None,
) -> Model:
    """Register an endpoint the orchestrator may call.

    ``metadata`` may carry ``api_key_env`` (the *name* of an environment
    variable), ``extra_body`` and ``api_mode`` (``"chat_completions"``, the
    default, or ``"responses"`` for OpenAI models not served on
    ``/chat/completions``). A credential itself is never accepted here, so it
    cannot reach the database or a run artifact (section 36).

    Raises:
        EntityConflict: this provider/model/role is already registered.
    """
    safe_metadata = _validated_metadata(metadata or {})
    repository = ModelRepository(session)
    existing = [
        candidate
        for candidate in repository.list(role=role)
        if candidate.provider == provider and candidate.model_name == model_name
    ]
    if existing:
        raise EntityConflict(
            f"Model '{model_name}' from provider '{provider}' is already "
            f"registered for role '{role.value}'"
        )
    model = Model(
        provider=provider,
        model_name=model_name,
        role=role,
        endpoint=endpoint,
        external_model_id=external_model_id,
        timeout_seconds=timeout_seconds,
        context_window=context_window,
        enabled=enabled,
        metadata=safe_metadata,
    )
    return repository.add(model)


def _validated_metadata(metadata: dict[str, object]) -> dict[str, object]:
    forbidden = sorted(
        key for key in metadata if key in _FORBIDDEN_METADATA_CREDENTIAL_KEYS
    )
    if forbidden:
        raise EntityConflict(
            "Model metadata must not contain raw credentials; use api_key_env "
            f"instead of: {', '.join(forbidden)}"
        )
    return dict(metadata)


def get_model(session: Session, model_id: UUID) -> Model:
    """Raises:
    EntityNotFound: no such model.
    """
    model = ModelRepository(session).get(model_id)
    if model is None:
        raise EntityNotFound("Model", model_id)
    return model


async def check_provider(config: ProviderConfig) -> ConnectionReport:
    """Probe one endpoint (phase E item 4).

    Returns a report rather than raising: a failing probe is information an
    operator asked for, not a failed request.
    """
    if not config.enabled:
        return ConnectionReport(
            provider_id=config.provider_id, reachable=False, detail="Provider is disabled"
        )
    provider = build_provider(config)
    try:
        return await provider.check_connection()
    finally:
        await provider.aclose()


async def check_all(configs: list[ProviderConfig]) -> list[ConnectionReport]:
    """Probe every provider concurrently, preserving input order."""
    if not configs:
        return []
    return list(await asyncio.gather(*(check_provider(config) for config in configs)))


def config_for_provider_id(
    providers: list[ConfiguredProvider], provider_id: str
) -> ProviderConfig:
    """Find a configured provider by its id.

    Raises:
        ProviderNotConfigured: no provider carries that id.
    """
    for provider in providers:
        if provider.config.provider_id == provider_id:
            return provider.config
    raise ProviderNotConfigured(f"No configured provider with id '{provider_id}'")
