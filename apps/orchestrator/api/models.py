"""Model provider endpoints (build.md section 39).

``GET /models`` answers "what would this installation actually call?", and the
connection test answers "would it answer?" -- deliberately separate, because
the second one touches the network and the first must not.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from ..config.settings import Settings, get_settings
from ..db.session import get_db
from ..schemas.models import (
    ConnectionReportResponse,
    ModelRegisterRequest,
    ProviderResponse,
)
from ..services import model_providers as provider_service

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[ProviderResponse])
def list_models(
    session: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> list[ProviderResponse]:
    """Registered models plus the providers configured in the environment."""
    return [
        ProviderResponse.from_configured(provider)
        for provider in provider_service.list_providers(session, settings)
    ]


@router.post("", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
def register_model(
    payload: ModelRegisterRequest, session: Session = Depends(get_db)
) -> ProviderResponse:
    model = provider_service.register_model(
        session,
        provider=payload.provider,
        model_name=payload.model_name,
        role=payload.role,
        endpoint=payload.endpoint,
        external_model_id=payload.external_model_id,
        timeout_seconds=payload.timeout_seconds,
        context_window=payload.context_window,
        enabled=payload.enabled,
        metadata=payload.metadata,
    )
    return ProviderResponse.from_configured(
        provider_service.ConfiguredProvider(
            provider_service.config_from_model(model), source="database", model_id=model.id
        )
    )


@router.post("/connection-test", response_model=list[ConnectionReportResponse])
async def connection_test(
    provider_id: str | None = Query(
        default=None, description="Test one provider; omit to test all configured ones."
    ),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[ConnectionReportResponse]:
    """Probe configured endpoints without running inference (phase E item 4).

    Raises:
        ProviderNotConfigured: ``provider_id`` matches nothing configured.
            Mapped to 503 so a probe against a missing provider is never
            mistaken for a probe that succeeded.
    """
    providers = provider_service.list_providers(session, settings)
    configs = (
        [provider_service.config_for_provider_id(providers, provider_id)]
        if provider_id is not None
        else [provider.config for provider in providers]
    )
    reports = await provider_service.check_all(configs)
    return [ConnectionReportResponse.from_domain(report) for report in reports]
