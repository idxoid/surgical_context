"""Ask and streaming ask routes."""

from __future__ import annotations

import logging
from collections.abc import Generator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from context_engine.api.routes.deps import (
    AuthHeader,
    TraceIdHeader,
    UserIdHeader,
    WorkspaceHeader,
    require_services,
)
from context_engine.api.schemas import (
    AskAxisRequest,
    AskAxisResponse,
    AskRequest,
    AskResponse,
    IntentRequest,
    IntentResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ask"])


@router.post("/ask", response_model=AskResponse)
def ask(
    req: AskRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    x_trace_id: TraceIdHeader = None,
    request: Request = None,
):
    """Ask about a symbol (with multi-user audit logging)."""
    main = require_services(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace_id = main._resolve_workspace(x_workspace, authorization)
    trace = main._start_trace("/ask", x_trace_id, workspace_id)
    status = "ok"
    try:
        with main.db_session(user_id=user_id) as db:
            return main.ask_service.ask(
                req,
                user_id=user_id,
                workspace_id=workspace_id,
                trace=trace,
                db=db,
                resolve_ask_context=main._resolve_ask_context,
            )
    except HTTPException:
        raise
    except Exception:
        status = "error"
        logger.exception("trace_id=%s endpoint=/ask status=error", trace.trace_id)
        raise
    finally:
        main.default_metrics.record_trace(trace, status)


@router.post("/ask/axis", response_model=AskAxisResponse)
def ask_axis(
    req: AskAxisRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    x_trace_id: TraceIdHeader = None,
    request: Request = None,
):
    """Axis-pipeline answer: intent → roles → ranked candidates → context."""
    main = require_services(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    base_workspace_id = main._resolve_workspace(x_workspace, authorization)
    trace = main._start_trace("/ask/axis", x_trace_id, base_workspace_id)
    status = "ok"
    try:
        with main.db_session(user_id=user_id) as db:
            return main.ask_service.ask_axis(
                req,
                user_id=user_id,
                base_workspace_id=base_workspace_id,
                trace=trace,
                db=db,
            )
    except HTTPException:
        raise
    except Exception:
        status = "error"
        logger.exception(
            "trace_id=%s endpoint=/ask/axis status=error",
            trace.trace_id,
        )
        raise
    finally:
        main.default_metrics.record_trace(trace, status)


@router.post("/intent", response_model=IntentResponse)
def intent(
    req: IntentRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    x_trace_id: TraceIdHeader = None,
    request: Request = None,
):
    """Classify-only intent preview: question → ranked role matches.

    Embedding cosine of the question against role descriptions — no graph, no
    retrieval. The cheap path for an editor "intent" panel.
    """
    main = require_services(request)
    main._resolve_request_user(x_user_id, authorization)
    base_workspace_id = main._resolve_workspace(x_workspace, authorization)
    trace = main._start_trace("/intent", x_trace_id, base_workspace_id)
    status = "ok"
    try:
        return main.ask_service.classify_intent(
            req,
            base_workspace_id=base_workspace_id,
            trace=trace,
        )
    except HTTPException:
        raise
    except Exception:
        status = "error"
        logger.exception("trace_id=%s endpoint=/intent status=error", trace.trace_id)
        raise
    finally:
        main.default_metrics.record_trace(trace, status)


@router.post("/ask/stream")
def ask_stream(
    req: AskRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    x_trace_id: TraceIdHeader = None,
    request: Request = None,
):
    """Streaming version of /ask endpoint (SSE)."""
    main = require_services(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace_id = main._resolve_workspace(x_workspace, authorization)
    trace = main._start_trace("/ask/stream", x_trace_id, workspace_id)

    def response_generator() -> Generator[str, None, None]:
        with main.db_session(user_id=user_id) as db:
            yield from main.ask_service.ask_stream(
                req,
                user_id=user_id,
                workspace_id=workspace_id,
                trace=trace,
                db=db,
                resolve_ask_context=main._resolve_ask_context,
            )

    return StreamingResponse(response_generator(), media_type="text/event-stream")
