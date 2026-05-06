"""ContextArbitrator — thin orchestrator facade composing pure components."""

import hashlib
from typing import Any, cast

from sidecar.cache.layered import CachedBody, LayeredCache, default_cache
from sidecar.context.code_resolver import CodeResolver
from sidecar.context.deduplicator import ContextDeduplicator
from sidecar.context.graph_expander import GraphExpander
from sidecar.context.intent_classifier import IntentClassifier, IntentResolution, IntentSignal
from sidecar.context.prompt_compiler import PromptCompiler
from sidecar.context.types import PromptContext, SubgraphNode
from sidecar.context.unified_ranker import (
    DEFAULT_WEIGHTS,
    RankerWeights,
    UnifiedRanker,
    VectorSearcher,
)
from sidecar.workspace import DEFAULT_WORKSPACE_ID


class ContextArbitrator:
    """Orchestrator: composes GraphExpander, ContextDeduplicator, CodeResolver, DocResolver, PromptCompiler."""

    def __init__(
        self,
        neo4j_client,
        overlay=None,
        vector_db=None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        cache: LayeredCache | None = None,
        ranker_weights: RankerWeights | None = None,
    ):
        self.db = neo4j_client
        self.overlay = overlay
        self.vector_db = vector_db
        self.workspace_id = workspace_id
        self.cache = cache or default_cache
        self.ranker_weights = ranker_weights or DEFAULT_WEIGHTS

    def get_context_for_symbol(
        self,
        symbol_name: str,
        question: str = "",
        token_budget: int = 4000,
    ) -> PromptContext | str:
        """Orchestrate the pipeline: expand → deduplicate → resolve → compile (with intent-aware tier selection)."""
        intent_signal = IntentClassifier.classify_with_metadata(question)
        intent_resolution = IntentClassifier.resolve_signal_with_profile(
            intent_signal,
            self._repository_profile(),
        )
        cache_hits: list[str] = []

        if self.vector_db:
            return self._get_context_unified(
                symbol_name, question, token_budget, intent_signal, intent_resolution, cache_hits
            )
        return self._get_context_graph_only(
            symbol_name, token_budget, intent_signal, intent_resolution, cache_hits
        )

    # ------------------------------------------------------------------
    # Unified path (graph BFS + vector search blended)
    # ------------------------------------------------------------------

    def _get_context_unified(
        self,
        symbol_name,
        question,
        token_budget,
        intent_signal: IntentSignal,
        intent_resolution: IntentResolution,
        cache_hits,
    ) -> PromptContext | str:
        intent = intent_signal.primary
        ranker = UnifiedRanker(
            self.db,
            VectorSearcher(self.vector_db),
            workspace_id=self.workspace_id,
            weights=self.ranker_weights,
        )

        target, target_selection = cast(
            tuple[SubgraphNode | None, dict[str, Any]],
            ranker.get_target(
                symbol_name,
                query=question,
                intent=intent,
                with_metadata=True,
            ),
        )
        if target is None:
            fallback_target, fallback_selection = self._resolve_concept_anchor_target(
                ranker,
                symbol_name=symbol_name,
                question=question,
                intent=intent,
            )
            if fallback_target is not None:
                target = fallback_target
                target_selection = fallback_selection
            else:
                return f"Error: Symbol '{symbol_name}' not found in graph."

        reserved = UnifiedRanker.PREAMBLE_TOKENS + target.token_estimate
        if reserved > token_budget:
            # Fallback: if target is massive, use a signature-only estimate (~10% of size or capped)
            # and flag it for the resolver to only pull the head.
            target.token_estimate = min(500, int(target.token_estimate * 0.1))
            target.relation = "target_signature_only"
            reserved = UnifiedRanker.PREAMBLE_TOKENS + target.token_estimate
            if reserved > token_budget:
                return f"Error: Token budget {token_budget} too small even for signature."

        query_str = f"{symbol_name} {question}".strip()
        candidates, budget_info, stopped_reason, pruned_details, missing_roles = ranker.rank(
            target, query_str, intent, token_budget
        )

        subgraph, docs = ranker.candidates_to_subgraph(
            target, candidates, budget_info, stopped_reason, pruned_details
        )

        # Resolve code for all nodes
        resolver = CodeResolver(self.overlay, workspace_id=self.workspace_id)
        code_map = {}
        for node in [subgraph.primary] + subgraph.nodes:
            line_range = (node.range[0], node.range[1])
            overlay_dirty = bool(
                self.overlay and self.overlay.has(node.file_path, workspace_id=self.workspace_id)
            )
            cached = None
            if node.file_hash and not overlay_dirty:
                cached = self.cache.get_body(node.file_path, line_range, node.file_hash)
            if cached is not None:
                cache_hits.append("l1_body")
                code_map[node.uid] = (cached.code, cached.is_dirty)
                continue

            # OPTIMAL CONTEXT: Resolve signature-only for low-gain or distant neighbors
            is_target_massive = (
                node.uid == subgraph.primary.uid and node.relation == "target_signature_only"
            )
            is_distant_neighbor = node.render_mode == "signature_only"

            if is_target_massive or is_distant_neighbor:
                # Pull only the head (signature + docstring) — approx first 15 lines
                end_line = min(node.range[1], node.range[0] + 15)
                code, is_dirty = resolver.resolve(node.file_path, node.range[0], end_line)
            else:
                code, is_dirty = resolver.resolve(node.file_path, *line_range)

            code_map[node.uid] = (code, is_dirty)
            if node.file_hash and not is_dirty:
                self.cache.put_body(
                    node.file_path,
                    line_range,
                    node.file_hash,
                    CachedBody(code=code, token_count=node.token_estimate, is_dirty=False),
                )

        mechanism = ranker._determine_mechanism(target, query=question)
        required_roles = ranker._get_required_roles(mechanism)

        ctx = PromptCompiler().compile_with_intent(subgraph, code_map, docs, intent)
        ctx.stopped_reason = subgraph.stopped_reason
        ctx.mechanism = mechanism
        ctx.pruned_details = subgraph.pruned_details
        ctx.missing_roles = missing_roles
        ctx.intent_distribution = intent_signal.distribution
        ctx.intent_confidence = intent_signal.confidence
        ctx.intent_ambiguous = intent_signal.ambiguous
        ctx.intent_effective_mode = intent_resolution.effective_mode
        ctx.intent_resolution = intent_resolution.to_dict()
        ctx.budget["cache_hits"] = sorted(set(cache_hits))
        ctx.budget["ranker"] = "unified"
        w = self.ranker_weights
        ctx.budget["ranker_weights"] = {
            "alpha": w.alpha,
            "beta": w.beta,
            "gamma": w.gamma,
            "delta": w.delta,
            "epsilon": w.epsilon,
        }
        ctx.ranker_state = {
            "strategy": "unified",
            "weights": dict(ctx.budget["ranker_weights"]),
            "candidates_considered": budget_info.get("pool_size", 0),
            "candidates_selected": len(candidates),
            "pruned_total_count": len(pruned_details),
            "required_roles": required_roles,
            "target_selection": target_selection,
            "strategy_profile": getattr(ranker, "strategy_profile", {}),
        }
        return ctx

    @staticmethod
    def _should_try_concept_anchor_fallback(question: str) -> bool:
        q = (question or "").lower()
        return any(token in q for token in ("how ", "how does", "how do", "behavior", "works"))

    @staticmethod
    def _concept_anchor_candidates(symbol_name: str) -> list[str]:
        concept = (symbol_name or "").strip().lower()
        concept_map = {
            "middleware": ["use", "handle", "router", "route"],
        }
        return concept_map.get(concept, [])

    def _resolve_concept_anchor_target(
        self,
        ranker: UnifiedRanker,
        *,
        symbol_name: str,
        question: str,
        intent,
    ) -> tuple[SubgraphNode | None, dict[str, Any]]:
        if not self._should_try_concept_anchor_fallback(question):
            return None, {}
        anchors = self._concept_anchor_candidates(symbol_name)
        if not anchors:
            return None, {}
        for anchor in anchors:
            candidate, metadata = cast(
                tuple[SubgraphNode | None, dict[str, Any]],
                ranker.get_target(
                    anchor,
                    query=question,
                    intent=intent,
                    with_metadata=True,
                ),
            )
            if candidate is not None:
                metadata = {
                    **(metadata or {}),
                    "strategy": "concept_anchor_fallback",
                    "missing_symbol": symbol_name,
                    "anchor_symbol": anchor,
                    "anchors_considered": anchors,
                }
                return candidate, metadata
        return None, {}

    # ------------------------------------------------------------------
    # Graph-only path (no vector_db — original behaviour)
    # ------------------------------------------------------------------

    def _get_context_graph_only(
        self,
        symbol_name,
        token_budget,
        intent_signal: IntentSignal,
        intent_resolution: IntentResolution,
        cache_hits,
    ) -> PromptContext | str:
        intent = intent_signal.primary
        intent_hash = hashlib.sha256(intent.value.encode("utf-8")).hexdigest()
        graph_version = self._graph_version()

        subgraph = None
        primary_uid = self._primary_uid(symbol_name)
        if primary_uid:
            subgraph = self.cache.get_subgraph(
                primary_uid, intent_hash, token_budget, self.workspace_id, graph_version
            )
            if subgraph is not None:
                cache_hits.append("l2_subgraph")

        if subgraph is None:
            subgraph = GraphExpander(self.db, workspace_id=self.workspace_id).expand(
                symbol_name, token_budget=token_budget
            )
            if not isinstance(subgraph, str):
                primary_uid = subgraph.primary.uid
                self.cache.put_subgraph(
                    primary_uid,
                    intent_hash,
                    token_budget,
                    self.workspace_id,
                    graph_version,
                    subgraph,
                )
        if isinstance(subgraph, str):
            return subgraph

        subgraph = ContextDeduplicator().deduplicate(subgraph)

        resolver = CodeResolver(self.overlay, workspace_id=self.workspace_id)
        code_map = {}
        for node in [subgraph.primary] + subgraph.nodes:
            line_range = (node.range[0], node.range[1])
            overlay_dirty = bool(
                self.overlay and self.overlay.has(node.file_path, workspace_id=self.workspace_id)
            )
            cached = None
            if node.file_hash and not overlay_dirty:
                cached = self.cache.get_body(node.file_path, line_range, node.file_hash)
            if cached is not None:
                cache_hits.append("l1_body")
                code_map[node.uid] = (cached.code, cached.is_dirty)
                continue
            code, is_dirty = resolver.resolve(node.file_path, *line_range)
            code_map[node.uid] = (code, is_dirty)
            if node.file_hash and not is_dirty:
                self.cache.put_body(
                    node.file_path,
                    line_range,
                    node.file_hash,
                    CachedBody(code=code, token_count=node.token_estimate, is_dirty=False),
                )

        ctx = PromptCompiler().compile_with_intent(subgraph, code_map, [], intent)
        ctx.intent_distribution = intent_signal.distribution
        ctx.intent_confidence = intent_signal.confidence
        ctx.intent_ambiguous = intent_signal.ambiguous
        ctx.intent_effective_mode = intent_resolution.effective_mode
        ctx.intent_resolution = intent_resolution.to_dict()
        ctx.budget["cache_hits"] = sorted(set(cache_hits))
        ctx.budget["ranker"] = "graph_only"
        ctx.ranker_state = {
            "strategy": "graph_only",
            "candidates_considered": len(subgraph.nodes),
            "candidates_selected": len(subgraph.nodes),
            "pruned_total_count": len(subgraph.pruned_details),
        }
        return ctx

    def _repository_profile(self) -> dict | None:
        get_profile = getattr(self.db, "get_repository_profile", None)
        if not callable(get_profile):
            return None
        try:
            profile = get_profile(workspace_id=self.workspace_id)
        except Exception:
            return None
        return profile if isinstance(profile, dict) else None

    def _primary_uid(self, symbol_name: str) -> str | Any | None:
        query = """
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {name: $name})
        RETURN s.uid AS uid
        LIMIT 1
        """
        try:
            with self.db.driver.session() as session:
                result = session.run(
                    query, name=symbol_name, workspace_id=self.workspace_id
                ).single()
        except Exception:
            return None
        if not result:
            return None
        try:
            return result["uid"]
        except (KeyError, TypeError):
            return None

    def _graph_version(self) -> int:
        query = """
        MATCH (w:Workspace {id: $workspace_id})
        RETURN coalesce(w.graph_version, 0) AS graph_version
        """
        try:
            with self.db.driver.session() as session:
                result = session.run(query, workspace_id=self.workspace_id).single()
        except Exception:
            return 0
        if not result:
            return 0
        try:
            return int(result["graph_version"])
        except (KeyError, TypeError, ValueError):
            return 0
