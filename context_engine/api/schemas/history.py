"""History request/response models."""

from typing import Any

from pydantic import BaseModel, Field


class HistoryAskRecordRequest(BaseModel):
    conversation_id: str | None = None
    request_id: str
    prompt_summary: str = ""
    prompt_hash: str = ""
    answer_summary: str = ""
    answer_hash: str = ""
    symbol: str = ""
    trace_id: str = ""
    feedback_token: str = ""
    ask_snapshot: dict[str, Any] = Field(default_factory=dict)
    inspector_snapshot: dict[str, Any] = Field(default_factory=dict)
    impact_snapshot: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HistoryAskRecordResponse(BaseModel):
    status: str
    conversation_id: str
    user_message_id: str
    assistant_message_id: str
    selected_request_id: str


class HistoryConversationsResponse(BaseModel):
    conversations: list[dict[str, Any]]


class HistoryConversationResponse(BaseModel):
    conversation: dict[str, Any]
    messages: list[dict[str, Any]]


class HistoryRequestBundleResponse(BaseModel):
    message: dict[str, Any]
    ask_snapshot: dict[str, Any] | None
    inspector_snapshot: dict[str, Any] | None
    impact_snapshot: dict[str, Any] | None
