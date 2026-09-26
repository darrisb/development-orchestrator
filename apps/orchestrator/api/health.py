from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from ..config import Settings, get_settings
from ..schemas.health import HealthResponse
from ..services.health import build_health_report

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(response: Response, settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Liveness plus readiness for PostgreSQL and the artifact store."""
    report = build_health_report(settings)
    if report.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report
