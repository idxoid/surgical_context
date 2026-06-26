"""Retrieval feedback routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from context_engine.api.routes.deps import AuthHeader, UserIdHeader, WorkspaceHeader, require_main
from context_engine.api.schemas import FeedbackRequest, FeedbackResponse
from context_engine.feedback import FeedbackEvent

router = APIRouter(tags=["feedback"])


@router.post(
    "/feedback",
    response_model=FeedbackResponse,
    responses={
        400: {"description": "Invalid feedback event (e.g. duplicate or malformed payload)"},
        403: {
            "description": ("Feedback token belongs to another workspace or user"),
        },
        404: {"description": "Unknown feedback token"},
    },
)
def record_feedback(
    req: FeedbackRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    """Record retrieval feedback against an issued feedback token."""
    main = require_main(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace_id = main._resolve_workspace(x_workspace, authorization)
    snapshot = main.feedback_store.get_snapshot(req.feedback_token)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Unknown feedback token")
    if snapshot.workspace_id != workspace_id:
        raise HTTPException(status_code=403, detail="Feedback token belongs to another workspace")
    if snapshot.user_id != user_id:
        raise HTTPException(status_code=403, detail="Feedback token belongs to another user")

    event = FeedbackEvent(
        feedback_token=req.feedback_token,
        kind=req.kind,
        workspace_id=workspace_id,
        user_id=user_id,
        trace_id=snapshot.trace_id,
        details=req.details,
        client_timestamp=req.timestamp,
    )
    try:
        main.feedback_store.record_feedback(event)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    main.default_metrics.increment(
        "context_engine_feedback_events_total",
        labels={"kind": event.kind, "outcome": event.outcome, "workspace": workspace_id},
    )
    return {
        "status": "recorded",
        "feedback_token": req.feedback_token,
        "kind": event.kind,
        "outcome": event.outcome,
        "workspace_id": workspace_id,
        "trace_id": snapshot.trace_id,
    }
