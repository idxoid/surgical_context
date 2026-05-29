"""ContextArbitrator — thin orchestrator facade composing pure components."""

import logging
from typing import TYPE_CHECKING, Any, cast

from sidecar.cache.layered import CachedBody, LayeredCache, default_cache
from sidecar.context.code_resolver import CodeResolver
from sidecar.context.intent_classifier import (
    Intent,
    IntentClassifier,
    IntentResolution,
    IntentSignal,
)
from sidecar.context.prompt_compiler import PromptCompiler
from sidecar.context.ranker.signal_constants import NOISE_PATH_PATTERNS
from sidecar.context.role_taxonomy import normalize_roles
from sidecar.context.types import PromptContext, SubgraphNode
from sidecar.context.unified_ranker import (
    DEFAULT_WEIGHTS,
    RankerWeights,
    UnifiedRanker,
    VectorSearcher,
)
from sidecar.retrieval.trace import unified_trace
from sidecar.workspace import DEFAULT_WORKSPACE_ID

_log = logging.getLogger(__name__)
if TYPE_CHECKING:
    from sidecar.retrieval.protocols import VectorSearchProvider, WorkspaceMetaProvider


class ContextArbitrator:
    """Orchestrator: UnifiedRanker → CodeResolver → PromptCompiler (intent-aware tier assembly)."""

    def __init__(
        self,
        neo4j_client,
        overlay=None,
        vector_db=None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
        cache: LayeredCache | None = None,
        ranker_weights: RankerWeights | None = None,
        *,
        vector_search: "VectorSearchProvider | None" = None,
        workspace_meta: "WorkspaceMetaProvider | None" = None,
    ):
        self.db = neo4j_client
        self.overlay = overlay
        self.vector_db = vector_db
        self.workspace_id = workspace_id
        self.user_id = (user_id or "anonymous").lower().strip() or "anonymous"
        self.cache = cache or default_cache
        self.ranker_weights = ranker_weights or DEFAULT_WEIGHTS
        self._vector_search = vector_search
        self._workspace_meta = workspace_meta

    def get_context_for_symbol(
        self,
        symbol_name: str,
        question: str = "",
        token_budget: int = 4000,
    ) -> PromptContext | str:
        """Orchestrate the pipeline: rank → resolve → compile (with intent-aware tier selection)."""
        intent_signal = IntentClassifier.classify_with_metadata(question)
        intent_policy = IntentClassifier.policy_from_signal(intent_signal)
        intent_resolution = IntentClassifier.resolve_signal_with_profile(
            intent_signal,
            self._repository_profile(),
        )
        cache_hits: list[str] = []

        if not self.vector_db and not self._vector_search:
            _log.warning(
                "No vector_db configured for workspace %s — running in graph-only mode "
                "(semantic search and role backfill are active but vector scores will be zero).",
                self.workspace_id,
            )
        return self._get_context_unified(
            symbol_name,
            question,
            token_budget,
            intent_signal,
            intent_policy,
            intent_resolution,
            cache_hits,
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
        intent_policy,
        intent_resolution: IntentResolution,
        cache_hits,
    ) -> PromptContext | str:
        intent = intent_signal.primary
        ranker = UnifiedRanker(
            self.db,
            self._vector_search_for_ranker(),
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
        elif self._target_selection_is_low_quality(target_selection):
            fallback_target, fallback_selection = self._resolve_concept_anchor_target(
                ranker,
                symbol_name=symbol_name,
                question=question,
                intent=intent,
            )
            if fallback_target is not None:
                target = fallback_target
                target_selection = {
                    **fallback_selection,
                    "replaced_target_selection": target_selection,
                }

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
        # Derive secondary intent from score distribution (runner-up after primary).
        secondary_intent: Intent | None = None
        if intent_signal.ambiguous and intent_signal.distribution:
            sorted_intents = sorted(
                intent_signal.distribution.items(), key=lambda kv: kv[1], reverse=True
            )
            for name, _ in sorted_intents:
                try:
                    candidate_intent = Intent(name)
                except ValueError:
                    continue
                if candidate_intent != intent:
                    secondary_intent = candidate_intent
                    break
        candidates, budget_info, stopped_reason, pruned_details, missing_roles = ranker.rank(
            target,
            query_str,
            intent,
            token_budget,
            ambiguous=intent_signal.ambiguous,
            secondary_intent=secondary_intent,
            intent_policy=intent_policy,
        )

        subgraph, docs = ranker.candidates_to_subgraph(
            target, candidates, budget_info, stopped_reason, pruned_details
        )

        # Resolve code for all nodes (sandbox graph file paths under workspace root)
        from sidecar.workspace_paths import registered_workspace_root

        project_root = registered_workspace_root(self.db, self.workspace_id)
        resolver = CodeResolver(
            self.overlay,
            workspace_id=self.workspace_id,
            user_id=self.user_id,
            workspace_root=project_root,
        )
        code_map = {}
        for node in [subgraph.primary] + subgraph.nodes:
            line_range = (node.range[0], node.range[1])
            is_target_massive = (
                node.uid == subgraph.primary.uid and node.relation == "target_signature_only"
            )
            is_distant_neighbor = node.render_mode == "signature_only"
            if is_target_massive or is_distant_neighbor:
                # Pull only the head (signature + docstring) and expose/cache
                # the effective range, not the original full symbol span.
                end_line = min(node.range[1], node.range[0] + 15)
                line_range = (node.range[0], end_line)
                node.range = [line_range[0], line_range[1]]
                node.render_mode = "signature_only"

            from sidecar.workspace_paths import resolve_graph_file_path

            safe_file_path = resolve_graph_file_path(node.file_path, workspace_root=project_root)
            if safe_file_path is None:
                code_map[node.uid] = ("", False)
                continue

            overlay_dirty = bool(
                self.overlay
                and self.overlay.has(
                    safe_file_path,
                    workspace_id=self.workspace_id,
                    user_id=self.user_id,
                )
            )
            cached = None
            if node.file_hash and not overlay_dirty:
                cached = self.cache.get_body(safe_file_path, line_range, node.file_hash)
            if cached is not None:
                cache_hits.append("l1_body")
                code_map[node.uid] = (cached.code, cached.is_dirty)
                continue

            code, is_dirty = resolver.resolve(safe_file_path, *line_range)

            code_map[node.uid] = (code, is_dirty)
            if node.file_hash and not is_dirty:
                self.cache.put_body(
                    safe_file_path,
                    line_range,
                    node.file_hash,
                    CachedBody(code=code, token_count=node.token_estimate, is_dirty=False),
                )

        mechanism = ranker._determine_mechanism(target, query=question)
        required_roles = ranker._get_required_roles(mechanism)
        if intent == Intent.IMPACT_ANALYSIS:
            required_roles = [
                "impact_runtime",
                "impact_public_api",
                "impact_test_surface",
                "docs_or_concept",
            ]
        required_roles = normalize_roles([*required_roles, *intent_policy.supplemental_roles])

        ctx = PromptCompiler().compile_with_intent(
            subgraph,
            code_map,
            docs,
            intent,
            tier_priority=intent_policy.tier_order,
        )
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
        has_vector = bool(self.vector_db or self._vector_search)
        ctx.budget["ranker"] = "unified" if has_vector else "unified_graph_only"
        w = self.ranker_weights
        ctx.budget["ranker_weights"] = {
            "alpha": w.alpha,
            "beta": w.beta,
            "gamma": w.gamma,
            "delta": w.delta,
            "epsilon": w.epsilon,
        }
        ctx.budget["intent_policy"] = {
            "active_intents": [intent.value for intent in intent_policy.active_intents],
            "secondary_intents": [intent.value for intent in intent_policy.secondary_intents],
            "budget_share": intent_policy.budget_share,
            "tier_order": list(intent_policy.tier_order),
            "supplemental_roles": list(intent_policy.supplemental_roles),
            "doc_first": intent_policy.doc_first,
        }
        ctx.ranker_state = {
            "strategy": "unified" if has_vector else "unified_graph_only",
            "weights": dict(ctx.budget["ranker_weights"]),
            "candidates_considered": budget_info.get("pool_size", 0),
            "candidates_selected": len(candidates),
            "pruned_total_count": len(pruned_details),
            "required_roles": required_roles,
            "intent_policy": dict(ctx.budget["intent_policy"]),
            "target_selection": target_selection,
            "strategy_profile": getattr(ranker, "strategy_profile", {}),
        }
        ctx.retrieval_trace = unified_trace(
            workspace_id=self.workspace_id,
            intent=intent.value,
            mechanism=mechanism,
            required_roles=required_roles,
            stopped_reason=subgraph.stopped_reason or "",
            target_selection=target_selection or {},
            budget_info=budget_info,
            ranker_state=ctx.ranker_state,
            cache_hits=sorted(set(cache_hits)),
            missing_roles=missing_roles,
            pruned_count=len(pruned_details),
        )
        return ctx

    @staticmethod
    def _should_try_concept_anchor_fallback(question: str) -> bool:
        q = (question or "").lower()
        return any(token in q for token in ("how ", "how does", "how do", "behavior", "works"))

    @staticmethod
    def _target_selection_is_low_quality(target_selection: dict[str, Any] | None) -> bool:
        if not target_selection:
            return False
        score = target_selection.get("selected_score")
        try:
            numeric_score = float(score)
        except (TypeError, ValueError):
            return False
        return numeric_score < 0.0

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
        anchors: list[str] = []
        dynamic_anchor_loader = getattr(ranker, "concept_anchor_candidates", None)
        if callable(dynamic_anchor_loader):
            anchors.extend(dynamic_anchor_loader(symbol_name, query=question))
        anchors = list(dict.fromkeys(anchors))
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

    def _vector_search_for_ranker(self):
        if self._vector_search is not None:
            return self._vector_search
        return VectorSearcher(self.vector_db)

    def _repository_profile(self) -> dict | None:
        if self._workspace_meta is not None:
            profile = self._workspace_meta.repository_profile(self.workspace_id)
            return profile if profile else None
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
        MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {name: $name})
        WHERE NOT any(noise IN $noise_patterns WHERE f.path CONTAINS noise)
        OPTIONAL MATCH ()-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->(s)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
        WITH s, f, count(DISTINCT r) AS inbound_edges,
             CASE WHEN toLower(f.path) CONTAINS ('/' + toLower($name) + '.') THEN 1 ELSE 0 END AS stem_match
        RETURN s.uid AS uid, inbound_edges, stem_match
        ORDER BY stem_match DESC, inbound_edges DESC
        LIMIT 1
        """
        try:
            with self.db.driver.session() as session:
                result = session.run(
                    query,
                    name=symbol_name,
                    workspace_id=self.workspace_id,
                    noise_patterns=list(NOISE_PATH_PATTERNS),
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
        if self._workspace_meta is not None:
            return int(self._workspace_meta.graph_version(self.workspace_id))
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
