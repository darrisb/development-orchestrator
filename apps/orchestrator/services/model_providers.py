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
from ..domain.enums import ModelRole
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
    variable) and ``extra_body``. A credential itself is never accepted here,
    so it cannot reach the database or a run artifact (section 36).

    Raises:
        EntityConflict: this provider/model/role is already registered.
    """
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
        metadata=metadata or {},
    )
    return repository.add(model)


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
