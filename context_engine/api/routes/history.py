"""Local dialog history routes."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from context_engine.api.routes.deps import require_main
from context_engine.api.schemas import (
    HistoryAskRecordRequest,
    HistoryAskRecordResponse,
    HistoryConversationResponse,
    HistoryConversationsResponse,
    HistoryRequestBundleResponse,
)
from context_engine.history import hash_history_text

router = APIRouter(tags=["history"])


@router.post("/history/ask", response_model=HistoryAskRecordResponse)
def record_history_ask(
    req: HistoryAskRecordRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
    request: Request | None = None,
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

    conversation_id = req.conversation_id
    if conversation_id:
        conversation = main.history_provider.get_conversation(conversation_id)
        if conversation:
            main._history_conversation_for_scope(
                conversation_id,
                workspace_id=workspace_id,
                user_id=user_id,
            )
        else:
            title = req.prompt_summary or (
                f"Ask about {req.symbol}" if req.symbol else "Workspace ask"
            )
            conversation_id = main.history_provider.create_conversation(
                workspace_id=workspace_id,
                user_id=user_id,
                conversation_id=conversation_id,
                title=title,
                selected_request_id=req.request_id,
                metadata={
                    "source": "extension",
                    "symbol": req.symbol,
                    **req.metadata,
                },
            )
    else:
        title = req.prompt_summary or (f"Ask about {req.symbol}" if req.symbol else "Workspace ask")
        conversation_id = main.history_provider.create_conversation(
            workspace_id=workspace_id,
            user_id=user_id,
            title=title,
            selected_request_id=req.request_id,
            metadata={
                "source": "extension",
                "symbol": req.symbol,
                **req.metadata,
            },
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
    if req.inspector_snapshot:
        main.history_provider.save_inspector_snapshot(
            assistant_message_id,
            {
                **req.inspector_snapshot,
                "request_id": req.request_id,
                "trace_id": req.trace_id,
                "feedback_token": req.feedback_token,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "symbol": req.symbol,
            },
        )
    if req.impact_snapshot:
        main.history_provider.save_impact_snapshot(
            assistant_message_id,
            {
                **req.impact_snapshot,
                "request_id": req.request_id,
                "trace_id": req.trace_id,
                "feedback_token": req.feedback_token,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "symbol": req.symbol,
            },
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
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
    request: Request | None = None,
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


@router.get("/history/conversations/{conversation_id}", response_model=HistoryConversationResponse)
def history_conversation(
    conversation_id: str,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
    request: Request | None = None,
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
)
def history_request_bundle(
    conversation_id: str,
    request_id: str,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
    request: Request | None = None,
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
