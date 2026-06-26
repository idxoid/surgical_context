"""Local dialog history routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from context_engine.api.routes.deps import (
    AuthHeader,
    UserIdHeader,
    WorkspaceHeader,
    require_main,
)
from context_engine.api.schemas import (
    HistoryAskRecordRequest,
    HistoryAskRecordResponse,
    HistoryConversationResponse,
    HistoryConversationsResponse,
    HistoryRequestBundleResponse,
)
from context_engine.history import hash_history_text

router = APIRouter(tags=["history"])


def _history_ask_title(req: HistoryAskRecordRequest) -> str:
    if req.prompt_summary:
        return req.prompt_summary
    if req.symbol:
        return f"Ask about {req.symbol}"
    return "Workspace ask"


def _history_ask_metadata(req: HistoryAskRecordRequest) -> dict:
    return {
        "source": "extension",
        "symbol": req.symbol,
        **req.metadata,
    }


def _surface_snapshot_payload(
    snapshot: dict,
    *,
    req: HistoryAskRecordRequest,
    workspace_id: str,
    user_id: str,
) -> dict:
    return {
        **snapshot,
        "request_id": req.request_id,
        "trace_id": req.trace_id,
        "feedback_token": req.feedback_token,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "symbol": req.symbol,
    }


def _ensure_history_conversation_id(
    main,
    req: HistoryAskRecordRequest,
    *,
    workspace_id: str,
    user_id: str,
) -> str:
    conversation_id = req.conversation_id
    title = _history_ask_title(req)
    metadata = _history_ask_metadata(req)
    create_kwargs = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "title": title,
        "selected_request_id": req.request_id,
        "metadata": metadata,
    }

    if not conversation_id:
        return main.history_provider.create_conversation(**create_kwargs)

    conversation = main.history_provider.get_conversation(conversation_id)
    if conversation:
        main._history_conversation_for_scope(
            conversation_id,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        return conversation_id

    return main.history_provider.create_conversation(
        conversation_id=conversation_id,
        **create_kwargs,
    )


def _save_optional_surface_snapshots(
    main,
    req: HistoryAskRecordRequest,
    assistant_message_id: str,
    *,
    workspace_id: str,
    user_id: str,
) -> None:
    snapshot_kwargs = {
        "req": req,
        "workspace_id": workspace_id,
        "user_id": user_id,
    }
    if req.inspector_snapshot:
        main.history_provider.save_inspector_snapshot(
            assistant_message_id,
            _surface_snapshot_payload(req.inspector_snapshot, **snapshot_kwargs),
        )
    if req.impact_snapshot:
        main.history_provider.save_impact_snapshot(
            assistant_message_id,
            _surface_snapshot_payload(req.impact_snapshot, **snapshot_kwargs),
        )


@router.post(
    "/history/ask",
    response_model=HistoryAskRecordResponse,
    responses={
        400: {"description": "request_id is required"},
    },
)
def record_history_ask(
    req: HistoryAskRecordRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    """Persist a sanitized ask/request snapshot for local dialog history."""
    main = require_main(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace_id = main._resolve_workspace(x_workspace, authorization)
    if not main._history_enabled():
        return {
            "status": "disabled",
            "conversation_id": req.conversation_id or "",
            "user_message_id": "",
            "assistant_message_id": "",
            "selected_request_id": req.request_id,
        }
    if not req.request_id.strip():
        raise HTTPException(status_code=400, detail="request_id is required")

    conversation_id = _ensure_history_conversation_id(
        main,
        req,
        workspace_id=workspace_id,
        user_id=user_id,
    )

    prompt_hash = req.prompt_hash or hash_history_text(req.prompt_summary)
    answer_hash = req.answer_hash or hash_history_text(req.answer_summary)
    user_message_id = main.history_provider.append_message(
        conversation_id=conversation_id,
        role="user",
        request_id=req.request_id,
        content_summary=req.prompt_summary,
        content_hash=prompt_hash,
        symbol=req.symbol,
        trace_id=req.trace_id,
        metadata={
            "source": "extension",
            "kind": "prompt",
        },
    )
    assistant_message_id = main.history_provider.append_message(
        conversation_id=conversation_id,
        role="assistant",
        request_id=req.request_id,
        content_summary=req.answer_summary,
        content_hash=answer_hash,
        symbol=req.symbol,
        trace_id=req.trace_id,
        feedback_token=req.feedback_token,
        metadata={
            "source": "extension",
            "kind": "answer",
            "has_feedback_token": bool(req.feedback_token),
        },
    )

    main.history_provider.save_ask_snapshot(
        assistant_message_id,
        main._history_snapshot(
            req,
            workspace_id=workspace_id,
            user_id=user_id,
            answer_summary=req.answer_summary,
        ),
    )
    _save_optional_surface_snapshots(
        main,
        req,
        assistant_message_id,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    main.history_provider.set_selected_request(conversation_id, req.request_id)

    return {
        "status": "recorded",
        "conversation_id": conversation_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "selected_request_id": req.request_id,
    }


@router.get("/history/conversations", response_model=HistoryConversationsResponse)
def history_conversations(
    limit: int = 30,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    """List local history conversations for the current workspace and user."""
    main = require_main(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace_id = main._resolve_workspace(x_workspace, authorization)
    if not main._history_enabled():
        return {"conversations": []}
    return {
        "conversations": main.history_provider.list_conversations(
            workspace_id=workspace_id,
            user_id=user_id,
            limit=limit,
        )
    }


@router.get(
    "/history/conversations/{conversation_id}",
    response_model=HistoryConversationResponse,
    responses={
        404: {"description": "Unknown history conversation"},
    },
)
def history_conversation(
    conversation_id: str,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    """Return a sanitized conversation bundle with messages and snapshots."""
    main = require_main(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace_id = main._resolve_workspace(x_workspace, authorization)
    main._history_conversation_for_scope(
        conversation_id, workspace_id=workspace_id, user_id=user_id
    )
    bundle = main.history_provider.get_conversation_bundle(conversation_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Unknown history conversation")
    return bundle


@router.get(
    "/history/conversations/{conversation_id}/requests/{request_id}",
    response_model=HistoryRequestBundleResponse,
    responses={
        404: {"description": "Unknown history request"},
    },
)
def history_request_bundle(
    conversation_id: str,
    request_id: str,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    """Return the snapshots for a selected request in a conversation."""
    main = require_main(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace_id = main._resolve_workspace(x_workspace, authorization)
    main._history_conversation_for_scope(
        conversation_id, workspace_id=workspace_id, user_id=user_id
    )
    bundle = main.history_provider.get_request_bundle(conversation_id, request_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Unknown history request")
    return bundle
