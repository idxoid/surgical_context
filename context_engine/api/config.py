"""Sidecar runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from context_engine.history import parse_retention_days


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default).lower())
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SidecarConfig:
    model_preference: str
    allow_cloud_llm: bool
    auth_required: bool
    trust_client_user_header: bool
    trust_client_workspace_header: bool
    index_queue_max_pending: int
    index_queue_debounce_ms: int
    index_queue_batch_size: int
    history_mode: str
    history_db_path: str
    history_retention_days: int | None


def load_context_engine_config() -> SidecarConfig:
    return SidecarConfig(
        model_preference=os.getenv("MODEL_PREFERENCE", "ollama"),
        allow_cloud_llm=_env_bool("ALLOW_CLOUD_LLM", default=False),
        auth_required=_env_bool("AUTH_REQUIRED", default=False),
        trust_client_user_header=_env_bool("TRUST_CLIENT_USER_HEADER", default=False),
        trust_client_workspace_header=_env_bool("TRUST_CLIENT_WORKSPACE_HEADER", default=False),
        index_queue_max_pending=int(os.getenv("INDEX_QUEUE_MAX_PENDING", "500")),
        index_queue_debounce_ms=int(os.getenv("INDEX_QUEUE_DEBOUNCE_MS", "500")),
        index_queue_batch_size=int(os.getenv("INDEX_QUEUE_BATCH_SIZE", "50")),
        history_mode=os.getenv("HISTORY_MODE", "local"),
        history_db_path=os.getenv("HISTORY_DB_PATH", "./data/history/surgical_context.sqlite3"),
        history_retention_days=parse_retention_days(os.getenv("HISTORY_RETENTION_DAYS", "")),
    )
