"""Sidecar startup warmup — pay cold-start costs before the first /ask."""

from __future__ import annotations

import logging
import os
import time

from context_engine.api.state import SidecarState
from context_engine.index_profile import effective_index_workspace_id
from context_engine.workspace import DEFAULT_WORKSPACE_ID

logger = logging.getLogger(__name__)


def _warmup_enabled() -> bool:
    return os.getenv("SIDECAR_WARMUP_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def warm_sidecar(state: SidecarState) -> None:
    """Preload Lance, embeddings, and Neo4j so the first ask avoids cold start."""
    if not _warmup_enabled():
        logger.info("Sidecar warmup skipped (SIDECAR_WARMUP_ENABLED=false)")
        return

    started = time.monotonic()
    index_workspace_id = effective_index_workspace_id(DEFAULT_WORKSPACE_ID)
    stages: dict[str, float] = {}

    stage_started = time.monotonic()
    try:
        state.vector_db.warmup(workspace_id=index_workspace_id)
        stages["lance_default"] = round((time.monotonic() - stage_started) * 1000, 2)
    except Exception:
        logger.exception("Sidecar warmup: default Lance client failed")
        stages["lance_default"] = -1.0

    axis_lance = state.ask_context_builder.lance_for_index_workspace(index_workspace_id)
    if axis_lance is not state.vector_db:
        stage_started = time.monotonic()
        try:
            axis_lance.warmup(workspace_id=index_workspace_id)
            stages["lance_axis"] = round((time.monotonic() - stage_started) * 1000, 2)
        except Exception:
            logger.exception("Sidecar warmup: axis Lance client failed")
            stages["lance_axis"] = -1.0

    stage_started = time.monotonic()
    try:
        from context_engine.database.provider import get_database_provider

        client = get_database_provider().client_for()
        client.health_check()
        stages["neo4j"] = round((time.monotonic() - stage_started) * 1000, 2)
    except Exception:
        logger.exception("Sidecar warmup: Neo4j health check failed")
        stages["neo4j"] = -1.0

    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
    logger.info(
        "Sidecar warmup complete in %.2fms (workspace=%s stages=%s)",
        elapsed_ms,
        index_workspace_id,
        stages,
    )
