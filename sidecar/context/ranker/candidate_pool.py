"""Core ranker datatypes shared across UnifiedRanker facade and ranker submodules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from sidecar.workspace import DEFAULT_WORKSPACE_ID

ANCHOR_TYPE_WEIGHTS = {
    "definition": 1.0,
    "warning": 0.95,
    "deprecated": 0.85,
    "reference": 0.65,
    "example": 0.45,
}


@dataclass
class RankerWeights:
    alpha: float = 1.0  # graph structural score
    beta: float = 0.8  # semantic similarity score
    gamma: float = 0.4  # intent tier prior
    delta: float = 0.5  # overlap bonus (both signals fired)
    epsilon: float = 0.3  # token cost penalty per 100 tokens


DEFAULT_WEIGHTS = RankerWeights()


@dataclass
class Candidate:
    kind: str  # "symbol" | "doc"
    uid: str  # symbol UID or doc chunk_id
    token_cost: int
    graph_score: float = 0.0
    semantic_score: float = 0.0
    intent_weight: float = 0.0
    noise_factor: float = 1.0  # multiplicative downrank for tests/tutorials
    provenance: list[str] = field(default_factory=list)
    name: str = ""
    symbol_kind: str = ""
    qualified_name: str = ""
    file_path: str = ""
    range: list[int] = field(default_factory=lambda: [0, 0])
    render_mode: str = "full"
    evidence_role: str = ""
    supporting_roles: list[str] = field(default_factory=list)
    relation: str = ""
    direction: str = ""
    depth: int = 0
    file_hash: str = ""
    content: str = ""
    anchor_type: str = ""
    anchor_confidence: float = 0.0
    primary_bias: float = 0.0

    @property
    def overlap(self) -> bool:
        return self.graph_score > 0 and self.semantic_score > 0


def anchor_edge_quality(
    anchor_type: str,
    confidence: float,
    primary_bias: float,
) -> float:
    """Normalize DocAnchor edge properties into a [0, 1] quality score."""
    type_weight = ANCHOR_TYPE_WEIGHTS.get(anchor_type or "reference", 0.65)
    return max(0.05, min(1.0, type_weight * confidence * primary_bias))


class VectorSearcher:
    """Thin wrapper around LanceDB for use by UnifiedRanker."""

    def __init__(self, lancedb_client):
        self.db = lancedb_client

    def search_docs(
        self,
        query: str,
        limit: int = 30,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict]:
        try:
            raw = self.db.search(query, limit, workspace_id=workspace_id)
        except TypeError:
            raw = self.db.search(query, limit)
        return cast(
            list[dict[str, Any]],
            [
                {
                    "chunk_id": r.get("id", f"{r['file_path']}::chunk"),
                    "file_path": r["file_path"],
                    "content": r["chunk"],
                    "score": float(r.get("score") or 0.0),
                }
                for r in raw
            ],
        )

    def search_symbols(
        self,
        query: str,
        limit: int = 30,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict]:
        try:
            return cast(
                list[dict[str, Any]],
                self.db.search_symbols(
                    query, limit=limit, threshold=1.0, workspace_id=workspace_id
                ),
            )
        except TypeError:
            return cast(
                list[dict[str, Any]],
                self.db.search_symbols(query, limit=limit, threshold=1.0),
            )
