"""Shared dataclasses for context assembly pipeline."""

from dataclasses import dataclass, field
from typing import Any

RESOLVER_VERSION = "context-arbitrator-v2"


@dataclass
class SymbolContext:
    symbol: str
    file_path: str
    relation: str
    uid: str = ""
    range: list[int] = field(default_factory=list)
    kind: str = ""
    direction: str = "callee"
    depth: int = 0
    relevance_score: float = 0.0
    graph_score: float = 0.0
    semantic_score: float = 0.0
    blended_score: float = 0.0
    intent_weight: float = 0.0
    is_dirty: bool = False
    code: str = ""
    provenance: list[str] = field(default_factory=list)


@dataclass
class DocChunk:
    source_file: str
    chunk_id: str
    content: str
    score: float | None = None
    graph_score: float = 0.0
    semantic_score: float = 0.0
    blended_score: float = 0.0
    intent_weight: float = 0.0
    matched_symbols: list[str] = field(default_factory=list)
    provenance: list[str] = field(default_factory=list)
    anchor_type: str = ""
    anchor_confidence: float = 0.0
    primary_bias: float = 0.0


@dataclass
class PromptContext:
    primary_source: SymbolContext
    graph_context: list[SymbolContext] = field(default_factory=list)
    documentation: list[DocChunk] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    mode: str = "surgical_full"  # "surgical_full" | "surgical_doc_only" | "standard"
    intent: str = ""  # e.g. "navigation", "debugging", "refactor", etc.
    intent_distribution: dict[str, float] = field(default_factory=dict)
    intent_confidence: float = 0.0
    intent_ambiguous: bool = False
    intent_effective_mode: str = ""
    intent_resolution: dict[str, Any] = field(default_factory=dict)
    tier_tokens: dict[str, int] = field(default_factory=dict)  # token counts per tier
    trace_id: str = ""
    stopped_reason: str = ""
    mechanism: str = ""
    pruned_details: list[dict] = field(default_factory=list)
    missing_roles: list[str] = field(default_factory=list)
    workspace_id: str = ""
    resolver_version: str = RESOLVER_VERSION
    stage_timings_ms: dict[str, float] = field(default_factory=dict)
    token_counts: dict[str, int] = field(default_factory=dict)
    model_route: dict[str, Any] = field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    cost_basis: str = "not_configured"
    pruning_reasons: list[str] = field(default_factory=list)
    feedback_token: str = ""
    ranker_state: dict[str, Any] = field(default_factory=dict)

    def to_system_prompt(self) -> str:
        """Render to the flat text format the LLM receives."""
        blocks = [
            f"--- TARGET SYMBOL: {self.primary_source.symbol} ---",
            self.primary_source.code,
        ]
        if self.graph_context:
            blocks.append("\n--- DEPENDENCIES ---")
            for dep in self.graph_context:
                blocks.append(f"\n# From {dep.symbol} [{dep.relation}]:")
                blocks.append(dep.code)
        if self.documentation:
            blocks.append("\n--- DOCUMENTATION ---")
            for doc in self.documentation:
                blocks.append(f"[{doc.source_file}]\n{doc.content}")
        return "\n".join(blocks)

    def to_dict(self) -> dict:
        """Serialize to the JSON Prompt Contract shape."""
        # Calculate tiers_used (which tiers were populated)
        tiers_used: list[str] = []
        if self.primary_source.code:
            tiers_used.append("code")
        if self.graph_context:
            tiers_used.append("cross_refs")
        if self.documentation:
            tiers_used.append("docs")

        pruning_reasons = list(self.pruning_reasons)
        if self.budget.get("dedup_saved", 0) and not pruning_reasons:
            pruning_reasons.append("deduplicated overlapping graph symbols")

        docs_tokens = sum(
            self.tier_tokens.get(tier, 0) for tier in ("specs", "architecture", "concept", "idea")
        )
        ranker_state = self._ranker_metadata()

        return {
            "mode": self.mode,
            "intent": self.intent,
            "intent_details": {
                "primary": self.intent,
                "distribution": self.intent_distribution,
                "ambiguous": self.intent_ambiguous,
                "confidence": self.intent_confidence,
                "effective_mode": self.intent_effective_mode,
                "resolution": self.intent_resolution,
            },
            "metadata": {
                "query_intent": self.intent,
                "effective_intent_mode": self.intent_effective_mode,
                "tiers_used": tiers_used,
                "stopped_reason": self.stopped_reason,
                "missing_roles": self.missing_roles,
                "pruned_count": len(self.pruned_details),
                "tier_tokens": self.tier_tokens,
                "tokens_primary": self.tier_tokens.get("code", 0),
                "tokens_graph": self.tier_tokens.get("cross_refs", 0),
                "tokens_docs": docs_tokens,
                "pruning_reasons": pruning_reasons,
                "ranker": ranker_state,
                "assembly": {
                    "trace_id": self.trace_id,
                    "workspace_id": self.workspace_id,
                    "resolver_version": self.resolver_version,
                    "cache_hits": self.budget.get("cache_hits", []),
                    "feedback_token": self.feedback_token,
                    "stage_timings_ms": self.stage_timings_ms,
                    "token_counts": self.token_counts,
                    "model_route": self.model_route,
                    "estimated_cost_usd": self.estimated_cost_usd,
                    "cost_basis": self.cost_basis,
                },
            },
            "primary_source": self._symbol_to_dict(self.primary_source),
            "graph_context": [self._symbol_to_dict(dep) for dep in self.graph_context],
            "documentation": [
                {
                    "chunk_id": doc.chunk_id,
                    "source_file": doc.source_file,
                    "content": doc.content,
                    "score": doc.score,
                    "scores": {
                        "graph_score": doc.graph_score,
                        "semantic_score": doc.semantic_score or doc.score,
                        "blended_score": doc.blended_score or doc.score,
                        "intent_weight": doc.intent_weight,
                    },
                    "matched_symbols": doc.matched_symbols,
                    "provenance": doc.provenance or ["vector:docs"],
                    "anchor_type": doc.anchor_type,
                    "anchor_confidence": doc.anchor_confidence,
                    "primary_bias": doc.primary_bias,
                    "anchor": {
                        "type": doc.anchor_type,
                        "confidence": doc.anchor_confidence,
                        "primary_bias": doc.primary_bias,
                    },
                }
                for doc in self.documentation
            ],
            "pruned": self._serialize_pruned_candidates(),
            "budget": self.budget,
        }

    def _symbol_to_dict(self, symbol: SymbolContext) -> dict[str, Any]:
        payload = {
            "symbol": symbol.symbol,
            "file_path": symbol.file_path,
            "relation": symbol.relation,
            "direction": symbol.direction,
            "depth": symbol.depth,
            "relevance_score": symbol.relevance_score,
            "scores": {
                "relevance": symbol.relevance_score,
                "graph_score": symbol.graph_score,
                "semantic_score": symbol.semantic_score,
                "blended_score": symbol.blended_score or symbol.relevance_score,
                "intent_weight": symbol.intent_weight,
            },
            "provenance": symbol.provenance or ["graph", "code_resolver"],
            "is_dirty": symbol.is_dirty,
            "code": symbol.code,
        }
        if symbol.uid:
            payload["uid"] = symbol.uid
        if symbol.range:
            payload["range"] = symbol.range
        if symbol.kind:
            payload["kind"] = symbol.kind
        return payload

    def _ranker_metadata(self) -> dict[str, Any]:
        ranker_state = dict(self.ranker_state)
        if not ranker_state and self.budget.get("ranker"):
            ranker_state["strategy"] = self.budget["ranker"]

        weights = self.budget.get("ranker_weights")
        if weights and "weights" not in ranker_state:
            ranker_state["weights"] = dict(weights)

        if "candidates_considered" not in ranker_state and "pool_size" in self.budget:
            ranker_state["candidates_considered"] = self.budget["pool_size"]
        if "candidates_selected" not in ranker_state:
            ranker_state["candidates_selected"] = len(self.graph_context) + len(self.documentation)
        if "pruned_total_count" not in ranker_state:
            ranker_state["pruned_total_count"] = len(self.pruned_details)
        return ranker_state

    def _serialize_pruned_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        pruned = sorted(
            self.pruned_details,
            key=lambda item: item.get("blended_score", item.get("gain", 0.0)),
            reverse=True,
        )
        return pruned[:limit]

    def token_count(self) -> int:
        """Count tokens in the assembled prompt using cl100k_base encoding (GPT-3.5/4)."""
        try:
            import tiktoken
        except ImportError as e:
            raise ImportError("tiktoken is required for token counting") from e

        enc = tiktoken.get_encoding("cl100k_base")
        prompt_text = self.to_system_prompt()
        return len(enc.encode(prompt_text))


class BudgetTooSmall(ValueError):
    """Raised when target symbol alone exceeds token_budget."""

    pass


@dataclass
class SubgraphNode:
    """Internal: graph node with metadata from expansion."""

    uid: str
    name: str
    file_path: str
    range: list[int]
    token_estimate: int
    relation: str
    direction: str
    depth: int
    relevance_score: float
    kind: str = ""
    qualified_name: str = ""
    evidence_role: str = ""
    render_mode: str = "full"
    file_hash: str = ""
    provenance: list[str] = field(default_factory=list)
    graph_score: float = 0.0
    semantic_score: float = 0.0
    blended_score: float = 0.0
    intent_weight: float = 0.0


@dataclass
class Subgraph:
    """Internal: result of graph expansion (before code resolution)."""

    primary: SubgraphNode
    nodes: list[SubgraphNode]
    budget: dict
    stopped_reason: str = ""
    pruned_details: list[dict] = field(default_factory=list)
