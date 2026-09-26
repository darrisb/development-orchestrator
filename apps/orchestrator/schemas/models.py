"""Model-provider schemas (build.md section 39: typed request/response).

No response here carries an API key, and none carries an endpoint's raw error
body. What an operator needs to diagnose a misconfiguration is the endpoint,
the model name and whether a credential was present at all.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from ..domain.enums import ModelRole
from ..providers import ConnectionReport
from ..services.model_providers import ConfiguredProvider


class ModelRegisterRequest(BaseModel):
    provider: str = Field(default="openai_compatible")
    model_name: str
    role: ModelRole
    endpoint: str
    #: The name the endpoint knows, when it differs from ``model_name``.
    external_model_id: str | None = None
    timeout_seconds: int = Field(default=600, ge=1)
    context_window: int | None = Field(default=None, ge=1)
    enabled: bool = True
    #: May carry ``api_key_env`` (a variable *name*) and ``extra_body``.
    #: A credential value is never accepted here.
    metadata: dict[str, object] = Field(default_factory=dict)


class ProviderResponse(BaseModel):
    provider_id: str
    model_name: str
    role: ModelRole
    endpoint: str
    context_window: int | None
    enabled: bool
    authenticated: bool
    source: str
    model_id: UUID | None

    @classmethod
    def from_configured(cls, provider: ConfiguredProvider) -> ProviderResponse:
        config = provider.config
        return cls(
            provider_id=config.provider_id,
            model_name=config.model_name,
            role=config.role,
            endpoint=config.base_url,
            context_window=config.context_window,
            enabled=config.enabled,
            authenticated=bool(config.api_key),
            source=provider.source,
            model_id=provider.model_id,
        )


class ConnectionReportResponse(BaseModel):
    provider_id: str
    reachable: bool
    #: ``None`` when the endpoint lists no models, which some single-model
    #: servers do; that is not evidence the model is missing.
    model_available: bool | None
    healthy: bool
    detail: str | None
    latency_ms: int | None
    available_models: list[str]

    @classmethod
    def from_domain(cls, report: ConnectionReport) -> ConnectionReportResponse:
        return cls(
            provider_id=report.provider_id,
            reachable=report.reachable,
            model_available=report.model_available,
            healthy=report.healthy,
            detail=report.detail,
            latency_ms=report.latency_ms,
            available_models=list(report.available_models),
        )
