"""Structured retrieval decisions for observability and benchmarks.

Schema version must bump when keys or semantics change; see `context_engine/retrieval/trace.py` and [spec_prompt_contract_observability.md](../docs/spec_prompt_contract_observability.md).
"""

from __future__ import annotations

from typing import Any

RETRIEVAL_TRACE_SCHEMA_VERSION = "1"


def unified_trace(
    *,
    workspace_id: str,
    intent: str,
    mechanism: str,
    required_roles: list[str],
    stopped_reason: str,
    target_selection: dict[str, Any],
    budget_info: dict[str, Any],
    ranker_state: dict[str, Any],
    cache_hits: list[str],
    missing_roles: list[str],
    pruned_count: int,
) -> dict[str, Any]:
    """Snapshot for unified ranker path (graph + vector)."""
    return {
        "schema_version": RETRIEVAL_TRACE_SCHEMA_VERSION,
        "strategy": "unified",
        "workspace_id": workspace_id,
        "intent": intent,
        "mechanism": mechanism,
        "required_roles": list(required_roles),
        "stopped_reason": stopped_reason,
        "missing_roles": list(missing_roles),
        "target_selection": dict(target_selection),
        "budget": {
            "limit": budget_info.get("limit"),
            "spent": budget_info.get("spent"),
            "effective_cap": budget_info.get("effective_cap"),
            "floor": budget_info.get("floor"),
            "pool_size": budget_info.get("pool_size"),
            "pruned": budget_info.get("pruned"),
        },
        "ranker_state": dict(ranker_state),
        "cache_hits": list(cache_hits),
        "pruned_count": pruned_count,
    }


def graph_only_trace(
    *,
    workspace_id: str,
    intent: str,
    stopped_reason: str,
    cache_hits: list[str],
    ranker_state: dict[str, Any],
    pruned_count: int,
) -> dict[str, Any]:
    """Snapshot for legacy graph-only expander path."""
    return {
        "schema_version": RETRIEVAL_TRACE_SCHEMA_VERSION,
        "strategy": "graph_only",
        "workspace_id": workspace_id,
        "intent": intent,
        "stopped_reason": stopped_reason,
        "ranker_state": dict(ranker_state),
        "cache_hits": list(cache_hits),
        "pruned_count": pruned_count,
    }
