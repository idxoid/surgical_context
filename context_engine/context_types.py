"""Shared dataclasses for context assembly pipeline."""

from dataclasses import dataclass, field
from typing import Any

CONTEXT_PIPELINE_VERSION = "context-axis-v1"

# Chain priority for prompt ordering. The ranker sets ``chain_kind`` on a
# candidate when it emits a structural-chain signal (mandatory contract anchor,
# query-API seed, registration / marker chain step, api-relay, …); ``_dep_sort_key``
# reads it as a single typed value instead of re-parsing provenance strings.
# Higher number = sorts earlier inside the same caller group.
CHAIN_PRIORITY: dict[str, int] = {
    "mandatory": 4,
    "query_seed": 3,
    "registration": 2,
    "api_callee": 2,
    "relay": 1,
}


def upgrade_chain_kind(current: str, candidate: str) -> str:
    """Return whichever chain_kind has higher CHAIN_PRIORITY (current wins on tie)."""
    if not candidate:
        return current
    return (
        candidate if CHAIN_PRIORITY.get(candidate, 0) > CHAIN_PRIORITY.get(current, 0) else current
    )


@dataclass
class SymbolContext:
    symbol: str
    file_path: str
    relation: str
    uid: str = ""
    range: list[int] = field(default_factory=list)
    kind: str = ""
    edge_type: str = ""
    direction: str = "callee"
    depth: int = 0
    relevance_score: float = 0.0
    graph_score: float = 0.0
    semantic_score: float = 0.0
    blended_score: float = 0.0
    intent_weight: float = 0.0
    render_mode: str = "full"
    is_dirty: bool = False
    code: str = ""
    provenance: list[str] = field(default_factory=list)
    chain_kind: str = ""


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
    workspace_id: str = ""
    context_pipeline_version: str = CONTEXT_PIPELINE_VERSION
    stage_timings_ms: dict[str, float] = field(default_factory=dict)
    token_counts: dict[str, int] = field(default_factory=dict)
    model_route: dict[str, Any] = field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    cost_basis: str = "not_configured"
    pruning_reasons: list[str] = field(default_factory=list)
    feedback_token: str = ""
    index_manifest_id: str = ""
    index_manifest_schema_version: int | None = None

    # Relations that are callers of the target (explain *why* / *who uses* it).
    _CALLER_RELATIONS = frozenset(
        {
            "caller",
            "CALLS_DIRECT_in",
            "CALLS_SCOPED_in",
            "CALLS_DYNAMIC_in",
            "CALLS_INFERRED_in",
            "CALLS_GUESS_in",
            "CALLS_in",
        }
    )

    def _dep_sort_key(self, dep: "SymbolContext") -> tuple:
        """Sort: callers first, then chain priority, then shallower depth, then higher score."""
        is_caller = dep.direction == "caller" or dep.relation in self._CALLER_RELATIONS
        return (
            0 if is_caller else 1,
            -CHAIN_PRIORITY.get(dep.chain_kind, 0),
            dep.depth,
            -(dep.blended_score or dep.relevance_score),
        )

    def ordered_graph_context(self, *, include_empty_code: bool = False) -> list[SymbolContext]:
        """Return graph deps in the same order used for prompt rendering."""
        ordered = sorted(self.graph_context, key=self._dep_sort_key)
        if include_empty_code:
            return ordered
        return [dep for dep in ordered if dep.code and dep.code.strip()]

    @staticmethod
    def _dep_annotation(dep: "SymbolContext") -> str:
        parts = [dep.relation]
        if dep.depth:
            parts.append(f"depth={dep.depth}")
        if dep.blended_score:
            parts.append(f"score={dep.blended_score:.2f}")
        return ", ".join(parts)

    def to_system_prompt(self) -> str:
        """Render to the flat text format the LLM receives."""
        # Incompleteness disclaimer when the LLM would otherwise see partial context.
        header_lines: list[str] = []
        if self.stopped_reason in (
            "budget_exhausted",
            "expansion_no_progress",
            "floor_unfilled_sparse_target",
        ):
            spent = self.budget.get("spent", 0)
            limit = self.budget.get("limit", 0)
            header_lines.append(
                f"# Context note: budget limit reached ({spent}/{limit} tokens). Some neighbours omitted."
            )

        blocks: list[str] = []
        if header_lines:
            blocks.extend(header_lines)

        blocks.append(f"--- TARGET SYMBOL: {self.primary_source.symbol} ---")
        blocks.append(self.primary_source.code)

        # #3: sort callers before callees before deep neighbours.
        # #1: annotate each dependency with role, depth, and score.
        # #9: skip entries with no code.
        if self.graph_context:
            non_empty = self.ordered_graph_context()
            if non_empty:
                blocks.append("\n--- DEPENDENCIES ---")
                for dep in non_empty:
                    annotation = self._dep_annotation(dep)
                    blocks.append(f"\n# {dep.symbol} [{annotation}]:")
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
                "pruned_count": len(self.pruned_details),
                "tier_tokens": self.tier_tokens,
                "tokens_primary": self.tier_tokens.get("code", 0),
                "tokens_graph": self.tier_tokens.get("cross_refs", 0),
                "tokens_docs": docs_tokens,
                "pruning_reasons": pruning_reasons,
                "ranker": ranker_state,
                "index_manifest_id": self.index_manifest_id or None,
                "index_manifest_schema_version": self.index_manifest_schema_version,
                "assembly": {
                    "trace_id": self.trace_id,
                    "workspace_id": self.workspace_id,
                    "context_pipeline_version": self.context_pipeline_version,
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
            "provenance": symbol.provenance or ["graph", "axis"],
            "render_mode": symbol.render_mode,
            "is_dirty": symbol.is_dirty,
            "code": symbol.code,
        }
        if symbol.uid:
            payload["uid"] = symbol.uid
        if symbol.range:
            payload["range"] = symbol.range
        if symbol.kind:
            payload["kind"] = symbol.kind
        if symbol.edge_type:
            payload["edge_type"] = symbol.edge_type
        return payload

    def _ranker_metadata(self) -> dict[str, Any]:
        """Assembly counts for observability (axis path; no cascade ranker state)."""
        out: dict[str, Any] = {
            "candidates_selected": len(self.graph_context) + len(self.documentation),
            "pruned_total_count": len(self.pruned_details),
        }
        pool_size = self.budget.get("pool_size")
        if pool_size is not None:
            out["candidates_considered"] = pool_size
        return out

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
    chain_kind: str = ""


@dataclass
class Subgraph:
    """Internal: result of graph expansion (before code resolution)."""

    primary: SubgraphNode
    nodes: list[SubgraphNode]
    budget: dict
    stopped_reason: str = ""
    pruned_details: list[dict] = field(default_factory=list)
