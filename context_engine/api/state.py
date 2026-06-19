"""Process-wide sidecar service instances."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from context_engine.ai.engine import AIEngine
from context_engine.api.config import SidecarConfig
from context_engine.auth import AuditLog, UserAuth
from context_engine.database.lancedb_client import LanceDBClient
from context_engine.feedback import FeedbackStore
from context_engine.history import build_history_provider
from context_engine.indexer.git_delta_poller import (
    GitDeltaPoller,
    GitDeltaRegistry,
    poll_interval_seconds,
)
from context_engine.indexer.queue import IndexBatchQueue
from context_engine.indexer.service import IndexingService
from context_engine.overlay import InMemoryOverlay
from context_engine.workspace import WorkspaceResolver

logger = logging.getLogger(__name__)


@dataclass
class SidecarState:
    config: SidecarConfig
    overlay: InMemoryOverlay
    vector_db: LanceDBClient
    ai_engine: AIEngine
    user_auth: UserAuth
    audit_log: AuditLog
    workspace_resolver: WorkspaceResolver
    feedback_store: FeedbackStore
    history_provider: Any
    index_queue: IndexBatchQueue
    git_delta_registry: GitDeltaRegistry
    git_delta_poller: GitDeltaPoller
    indexing_service: IndexingService


def build_sidecar_state(config: SidecarConfig) -> SidecarState:
    overlay = InMemoryOverlay()
    vector_db = LanceDBClient()
    ai_engine = AIEngine(
        model_preference=config.model_preference,
        allow_cloud_llm=config.allow_cloud_llm,
    )
    if config.model_preference in {"auto", "claude"} and not config.allow_cloud_llm:
        logger.info(
            "Local-first LLM routing: MODEL_PREFERENCE=%s with ALLOW_CLOUD_LLM=false "
            "— assembled context stays on Ollama even when ANTHROPIC_API_KEY is set.",
            config.model_preference,
        )

    history_provider = build_history_provider(
        mode=config.history_mode,
        db_path=config.history_db_path,
        retention_days=config.history_retention_days,
    )
    git_delta_registry = GitDeltaRegistry()
    indexing_service = IndexingService(
        overlay=overlay,
        vector_db=vector_db,
        config=config,
        git_delta_registry=git_delta_registry,
    )
    index_queue = IndexBatchQueue(
        indexing_service.process_index_batch,
        max_pending=config.index_queue_max_pending,
        debounce_ms=config.index_queue_debounce_ms,
        batch_size=config.index_queue_batch_size,
    )
    indexing_service.attach_queue(index_queue)
    git_delta_poller = GitDeltaPoller(
        git_delta_registry,
        indexing_service.poll_git_delta_target,
        interval_seconds=poll_interval_seconds(),
        auto_start=False,
    )
    return SidecarState(
        config=config,
        overlay=overlay,
        vector_db=vector_db,
        ai_engine=ai_engine,
        user_auth=UserAuth(),
        audit_log=AuditLog(),
        workspace_resolver=WorkspaceResolver(),
        feedback_store=FeedbackStore(),
        history_provider=history_provider,
        index_queue=index_queue,
        git_delta_registry=git_delta_registry,
        git_delta_poller=git_delta_poller,
        indexing_service=indexing_service,
    )
