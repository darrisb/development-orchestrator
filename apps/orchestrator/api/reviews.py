from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..schemas.reviews import (
    AuthorizeHumanResolutionRequest,
    EscalationResponse,
    HumanResolutionWorkspaceResponse,
    PrepareHumanResolutionRequest,
    ReconcileHumanCommitRequest,
    ResolveEscalationRequest,
    ReviewResponse,
)
from ..services import human_resolution as resolution_service
from ..services import reviews as review_service

router = APIRouter(tags=["reviews"])


@router.get("/runs/{run_id}/reviews", response_model=list[ReviewResponse])
def list_run_reviews(run_id: UUID, session: Session = Depends(get_db)) -> list[ReviewResponse]:
    return [
        ReviewResponse.from_domain(review)
        for review in review_service.list_reviews_for_run(session, run_id)
    ]


@router.get("/escalations", response_model=list[EscalationResponse])
def list_escalations(
    task_id: UUID | None = None, session: Session = Depends(get_db)
) -> list[EscalationResponse]:
    """Open escalations, so a human can find the decisions waiting on them."""
    return [
        EscalationResponse.from_domain(escalation)
        for escalation in review_service.list_open_escalations(session, task_id=task_id)
    ]


@router.post("/escalations/{escalation_id}/resolve", response_model=EscalationResponse)
def resolve_escalation(
    escalation_id: UUID,
    payload: ResolveEscalationRequest,
    session: Session = Depends(get_db),
) -> EscalationResponse:
    return EscalationResponse.from_domain(
        review_service.resolve_escalation(
            session,
            escalation_id,
            resolution=payload.resolution,
            status=payload.status,
            option_key=payload.option_key,
            human_commit=payload.human_commit,
        )
    )


@router.post(
    "/escalations/{escalation_id}/reconcile-human-commit",
    response_model=EscalationResponse,
)
def reconcile_human_commit(
    escalation_id: UUID,
    payload: ReconcileHumanCommitRequest,
    session: Session = Depends(get_db),
) -> EscalationResponse:
    """Reconcile a historical COMPLETED_BY_HAND escalation with a human commit.

    For escalations resolved before concern 73, where the human commit was not
    recorded or integrated. This endpoint integrates the commit through the
    canonical mechanism and updates the escalation record.
    """
    return EscalationResponse.from_domain(
        review_service.reconcile_human_commit(
            session,
            escalation_id,
            human_commit=payload.human_commit,
        )
    )


@router.post(
    "/escalations/{escalation_id}/human-integration-resolution/workspace",
    response_model=HumanResolutionWorkspaceResponse,
)
def prepare_human_resolution_workspace(
    escalation_id: UUID,
    payload: PrepareHumanResolutionRequest,
    session: Session = Depends(get_db),
) -> HumanResolutionWorkspaceResponse:
    """Create the workspace a merge conflict gets resolved in.

    The supported answer to "the canonical merge of this human commit
    conflicts". Returns a detached worktree at the stated baseline, the paths
    that may be edited, and the human commit's own parent so the original patch
    can be read. Nothing is guessed and no ref moves: the human source commit is
    read-only throughout.
    """
    return HumanResolutionWorkspaceResponse.from_workspace(
        resolution_service.prepare_resolution_workspace(
            session,
            escalation_id,
            human_source_commit=payload.human_source_commit,
            expected_integration_sha=payload.expected_integration_sha,
        )
    )


@router.post(
    "/escalations/{escalation_id}/human-integration-resolution",
    response_model=EscalationResponse,
)
def authorize_human_resolution(
    escalation_id: UUID,
    payload: AuthorizeHumanResolutionRequest,
    session: Session = Depends(get_db),
) -> EscalationResponse:
    """Validate the operator's resolution and advance the baseline onto it.

    Creates the resolution commit itself -- a merge whose second parent is the
    historical human commit, so that commit really is in the baseline
    afterwards -- only after the content invariant and the project's own
    build/lint/tests pass over the resolved tree. Records both provenance
    values on the escalation and refuses to replay.
    """
    return EscalationResponse.from_domain(
        resolution_service.authorize_human_resolution(
            session,
            escalation_id,
            human_source_commit=payload.human_source_commit,
            expected_integration_sha=payload.expected_integration_sha,
        )
    )
