"""UnifiedRanker — blends graph BFS scores with semantic vector scores.

Replaces the current "graph then append top-3 docs" pattern with a single
ranked candidate pool where symbols and doc chunks compete on equal terms.

Score formula:
    score(c) = α * graph_score(c)
             + β * semantic_score(c)
             + γ * intent_weight(c)
             + δ * overlap_bonus(c)   # non-zero when BOTH signals fired
             - ε * token_cost(c) / 100

Both graph_score and semantic_score are normalized to [0, 1] before blending
so raw BFS values (~1.2) don't dominate cosine similarities (~0.8).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from heapq import heappop, heappush
from typing import cast

from sidecar.context.intent_classifier import (
    INTENT_TRAVERSAL,
    Intent,
    IntentClassifier,
    IntentPolicy,
    IntentSignal,
    QuestionShape,
    extract_question_shape,
    intent_edge_boost,
    modulate_shape,
)
from sidecar.context.mechanism_registry import role_backfill_specs_for_mechanism
from sidecar.context.ranker import (
    BudgetSelector,
    GraphCandidateSource,
    RoleBackfill,
    SubgraphAssembler,
    TargetSelector,
    VectorCandidateSource,
)
from sidecar.context.ranker.candidate_pool import (
    DEFAULT_WEIGHTS,
    Candidate,
    RankerWeights,
    VectorSearcher,
    anchor_edge_quality,
)
from sidecar.context.ranker.pruning import BudgetPruner
from sidecar.context.ranker.role_fulfilment import RoleFulfilment
from sidecar.context.ranker.scoring import RankerScoring
from sidecar.indexer.signal_constants import (
    EXPLORATION_NOISE_FACTOR as _EXPLORATION_NOISE_FACTOR,
)
from sidecar.indexer.signal_constants import (
    IMPACT_TOPIC_STOPWORDS as _IMPACT_TOPIC_STOPWORDS,
)
from sidecar.indexer.signal_constants import (
    LOW_SIGNAL_DOC_PATH_PATTERNS as _LOW_SIGNAL_DOC_PATH_PATTERNS,
)
from sidecar.indexer.signal_constants import (
    NOISE_FACTOR as _NOISE_FACTOR,
)
from sidecar.indexer.signal_constants import (
    NOISE_NAME_PREFIXES as _NOISE_NAME_PREFIXES,
)
from sidecar.indexer.signal_constants import (
    NOISE_NAME_SUBSTRINGS as _NOISE_NAME_SUBSTRINGS,
)
from sidecar.indexer.signal_constants import (
    NOISE_PATH_PATTERNS as _NOISE_PATH_PATTERNS,
)
from sidecar.context.role_taxonomy import normalize_roles
from sidecar.context.types import DocChunk, Subgraph, SubgraphNode, upgrade_chain_kind
from sidecar.workspace import DEFAULT_WORKSPACE_ID


def _path_is_noisy(file_path: str) -> bool:
    if not file_path:
        return False
    return any(pat in file_path for pat in _NOISE_PATH_PATTERNS)


def _name_is_noisy(name: str) -> bool:
    if not name:
        return False
    lower = name.lower()
    if name.startswith(_NOISE_NAME_PREFIXES):
        return True
    return any(sub in lower for sub in _NOISE_NAME_SUBSTRINGS)


def compute_noise_factor(
    file_path: str,
    name: str,
    *,
    kind: str = "symbol",
    intent: Intent | None = None,
) -> float:
    """Multiplicative score multiplier in [0, 1]."""
    is_noisy = _path_is_noisy(file_path) or _name_is_noisy(name)
    if is_noisy:
        if intent == Intent.EXPLORATION:
            return _EXPLORATION_NOISE_FACTOR
        return _NOISE_FACTOR
    if kind == "doc" and any(
        pat in (file_path or "").lower() for pat in _LOW_SIGNAL_DOC_PATH_PATTERNS
    ):
        return 0.35
    return 1.0


def compute_impact_noise_factor(
    file_path: str,
    name: str,
    *,
    query: str = "",
    target_name: str = "",
    kind: str = "symbol",
    content: str = "",
) -> float:
    """Noise factor for impact-analysis candidates."""
    if not (_path_is_noisy(file_path) or _name_is_noisy(name)):
        return compute_noise_factor(file_path, name, kind=kind)

    terms = {
        token
        for token in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", f"{target_name} {query}".lower())
        if token not in _IMPACT_TOPIC_STOPWORDS
    }
    if not terms:
        return _NOISE_FACTOR

    haystack = f"{file_path} {name} {content}".lower()
    if any(term in haystack for term in terms):
        return 1.0
    return _NOISE_FACTOR


class UnifiedRanker:
    """Merge graph BFS candidates and vector search candidates into one ranked pool."""

    PREAMBLE_TOKENS = 100
    _WORKSPACE_STATE_CACHE: dict[tuple[int, str], dict] = {}

    # Per-intent floors: we must not stop based on marginal gain
    # until we hit these minimums to ensure grounding.
    _INTENT_FLOORS = {
        Intent.NAVIGATION: 500,
        Intent.EXPLORATION: 1200,
        Intent.DEBUGGING: 1500,
        Intent.NEW_FEATURE: 2500,
        Intent.REFACTORING: 2500,
        Intent.DESIGN_QUESTION: 3500,
        Intent.IMPACT_ANALYSIS: 2200,
    }

    # Copied from GraphExpander to keep UnifiedRanker self-contained.
    _RELATION_PRIOR: dict[str, float] = {
        "CALLS_DIRECT_out": 1.0,
        "CALLS_DIRECT_in": 1.2,
        "CALLS_DYNAMIC_out": 0.7,
        "CALLS_DYNAMIC_in": 0.9,
        "CALLS_INFERRED_out": 0.4,
        "CALLS_INFERRED_in": 0.5,
        "CALLS_SCOPED_out": 0.9,
        "CALLS_SCOPED_in": 1.1,
        "CALLS_IMPORTED_out": 0.85,
        "CALLS_IMPORTED_in": 1.0,
        "CALLS_GUESS_out": 0.4,
        "CALLS_GUESS_in": 0.5,
        "IMPLEMENTS": 1.1,
        "OVERRIDES": 1.1,
        "REFERENCES": 0.3,
        "DEPENDS_ON": 0.8,
        "IMPORTS": 0.6,
        # File-level integration coref hop, materialised by
        # `_get_integrates_with_neighbors`. The candidate enters the pool but
        # its BFS expansion is suppressed (see the `INTEGRATES_COREF` gate
        # before `_get_neighbors` in `_graph_candidates_impl`): the integrating
        # file should surface ONE representative symbol that opens up retrieval
        # of that file, not drag its whole structural subtree (which in dense
        # repos like Celery — every internal module shares kombu/amqp imports
        # — displaces canonical backend files). With the terminal gate the
        # prior is safe at HAS_API_in level (1.2): the seed-side coref is the
        # only path to an otherwise unreachable file, and competing CALLS edges
        # on the same iteration are ranked by their own priors.
        "INTEGRATES_COREF": 1.2,
        "CALLS_out": 1.0,
        "CALLS_in": 1.2,
        "SEMANTIC_HINT_out": 1.3,
        "SEMANTIC_HINT_in": 1.3,
        "HAS_API_out": 1.45,
        "HAS_API_in": 1.2,
        "INHERITED_API_out": 1.35,
        "INHERITED_API_in": 1.15,
        # decorated_symbol -[DECORATED_BY]-> decorator. outgoing = the decorated
        # symbol reaching its decorator (the mechanism it plugs into); incoming =
        # a decorator reaching the symbols it decorates (registration surface).
        "DECORATED_BY_out": 1.0,
        "DECORATED_BY_in": 1.1,
        # dispatcher -[HANDLES]-> registered handler (inverse of DECORATED_BY).
        # outgoing = registry/decorator reaching handlers it owns; incoming = a
        # handler reached from its registration hook (@app.route, @app.task, …).
        "HANDLES_out": 1.15,
        "HANDLES_in": 0.95,
        # referrer -[USES_TYPE]-> project class it names (annotation/isinstance).
        # outgoing = a symbol reaching a type it consumes; incoming = a type
        # reaching its consumers. Low prior: a weaker association than a call, kept
        # separate so it can be filtered and never inflates degree.
        "USES_TYPE_out": 0.6,
        "USES_TYPE_in": 0.5,
        # ...but a DISPATCHER reference (isinstance/issubclass on the type) is the
        # resolution machinery for that type. Incoming from the type to its
        # dispatcher is the strong hop (a marker class -> the code that resolves it),
        # so it outranks the low-signal flood of param-annotation consumers.
        "USES_TYPE_DISPATCH_out": 0.7,
        "USES_TYPE_DISPATCH_in": 1.25,
        # owner -[INJECTS]-> provider wired into a parameter default. outgoing = an
        # owner reaching what it has injected (its runtime collaborators); incoming =
        # a provider reaching the owners that inject it. A real control-flow binding,
        # weighted near a scoped call.
        "INJECTS_out": 1.05,
        "INJECTS_in": 1.15,
        # caller -[INSTANTIATES]-> constructed class. Outgoing is a strong
        # factory/contract hop; incoming is weaker because many callers can
        # construct the same data type.
        "INSTANTIATES_out": 1.05,
        "INSTANTIATES_in": 0.65,
        # proxy_binding -[RESOLVES_ATTR]-> context accessor. Strong outgoing hop:
        # it is the binding semantics behind a lazy global proxy.
        "RESOLVES_ATTR_out": 1.25,
        "RESOLVES_ATTR_in": 0.8,
    }

    def __init__(
        self,
        neo4j_client,
        vector_searcher: VectorSearcher,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        weights: RankerWeights = DEFAULT_WEIGHTS,
    ):
        self.db = neo4j_client
        self.vector = vector_searcher
        self.workspace_id = workspace_id
        self.weights = weights
        workspace_state = self._load_workspace_state()
        self.repository_profile = workspace_state["repository_profile"]
        self.strategy_profile = self.repository_profile.get("strategy_profile", {})
        self.role_catalog = workspace_state["role_catalog"]
        self._derived_primary_role_by_uid = workspace_state["derived_primary_role_by_uid"]
        self._derived_supporting_roles_by_uid = workspace_state[
            "derived_supporting_roles_by_uid"
        ]
        self._structural_fan_by_uid = workspace_state["structural_fan_by_uid"]
        self._workspace_role_supply_counts_cache = None
        self._target_role_supply_counts_cache: dict[tuple[str, int, int], Counter[str]] = {}
        self.role_fulfilment = RoleFulfilment(self)
        self.scoring = RankerScoring(self)
        self.budget_pruner = BudgetPruner(self)
        self.target_selector = TargetSelector(self)
        self.graph_candidate_source = GraphCandidateSource(self)
        self.vector_candidate_source = VectorCandidateSource(self)
        self.role_backfill = RoleBackfill(self)
        self.budget_selector = BudgetSelector(self)
        self.subgraph_assembler = SubgraphAssembler(self)
        self._workspace_root = None

    def _load_workspace_state(self) -> dict:
        cache_key = (id(self.db), self.workspace_id)
        cached = self._WORKSPACE_STATE_CACHE.get(cache_key)
        if cached is not None:
            return cached

        repository_profile = self._load_repository_profile()
        role_catalog = self._load_role_catalog()
        role_state = self._load_derived_role_state_for_catalog(role_catalog)
        state = {
            "repository_profile": repository_profile,
            "role_catalog": role_catalog,
            "derived_primary_role_by_uid": role_state["primary"],
            "derived_supporting_roles_by_uid": role_state["supporting"],
            "structural_fan_by_uid": role_state["fan"],
        }
        self._WORKSPACE_STATE_CACHE[cache_key] = state
        return state

    def _workspace_project_root(self):
        if self._workspace_root is None:
            from sidecar.workspace_paths import registered_workspace_root

            self._workspace_root = registered_workspace_root(self.db, self.workspace_id)
        return self._workspace_root

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_target(
        self,
        symbol_name: str,
        query: str = "",
        intent: Intent | None = None,
        *,
        with_metadata: bool = False,
    ) -> SubgraphNode | tuple[SubgraphNode | None, dict] | None:
        """Fetch the primary symbol from Neo4j, disambiguating duplicates when needed."""
        return self.target_selector.get_target(
            symbol_name,
            query=query,
            intent=intent,
            with_metadata=with_metadata,
        )

    def _select_target_candidate(
        self,
        symbol_name: str,
        *,
        query: str = "",
        intent: Intent | None = None,
    ) -> tuple[SubgraphNode | None, dict]:
        return self.target_selector._select_target_candidate(
            symbol_name, query=query, intent=intent
        )

    def _load_target_candidates(self, symbol_name: str) -> list[dict]:
        from sidecar.indexer.mro_api_bridge import parse_class_method_symbol

        parsed = parse_class_method_symbol(symbol_name)
        if parsed:
            api_rows = self._load_class_method_target_candidates(*parsed)
            if api_rows:
                return api_rows

        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol {name: $name})
        CALL {
            WITH s
            OPTIONAL MATCH (s)-[out_r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT|HAS_API|INHERITED_API]->(:Symbol)
            WHERE coalesce(out_r.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT out_r) AS outgoing_edges
        }
        CALL {
            WITH s
            OPTIONAL MATCH (:Symbol)-[in_r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT|HAS_API|INHERITED_API]->(s)
            WHERE coalesce(in_r.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT in_r) AS incoming_edges
        }
        CALL {
            WITH s
            OPTIONAL MATCH (s)-[any_r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT|HAS_API|INHERITED_API]-(:Symbol)
            WHERE coalesce(any_r.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT any_r) AS total_edges
        }
        CALL {
            WITH f
            OPTIONAL MATCH (importer:File)-[imp_r:IMPORTS]->(f)
            WHERE coalesce(imp_r.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT importer) AS file_imports_in
        }
        WITH s, f, c, outgoing_edges, incoming_edges, total_edges, file_imports_in,
             CASE WHEN toLower(f.path) CONTAINS ('/' + toLower($name) + '.') THEN 1 ELSE 0 END AS stem_match
        ORDER BY
          CASE
            WHEN f.path CONTAINS '/test/' OR f.path CONTAINS '/tests/'
              OR f.path CONTAINS '/integration/' OR f.path CONTAINS '/sample/'
              OR f.path CONTAINS '/samples/' THEN 1
            ELSE 0
          END ASC,
          stem_match DESC,
          total_edges DESC,
          outgoing_edges DESC,
          size(f.path) ASC
        LIMIT $limit
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS kind,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               outgoing_edges,
               incoming_edges,
               total_edges,
               file_imports_in
        """
        try:
            with self.db.driver.session() as session:
                result = list(
                    session.run(
                        query,
                        name=symbol_name,
                        workspace_id=self.workspace_id,
                        limit=64,
                    )
                )
        except Exception:
            return []
        return result

    def _load_class_method_target_candidates(
        self,
        class_name: str,
        method_name: str,
    ) -> list[dict]:
        """Resolve ``Class.method`` via direct API, on-demand MRO, or qname tail."""
        qualified_suffix = f".{class_name}.{method_name}"
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol {name: $method_name})
        WITH s, f, c,
             EXISTS {
               MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->
                     (:Symbol {name: $class_name, kind: 'class'})
                     -[:HAS_API {workspace_id: $workspace_id}]->(s)
             } AS direct_api,
             EXISTS {
               MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->
                     (sub_cls:Symbol {name: $class_name, kind: 'class'})
               MATCH path=(sub_cls)-[:DEPENDS_ON*1..24]->(:Symbol)
                          -[:HAS_API {workspace_id: $workspace_id}]->(s)
               WHERE all(rel IN relationships(path)
                         WHERE coalesce(rel.workspace_id, $workspace_id) = $workspace_id)
             } AS inherited_api,
             coalesce(s.qualified_name, '') ENDS WITH $qualified_suffix AS qualified_tail
        WHERE direct_api OR inherited_api OR qualified_tail
        CALL {
            WITH s
            OPTIONAL MATCH (s)-[out_r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT|HAS_API|INHERITED_API]->(:Symbol)
            WHERE coalesce(out_r.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT out_r) AS outgoing_edges
        }
        CALL {
            WITH s
            OPTIONAL MATCH (:Symbol)-[in_r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT|HAS_API|INHERITED_API]->(s)
            WHERE coalesce(in_r.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT in_r) AS incoming_edges
        }
        CALL {
            WITH s
            OPTIONAL MATCH (s)-[any_r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT|HAS_API|INHERITED_API]-(:Symbol)
            WHERE coalesce(any_r.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT any_r) AS total_edges
        }
        WITH s, f, c, outgoing_edges, incoming_edges, total_edges, direct_api, inherited_api
        ORDER BY
          CASE
            WHEN direct_api THEN 0
            WHEN inherited_api THEN 1
            ELSE 2
          END ASC,
          CASE
            WHEN f.path CONTAINS '/test/' OR f.path CONTAINS '/tests/'
              OR f.path CONTAINS '/integration/' OR f.path CONTAINS '/sample/'
              OR f.path CONTAINS '/samples/' THEN 1
            ELSE 0
          END ASC,
          total_edges DESC,
          outgoing_edges DESC,
          size(f.path) ASC
        LIMIT $limit
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS kind,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               outgoing_edges,
               incoming_edges,
               total_edges
        """
        try:
            with self.db.driver.session() as session:
                result = list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        class_name=class_name,
                        method_name=method_name,
                        qualified_suffix=qualified_suffix,
                        limit=64,
                    )
                )
        except Exception:
            return []
        return result

    def _load_module_target_candidate(self, symbol_name: str) -> dict | None:
        """Resolve package/module targets that are represented by files, not symbols.

        Python compatibility surfaces such as ``pydantic.v1`` are packages
        (`pydantic/v1/__init__.py`) rather than functions/classes, so the normal
        Symbol lookup cannot find them. Treat the package initializer as a
        synthetic primary target and let doc search + role backfill complete the
        context.
        """
        clean_name = (symbol_name or "").strip().replace(".", "/").strip("/")
        if not clean_name or any(part in {"", ".."} for part in clean_name.split("/")):
            return None

        package_init_suffix = f"/{clean_name}/__init__.py"
        module_suffix = f"/{clean_name}.py"
        query = """
        MATCH (f:File {workspace_id: $workspace_id})
        WHERE f.path ENDS WITH $package_init_suffix
           OR f.path ENDS WITH $module_suffix
        RETURN f.path AS path, coalesce(f.hash, '') AS file_hash
        ORDER BY
          CASE WHEN f.path ENDS WITH $package_init_suffix THEN 0 ELSE 1 END,
          size(f.path) ASC
        LIMIT 1
        """
        try:
            with self.db.driver.session() as session:
                row = session.run(
                    query,
                    workspace_id=self.workspace_id,
                    package_init_suffix=package_init_suffix,
                    module_suffix=module_suffix,
                ).single()
        except Exception:
            return None
        if not row:
            return None

        file_path = row.get("path") if hasattr(row, "get") else row["path"]
        file_hash = row.get("file_hash", "") if hasattr(row, "get") else row["file_hash"]
        end_line, token_estimate = self._module_target_size(
            file_path, self._workspace_project_root()
        )
        return {
            "uid": f"module:{self.workspace_id}:{file_path}",
            "name": symbol_name,
            "kind": "module",
            "qualified_name": clean_name.replace("/", "."),
            "token_estimate": token_estimate,
            "file_path": file_path,
            "file_hash": file_hash,
            "range": [1, end_line],
            "outgoing_edges": 0,
            "incoming_edges": 0,
            "total_edges": 0,
        }

    def concept_anchor_candidates(
        self,
        symbol_name: str,
        *,
        query: str = "",
        limit: int = 8,
    ) -> list[str]:
        return self.target_selector.concept_anchor_candidates(
            symbol_name,
            query=query,
            limit=limit,
        )

    def _load_concept_anchor_candidates(
        self,
        symbol_name: str,
        *,
        query: str = "",
        limit: int = 24,
    ) -> list[dict]:
        concept = (symbol_name or "").strip().lower()
        if not concept:
            return []
        terms = {concept}
        if concept.endswith("s") and len(concept) > 4:
            terms.add(concept[:-1])
        for term in self._query_terms(query):
            terms.add(term)
            if term.endswith("s") and len(term) > 4:
                terms.add(term[:-1])
        query_l = (query or "").lower()
        target_l = f"{concept} {query_l}"
        if "decorator" in target_l:
            terms.update({"metadata", "reflect"})
        if "module" in target_l and any(
            token in target_l
            for token in ("compose", "composition", "feature", "import", "provider", "controller")
        ):
            terms.update({"container", "metadata", "module", "registry", "scanner"})
        terms = {term for term in terms if len(term) >= 4}
        if not terms:
            return []

        query_limit = max(limit, min(400, limit * 10))
        query_cypher = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE any(term IN $terms
            WHERE toLower(s.name) CONTAINS term
               OR toLower(coalesce(s.qualified_name, '')) CONTAINS term)
        CALL {
            WITH s
            OPTIONAL MATCH (s)-[out_r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->(:Symbol)
            WHERE coalesce(out_r.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT out_r) AS outgoing_edges
        }
        CALL {
            WITH s
            OPTIONAL MATCH (:Symbol)-[in_r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->(s)
            WHERE coalesce(in_r.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT in_r) AS incoming_edges
        }
        CALL {
            WITH s
            OPTIONAL MATCH (s)-[any_r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]-(:Symbol)
            WHERE coalesce(any_r.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT any_r) AS total_edges
        }
        WITH s, f, c, outgoing_edges, incoming_edges, total_edges
        ORDER BY
          CASE
            WHEN f.path CONTAINS '/test/' OR f.path CONTAINS '/tests/'
              OR f.path CONTAINS '/integration/' OR f.path CONTAINS '/sample/'
              OR f.path CONTAINS '/samples/' THEN 1
            ELSE 0
          END ASC,
          total_edges DESC,
          outgoing_edges DESC,
          size(f.path) ASC
        LIMIT $limit
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS kind,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               outgoing_edges,
               incoming_edges,
               total_edges
        """
        try:
            with self.db.driver.session() as session:
                rows = list(
                    session.run(
                        query_cypher,
                        workspace_id=self.workspace_id,
                        terms=sorted(terms),
                        limit=query_limit,
                    )
                )
        except Exception:
            return []
        return [row for row in rows if not _path_is_noisy(row.get("file_path", ""))]

    @staticmethod
    def _module_target_size(file_path: str, workspace_root=None) -> tuple[int, int]:
        from sidecar.workspace_paths import resolve_graph_file_path

        safe_path = resolve_graph_file_path(file_path, workspace_root=workspace_root)
        if safe_path is None:
            return 80, 640
        try:
            with open(safe_path, encoding="utf-8") as handle:
                line_count = sum(1 for _ in handle)
        except OSError:
            line_count = 80
        end_line = max(1, min(line_count, 120))
        return end_line, max(1, end_line * 8)

    def _build_target_node(
        self,
        row: dict,
        *,
        provenance: list[str] | None = None,
    ) -> SubgraphNode:
        token_cost = row.get("token_estimate", 0) or self._estimate_tokens_range(
            row.get("range", [0, 0])
        )
        return SubgraphNode(
            uid=row["uid"],
            name=row["name"],
            file_path=row["file_path"],
            range=row.get("range") or [0, 0],
            token_estimate=token_cost,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
            kind=row.get("kind", ""),
            qualified_name=row.get("qualified_name", ""),
            file_hash=row.get("file_hash", ""),
            provenance=provenance or ["primary:target"],
            graph_score=1.0,
            blended_score=1.0,
        )

    def _score_target_candidate(
        self,
        row: dict,
        *,
        query: str = "",
        intent: Intent | None = None,
    ) -> tuple[float, dict]:
        file_path = row.get("file_path", "")
        kind = row.get("kind", "")
        total_edges = float(row.get("total_edges", 0) or 0)
        outgoing_edges = float(row.get("outgoing_edges", 0) or 0)
        incoming_edges = float(row.get("incoming_edges", 0) or 0)
        token_estimate = float(row.get("token_estimate", 0) or 0)
        role = self._infer_role(
            SubgraphNode(
                uid=row["uid"],
                name=row["name"],
                file_path=file_path,
                range=row.get("range") or [0, 0],
                token_estimate=int(token_estimate),
                relation="target",
                direction="primary",
                depth=0,
                relevance_score=1.0,
                kind=kind,
                qualified_name=row.get("qualified_name", ""),
            )
        )
        # Target selection is structural only: path / role / edges / kind / size /
        # how many other files import the enclosing file. file_imports_in is a
        # public-API signal independent of role classification: a thin wrapper in
        # `runtime-core/apiWatch.ts` (imported by 5 files) is structurally the
        # public watch entry, even when the cascade classified it as core_runtime
        # (0.55 role bonus) and the heavier implementation `reactivity/watch.ts`
        # (imported by 1 file) gets orchestrator (0.95). Without this signal the
        # +0.4 role gap on the implementation wins target selection and the
        # public wrapper's neighborhood — the file the question actually asks
        # about — never enters retrieval.
        file_imports_in = float(row.get("file_imports_in", 0) or 0)
        components = {
            "path": self._target_path_bonus(file_path),
            "role": self._target_role_bonus(role),
            "edges": min(1.4, 0.22 * outgoing_edges + 0.08 * incoming_edges + 0.05 * total_edges),
            "kind": self._target_kind_bonus(kind, intent=intent),
            "size_penalty": -min(0.6, token_estimate / 6000.0),
            "imports_in": min(0.3, 0.04 * file_imports_in),
        }
        score = sum(components.values())
        return score, {"role": role, "components": components}

    @staticmethod
    def _normalized_target_path(file_path: str) -> str:
        return (file_path or "").replace("\\", "/").lower()

    def _target_path_bonus(self, file_path: str) -> float:
        if not file_path:
            return 0.0
        if _path_is_noisy(file_path):
            return -5.0
        if "/docs/" in file_path or "/examples/" in file_path:
            return -0.4
        if "/__init__." in file_path:
            return 0.1
        # Primary package entry files rank above sibling modules when resolving
        # duplicate symbol matches (e.g. main.py beats root_model.py).
        file_lc = self._normalized_target_path(file_path)
        if any(file_lc.endswith(s) for s in ("/main.py", "/index.py", "/app.py", "/base.py")):
            return 0.55
        return 0.35

    def _target_role_bonus(self, role: str) -> float:
        # Roles below must cover every role the cascade can emit (see role_cascade.py:
        # L2_PREDICATES + L1_FALLBACK_ROLE). When the cascade gained new discriminators
        # (proxy_mechanism / registration_step / dependency_solver / request_router /
        # interceptor / composition_surface / abstract_contract / integration_surface)
        # they were not added here, so every duplicate-target with one of these roles
        # silently got the default 0.35 — losing to a same-name function with role
        # supporting_surface (also 0.40 default). Keep this table aligned to the
        # cascade vocabulary; an entry missing here is a target-selection regression.
        role_weights = {
            # entry / dispatch surfaces — strong target signals
            "api_surface": 1.2,
            "executor": 1.0,
            "orchestrator": 0.95,
            "request_router": 1.0,
            "registration_step": 0.95,
            "proxy_mechanism": 1.0,
            "interceptor": 0.95,
            "dependency_solver": 0.95,
            # builders / construction / handlers
            "construction_surface": 0.9,
            "validator_handle": 0.95,
            "serializer_handle": 0.9,
            "binding_surface": 0.9,
            "factory_surface": 0.8,
            "schema_builder": 0.75,
            "composition_surface": 0.8,
            # runtime / errors / contracts
            "runtime_surface": 0.85,
            "error_surface": 0.75,
            "integration_surface": 0.85,
            "abstract_contract": 0.65,
            "representation_surface": 0.6,
            "core_runtime": 0.55,
            "config_surface": 0.3,
            "compat_bridge": 0.45,
            "supporting_surface": 0.4,
            "orphan": -0.4,
            "docs_or_concept": -0.4,
        }
        return role_weights.get(role, 0.35)

    def _target_kind_bonus(self, kind: str, *, intent: Intent | None = None) -> float:
        if kind == "function":
            return 0.35 if intent != Intent.DESIGN_QUESTION else 0.2
        if kind == "class":
            return 0.1 if intent != Intent.EXPLORATION else 0.0
        # A proxy_binding node is only materialized for a recognized lazy-proxy
        # pattern (`x = LocalProxy(...)`); it always carries proxy_mechanism and is
        # the *whole* point of disambiguating against a same-name getter function.
        if kind == "proxy_binding":
            return 0.35
        return 0.0

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        return [
            term
            for term in re.findall(r"[a-z_]{4,}", query.lower())
            if term not in {"this", "that", "with", "what", "does", "called"}
        ]

    def rank(
        self,
        target: SubgraphNode,
        query: str,
        intent: Intent,
        budget: int,
        graph_pool_size: int = 200,
        vector_limit: int = 100,
        *,
        ambiguous: bool = False,
        secondary_intent: Intent | None = None,
        intent_policy: IntentPolicy | None = None,
    ) -> tuple[list[Candidate], dict, str, list[dict], list[str]]:
        """Return budget-fitting candidates sorted by blended score.

        Returns (candidates, budget_info).  The primary symbol itself is not
        in the returned list — the caller holds it separately.
        """
        intent_policy = self._normalize_intent_policy(
            intent,
            intent_policy=intent_policy,
            ambiguous=ambiguous,
            secondary_intent=secondary_intent,
        )

        # 1. Mechanism + required roles (drives role-filling tiers in scoring/selection).
        # Compute this once up front; structural contract candidate sourcing also
        # needs the role plan, so doing it later would repeat the local role
        # supply traversal for every request.
        mechanism = self._determine_mechanism(target, query=query)
        required_roles = self._get_required_roles(mechanism, target=target)
        if intent == Intent.IMPACT_ANALYSIS:
            required_roles = normalize_roles(
                [
                    "impact_runtime",
                    "impact_public_api",
                    "impact_test_surface",
                    "docs_or_concept",
                ]
            )
        required_roles = self._apply_intent_policy_roles(required_roles, intent_policy)

        # 2. Collect graph BFS candidates (pool-size-limited, not budget-limited).
        # The earlier `min(graph_pool_size, 96)` cap on non-impact intents
        # was intended to skip "low-utility tail" candidates that pruning
        # would discard. Empirically it instead clipped cross-package
        # canonical files: rtk_q01 (factory_surface carried by query
        # builders one package over) and rtk_q03 lost role_recall; vue_q04
        # apiWatch.ts in runtime-core became unreachable from the reactivity
        # seed. Removing the cap recovers those (+0.011 role / +0.019 file
        # aggregate) at the price of one regression (sqlalchemy_q04 file
        # 1.00 → 0.50: the wider pool lets a heavier non-canonical file
        # outscore the expected one in pruning). Net is a clear improvement;
        # the q04 displacement is a pruning/scoring problem, not a pool one.
        effective_graph_pool_size = graph_pool_size
        graph_pool = self._graph_candidates(
            target.uid,
            pool_size=effective_graph_pool_size,
            intent=intent,
            target=target,
            query=query,
        )
        mandatory_delegates = self._mandatory_delegation_callees(target)
        graph_pool.extend(mandatory_delegates)
        graph_pool.extend(self._mandatory_delegation_type_neighbors(mandatory_delegates))
        graph_pool.extend(
            self._structural_contract_candidates(target, required_roles=required_roles)
        )
        self._mark_api_relay_candidates(graph_pool)
        self._mark_query_api_callees(graph_pool, query)

        # 3. Collect vector candidates for docs and symbols
        doc_pool = self._doc_candidates(query, limit=vector_limit)
        sym_vec_pool = self._sym_vec_candidates(query, limit=vector_limit)

        # 4. Doc-bridge: semantic relationships static graph edges cannot see.
        # When a marker API and its runtime consumer are co-mentioned in the
        # same DocAnchor, the bridge surfaces the consumer even when no
        # Symbol→Symbol edge connects them. Seeds are the target plus any
        # strong graph hits.
        bridge_seeds = {target.uid} | {c.uid for c in graph_pool if c.graph_score > 0.5}
        excluded = {target.uid} | {c.uid for c in graph_pool}
        bridge_pool_h1 = self._doc_bridge_candidates(
            bridge_seeds, excluded, limit=30, hop_decay=1.0
        )

        # 3b. 2-hop bridge: disabled by default. In benchmarking (65/65 real-repo
        # pass rate), enabling it added noise in hub-heavy graphs (fastapi, pydantic)
        # where hop-2 seeds retrieved unrelated utility symbols. Re-enable by setting
        # RANKER_2HOP_BRIDGE=1 in the environment for evaluation.
        import os as _os

        if _os.getenv("RANKER_2HOP_BRIDGE"):
            seeds_h2 = {c.uid for c in bridge_pool_h1 if c.graph_score > 0.4}
            excluded_h2 = excluded | seeds_h2
            bridge_pool_h2 = self._doc_bridge_candidates(
                seeds_h2, excluded_h2, limit=20, hop_decay=0.5
            )
            bridge_pool = [*bridge_pool_h1, *bridge_pool_h2]
        else:
            bridge_pool = bridge_pool_h1

        # 5. Fuse into unified pool, boosting docs linked via COVERS
        pool = self._fuse(graph_pool, doc_pool, sym_vec_pool, target.uid, bridge_pool=bridge_pool)

        # 6. Fill missing token costs for vector-only symbols before we
        # decide whether a role is genuinely selection-ready.
        self._fill_token_costs(pool)

        # If retrieval produced no docs at all, synthesize one tiny concept
        # anchor from target metadata so docs_or_concept is not impossible.
        if not any(c.kind == "doc" for c in pool):
            fallback_doc = self._target_concept_fallback_candidate(target, query=query)
            if fallback_doc is not None:
                pool.append(fallback_doc)

        # 7. Assign intent weights and noise factors

        intent_priors = self._intent_priors_for_policy(intent, intent_policy)
        for c in pool:
            c.evidence_role = self._role_of(c)
            c.supporting_roles = self._supporting_roles_of(c)
            c.intent_weight = intent_priors.get(c.kind, 0.3)
            if intent == Intent.IMPACT_ANALYSIS:
                c.noise_factor = compute_impact_noise_factor(
                    c.file_path,
                    c.name,
                    query=query,
                    target_name=target.name,
                    kind=c.kind,
                    content=c.content,
                )
            else:
                c.noise_factor = compute_noise_factor(
                    c.file_path, c.name, kind=c.kind, intent=intent
                )
                c.noise_factor *= self._topic_focus_factor(
                    c,
                    target,
                    query=query,
                    mechanism=mechanism,
                    intent=intent,
                    required_roles=required_roles,
                )

        # 8. Normalize each track to [0, 1]
        self._normalize(pool)

        # 9. Sort by blended score, with priority for role-fillers that the
        # target doesn't already cover. A candidate that's the *only* source
        # of a required role must outrank docs that satisfy `docs_or_concept`
        # for free, even if its blended score is poor (e.g. `openapi` in
        # fastapi/applications.py is large with weak graph signal, but it's
        # the unique api_surface for openapi-generation).
        target_roles_set = set(self._roles_of(target))
        # `docs_or_concept` is trivially fillable by any doc; treating it as
        # an "unfilled required role" would let every doc claim the big bonus
        # and crowd out real role-fillers.
        unfilled_required = (set(required_roles) - target_roles_set) - {"docs_or_concept"}

        # Required roles minus the trivially-filled `docs_or_concept` — only
        # roles in this set deserve a sort-order bump, otherwise every doc
        # claims the bonus and the priority lift is meaningless.
        non_trivial_required = set(required_roles) - {"docs_or_concept"}

        def _sort_key(c: Candidate) -> tuple:
            base = self._blended(c)
            roles = set(self._roles_of(c))
            api_behavior_rank = self._api_behavior_sort_rank(c)
            query_focus_rank = self._query_api_focus_rank(c, query)
            is_subsystem_isolated = c.noise_factor < 0.2 and c.kind != "doc"
            if c.relation == "MANDATORY_CALLEE":
                is_contract_anchor = any(
                    (str(step).startswith("mandatory-") and str(step).endswith("-contract"))
                    for step in c.provenance
                )
                return (4 if is_contract_anchor else 3, query_focus_rank, api_behavior_rank, base)
            # Tier 0 (best): candidates that fill a missing required role the
            # target itself doesn't cover. These must beat raw doc-relevance
            # so a large/weak role-filler still seats before unrelated docs.
            if roles & unfilled_required:
                if is_subsystem_isolated:
                    return (0.5, query_focus_rank, api_behavior_rank, base)
                return (2, query_focus_rank, api_behavior_rank, base)
            if roles & non_trivial_required:
                return (1, query_focus_rank, api_behavior_rank, base)
            return (0, query_focus_rank, api_behavior_rank, base)

        pool.sort(key=_sort_key, reverse=True)

        policy_floor = self._intent_policy_floor(intent, intent_policy)
        doc_first = intent_policy.doc_first if intent_policy else False

        # 11. Optimal context selection (marginal gain + doc deferral)
        return self.budget_pruner.select_under_budget(
            pool,
            target,
            query,
            intent,
            mechanism,
            required_roles,
            budget,
            floor_override=policy_floor,
            doc_first=doc_first,
        )

    @staticmethod
    def _has_api_behavior_provenance(c: Candidate) -> bool:
        return any(str(step).startswith("api-behavior:") for step in c.provenance)

    @staticmethod
    def _has_call_graph_provenance(c: Candidate) -> bool:
        return any(str(step).startswith("graph:CALLS") for step in c.provenance)

    @staticmethod
    def _has_api_relay_provenance(c: Candidate) -> bool:
        return any(str(step).startswith("api-relay:") for step in c.provenance)

    @staticmethod
    def _api_relay_score(c: Candidate) -> int:
        for step in c.provenance:
            match = re.match(r"api-relay:in=(\d+),out=(\d+)", str(step))
            if match:
                return int(match.group(1)) * int(match.group(2))
        return 0

    def _query_mentions_candidate_name(self, c: Candidate, query: str) -> bool:
        name = (c.name or "").strip()
        if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            return False
        if len(name) < 4 and c.relation not in self._API_ENTRY_RELATIONS:
            return False
        return re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(name.lower())}(?![A-Za-z0-9_])",
            (query or "").lower(),
        ) is not None

    @staticmethod
    def _has_query_api_callee_provenance(c: Candidate) -> bool:
        return any(str(step).startswith("query-api-callee:") for step in c.provenance)

    def _query_api_focus_rank(self, c: Candidate, query: str) -> int:
        if self._query_mentions_candidate_name(c, query):
            return 2
        if self._has_query_api_callee_provenance(c):
            return 1
        return 0

    def _mark_api_relay_candidates(self, pool: list[Candidate]) -> None:
        api_candidates = [
            c for c in pool if c.kind == "symbol" and c.relation in self._API_ENTRY_RELATIONS
        ]
        if not api_candidates:
            return
        query_text = """
        UNWIND $uids AS uid
        MATCH (n:Symbol {uid: uid})
        CALL {
            WITH n
            OPTIONAL MATCH (api_owner:Symbol)-[:HAS_API|INHERITED_API {workspace_id: $workspace_id}]->(n)
            OPTIONAL MATCH (api_owner)-[:HAS_API|INHERITED_API {workspace_id: $workspace_id}]->(api_caller:Symbol)
                          -[api_in:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS]->(n)
            WHERE coalesce(api_in.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT api_in) AS api_sibling_call_in_count
        }
        CALL {
            WITH n
            OPTIONAL MATCH (api_owner:Symbol)-[:HAS_API|INHERITED_API {workspace_id: $workspace_id}]->(n)
            OPTIONAL MATCH (api_owner)-[:HAS_API|INHERITED_API {workspace_id: $workspace_id}]->(api_callee:Symbol)
                          <-[api_out:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS]-(n)
            WHERE coalesce(api_out.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT api_out) AS api_sibling_call_out_count
        }
        RETURN n.uid AS uid,
               api_sibling_call_in_count,
               api_sibling_call_out_count
        """
        try:
            with self.db.driver.session() as session:
                rows = list(
                    session.run(
                        query_text,
                        uids=[c.uid for c in api_candidates],
                        workspace_id=self.workspace_id,
                    )
                )
        except Exception:
            return
        relay_counts = {
            str(row["uid"]): (
                int(row["api_sibling_call_in_count"] or 0),
                int(row["api_sibling_call_out_count"] or 0),
            )
            for row in rows
            if row.get("uid")
        }
        for candidate in api_candidates:
            in_count, out_count = relay_counts.get(candidate.uid, (0, 0))
            if in_count <= 0 or out_count <= 0:
                continue
            behavior_step = f"api-behavior:outcalls={out_count}"
            if not self._has_api_behavior_provenance(candidate):
                candidate.provenance.append(behavior_step)
            relay_step = f"api-relay:in={in_count},out={out_count}"
            if relay_step not in candidate.provenance:
                candidate.provenance.append(relay_step)
            candidate.chain_kind = upgrade_chain_kind(candidate.chain_kind, "relay")

    def _mark_query_api_callees(self, pool: list[Candidate], query: str) -> None:
        caller_uids = [
            c.uid
            for c in pool
            if c.relation in self._API_ENTRY_RELATIONS
            and self._query_mentions_candidate_name(c, query)
        ]
        if not caller_uids:
            return
        for candidate in pool:
            if candidate.uid in caller_uids:
                if "query-api-seed" not in candidate.provenance:
                    candidate.provenance.append("query-api-seed")
                candidate.chain_kind = upgrade_chain_kind(candidate.chain_kind, "query_seed")
        query_text = """
        MATCH (caller:Symbol)-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS]->(callee:Symbol)
        WHERE caller.uid IN $caller_uids
          AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
        RETURN callee.uid AS uid, count(DISTINCT caller) AS caller_count
        """
        try:
            with self.db.driver.session() as session:
                rows = list(
                    session.run(
                        query_text,
                        caller_uids=caller_uids,
                        workspace_id=self.workspace_id,
                    )
                )
        except Exception:
            return
        callee_counts = {
            str(row["uid"]): int(row["caller_count"] or 0)
            for row in rows
            if row.get("uid")
        }
        if not callee_counts:
            return
        for candidate in pool:
            caller_count = callee_counts.get(candidate.uid)
            if not caller_count:
                continue
            step = f"query-api-callee:callers={caller_count}"
            if step not in candidate.provenance:
                candidate.provenance.append(step)
            candidate.chain_kind = upgrade_chain_kind(candidate.chain_kind, "api_callee")

    def _api_behavior_sort_rank(self, c: Candidate) -> int:
        if c.kind != "symbol" or c.relation not in self._API_ENTRY_RELATIONS:
            return 0
        relay_score = self._api_relay_score(c)
        if relay_score > 0:
            return min(99, 3 + relay_score)
        rank = 0
        if self._has_api_behavior_provenance(c):
            rank += 1
        if self._has_call_graph_provenance(c):
            rank += 1
        return rank

    def candidates_to_subgraph(
        self,
        target: SubgraphNode,
        candidates: list[Candidate],
        budget_info: dict,
        stopped_reason: str = "",
        pruned_details: list | None = None,
    ) -> tuple[Subgraph, list[DocChunk]]:
        return cast(
            tuple[Subgraph, list[DocChunk]],
            self.subgraph_assembler.candidates_to_subgraph(
                (target, candidates, budget_info, stopped_reason, pruned_details)
            ),
        )

    def _candidates_to_subgraph_impl(
        self,
        payload: tuple[SubgraphNode, list[Candidate], dict, str, list | None],
    ) -> tuple[Subgraph, list[DocChunk]]:
        """Split ranked candidates back into Subgraph + DocChunks for PromptCompiler."""
        target, candidates, budget_info, stopped_reason, pruned_details = payload
        nodes = []
        docs = []
        for c in candidates:
            if c.kind == "symbol":
                blended = self._blended(c)
                nodes.append(
                    SubgraphNode(
                        uid=c.uid,
                        name=c.name,
                        file_path=c.file_path,
                        range=c.range,
                        token_estimate=c.token_cost,
                        relation=c.relation or "related",
                        direction=c.direction or "sibling",
                        depth=c.depth,
                        relevance_score=blended,
                        kind=getattr(c, "symbol_kind", ""),
                        render_mode=c.render_mode,
                        file_hash=c.file_hash,
                        provenance=list(c.provenance),
                        graph_score=c.graph_score,
                        semantic_score=c.semantic_score,
                        blended_score=blended,
                        intent_weight=c.intent_weight,
                        chain_kind=c.chain_kind,
                    )
                )
            else:
                docs.append(
                    DocChunk(
                        source_file=c.file_path,
                        chunk_id=c.uid,
                        content=c.content,
                        score=self._blended(c),
                        graph_score=c.graph_score,
                        semantic_score=c.semantic_score,
                        blended_score=self._blended(c),
                        intent_weight=c.intent_weight,
                        provenance=c.provenance,
                        anchor_type=c.anchor_type,
                        anchor_confidence=c.anchor_confidence,
                        primary_bias=c.primary_bias,
                    )
                )
        return Subgraph(
            primary=target,
            nodes=nodes,
            budget=budget_info,
            stopped_reason=stopped_reason,
            pruned_details=pruned_details or [],
        ), docs

    # ------------------------------------------------------------------
    # Candidate collection
    # ------------------------------------------------------------------

    # Intents where following an outgoing call chain (A→B→C→D) is the
    # primary way to answer the question. For these we soften the
    # distance penalty along outgoing CALLS edges so the BFS reaches
    # depth 5-6 instead of decaying around depth 3.
    #
    # Derived from `INTENT_TRAVERSAL[i].chase_chains` so the dictionary
    # in intent_classifier.py is the single source of truth — when
    # `chase_chains` flips for an intent there, the ranker honours it
    # automatically (no second hand-maintained list to keep in lockstep).
    _CHAIN_PURSUIT_INTENTS = frozenset(
        intent for intent, shape in INTENT_TRAVERSAL.items() if shape.chase_chains
    )
    _API_ENTRY_RELATIONS = frozenset({"HAS_API", "INHERITED_API"})
    _ALIAS_ENTRY_RELATIONS = frozenset({"REFERENCES"})
    _REGISTRATION_CHAIN_RELATIONS = frozenset(
        {
            "CALLS",
            "CALLS_DIRECT",
            "CALLS_SCOPED",
            "CALLS_IMPORTED",
            "CALLS_DYNAMIC",
            "CALLS_INFERRED",
            "CALLS_GUESS",
            "USES_TYPE",
            "INJECTS",
            "INSTANTIATES",
            "HANDLES",
            "RESOLVES_ATTR",
            "HAS_API",
            "INHERITED_API",
        }
    )
    _MARKER_CHAIN_RELATIONS = _REGISTRATION_CHAIN_RELATIONS
    _DELEGATION_CALL_RELATIONS = frozenset(
        {
            "CALLS",
            "CALLS_DIRECT",
            "CALLS_SCOPED",
            "CALLS_IMPORTED",
            "CALLS_DYNAMIC",
            "CALLS_INFERRED",
        }
    )
    _THIN_DELEGATOR_TOKEN_MAX = 96
    _THIN_DELEGATOR_MAX_OUT_CALLS = 2

    def _graph_candidates(
        self,
        target_uid: str,
        pool_size: int,
        intent: Intent | None = None,
        *,
        target: SubgraphNode | None = None,
        query: str = "",
    ) -> list[Candidate]:
        return cast(
            list[Candidate],
            self.graph_candidate_source.graph_candidates(
                target_uid,
                pool_size,
                intent=intent,
                target=target,
                query=query,
            ),
        )

    def _mandatory_delegation_callees(self, target: SubgraphNode) -> list[Candidate]:
        """Seat implementation callees for tiny facade/delegator targets.

        A short public wrapper can be the correct lookup hit while the actual
        behavior lives in its direct outgoing callee. The graph edge is already
        code-derived; this only prevents the large implementation body from
        being scored as disposable noise.
        """
        if not target.uid:
            return []
        target_kind = (target.kind or "").lower()
        if target_kind and target_kind not in {"function", "method"}:
            return []
        if not target.token_estimate or target.token_estimate > self._THIN_DELEGATOR_TOKEN_MAX:
            return []

        query = """
        MATCH (t:Symbol {uid: $uid})
        WHERE coalesce(t.token_estimate, 0) <= $max_tokens
          AND coalesce(t.kind, '') IN ['function', 'method']
        MATCH (t)-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED]->(n:Symbol)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
        WITH collect({rel: r, node: n}) AS rows, count(DISTINCT n) AS out_count
        WHERE out_count > 0 AND out_count <= $max_out_calls
        UNWIND rows AS row
        WITH row.rel AS r, row.node AS n
        OPTIONAL MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(n)
        RETURN DISTINCT n.uid AS uid,
               n.name AS name,
               coalesce(n.kind, '') AS symbol_kind,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(n.token_estimate, 0) AS token_estimate,
               coalesce(c.range, n.range, [0, 0]) AS range,
               type(r) AS rel_type
        LIMIT 8
        """
        try:
            with self.db.driver.session() as session:
                rows = list(
                    session.run(
                        query,
                        uid=target.uid,
                        workspace_id=self.workspace_id,
                        max_tokens=self._THIN_DELEGATOR_TOKEN_MAX,
                        max_out_calls=self._THIN_DELEGATOR_MAX_OUT_CALLS,
                    )
                )
        except Exception:
            return []

        candidates: list[Candidate] = []
        for row in rows:
            uid = row.get("uid")
            if not uid or uid == target.uid:
                continue
            rel_type = str(row.get("rel_type") or "CALLS")
            if rel_type not in self._DELEGATION_CALL_RELATIONS:
                continue
            token_cost = int(row.get("token_estimate") or 0)
            range_ = row.get("range") or [0, 0]
            if token_cost <= 0:
                token_cost = self._estimate_tokens_range(range_)
            candidates.append(
                Candidate(
                    kind="symbol",
                    uid=str(uid),
                    token_cost=token_cost,
                    graph_score=1.25,
                    name=str(row.get("name") or ""),
                    symbol_kind=str(row.get("symbol_kind") or ""),
                    file_path=str(row.get("file_path") or "<unknown>"),
                    range=range_,
                    relation="MANDATORY_CALLEE",
                    direction="callee",
                    depth=1,
                    file_hash=str(row.get("file_hash") or ""),
                    provenance=[
                        f"graph:{rel_type},depth=1,delegation",
                        "mandatory-delegation-callee",
                    ],
                    chain_kind="mandatory",
                )
            )
        return candidates

    def _mandatory_delegation_type_neighbors(self, delegates: list[Candidate]) -> list[Candidate]:
        """Surface type-contract neighbors of mandatory delegation callees."""
        delegate_uids = [c.uid for c in delegates if c.uid]
        if not delegate_uids:
            return []

        query = """
        UNWIND $delegate_uids AS delegate_uid
        MATCH (delegate:Symbol {uid: delegate_uid})-[r:USES_TYPE]->(n:Symbol)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
          AND n.uid <> delegate_uid
        OPTIONAL MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(n)
        RETURN DISTINCT n.uid AS uid,
               n.name AS name,
               coalesce(n.kind, '') AS symbol_kind,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(n.token_estimate, 0) AS token_estimate,
               coalesce(c.range, n.range, [0, 0]) AS range
        LIMIT 16
        """
        try:
            with self.db.driver.session() as session:
                rows = list(
                    session.run(
                        query,
                        delegate_uids=delegate_uids,
                        workspace_id=self.workspace_id,
                    )
                )
        except Exception:
            return []

        candidates: list[Candidate] = []
        for row in rows:
            uid = row.get("uid")
            if not uid:
                continue
            token_estimate = int(row.get("token_estimate") or 0)
            range_ = row.get("range") or [0, 0]
            if token_estimate <= 0:
                token_estimate = self._estimate_tokens_range(range_)
            compact = token_estimate > 512
            candidates.append(
                Candidate(
                    kind="symbol",
                    uid=str(uid),
                    token_cost=120 if compact else token_estimate,
                    graph_score=0.95,
                    name=str(row.get("name") or ""),
                    symbol_kind=str(row.get("symbol_kind") or ""),
                    file_path=str(row.get("file_path") or "<unknown>"),
                    range=range_,
                    render_mode="signature_only" if compact else "full",
                    relation="USES_TYPE",
                    direction="type",
                    depth=2,
                    file_hash=str(row.get("file_hash") or ""),
                    provenance=["graph:USES_TYPE,depth=2,delegation_surface"],
                    chain_kind="mandatory",
                )
            )
        return candidates

    def _structural_contract_neighbor_rows(
        self,
        uid: str,
        *,
        visited: set[str],
        limit: int = 32,
    ) -> list[dict]:
        """Outgoing structural-contract neighbors for role-filler closure.

        This is deliberately narrower than generic graph BFS: only code-derived
        contract/topology relations, and only outgoing from the current symbol.
        It gives the selector a compact path to role-bearing collaborators
        without reopening the high-fan incoming CALLS flood.
        """
        query = """
        MATCH (s:Symbol {uid: $uid})-[r:USES_TYPE|INSTANTIATES|DEPENDS_ON|HANDLES|HAS_API|INHERITED_API]->(n:Symbol)
        WHERE NOT n.uid IN $visited
          AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
        OPTIONAL MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(n)
        RETURN n.uid AS uid,
               n.name AS name,
               coalesce(n.kind, '') AS symbol_kind,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(n.token_estimate, 0) AS token_estimate,
               coalesce(c.range, n.range, [0, 0]) AS range,
               type(r) AS rel_type,
               coalesce(n.derived_primary_role, '') AS primary_role,
               coalesce(n.derived_supporting_roles_json, '') AS supporting_roles_json
        ORDER BY CASE type(r)
            WHEN 'USES_TYPE' THEN 0
            WHEN 'INSTANTIATES' THEN 1
            WHEN 'HANDLES' THEN 2
            WHEN 'HAS_API' THEN 3
            WHEN 'INHERITED_API' THEN 4
            WHEN 'DEPENDS_ON' THEN 5
            ELSE 6
        END,
        coalesce(n.token_estimate, 0) ASC
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                rows = session.run(
                    query,
                    uid=uid,
                    visited=list(visited),
                    workspace_id=self.workspace_id,
                    limit=limit,
                )
                return [dict(row) for row in rows]
        except Exception:
            return []

    def _structural_contract_neighbor_rows_by_parent(
        self,
        uids: list[str],
        *,
        visited: set[str],
        limit: int = 32,
    ) -> dict[str, list[dict]]:
        """Batch variant of ``_structural_contract_neighbor_rows``.

        One query per frontier depth keeps contract closure bounded by depth,
        not by ``frontier_size * per-node-query`` latency. For single-node
        frontiers we preserve the simpler query path, which also keeps the
        unit-test fakes compact.
        """
        unique_uids = [uid for uid in dict.fromkeys(uids) if uid]
        if not unique_uids:
            return {}
        if len(unique_uids) == 1:
            return {
                unique_uids[0]: self._structural_contract_neighbor_rows(
                    unique_uids[0],
                    visited=visited,
                    limit=limit,
                )
            }

        query = """
        UNWIND $uids AS parent_uid
        MATCH (s:Symbol {uid: parent_uid})
        CALL {
            WITH s
            MATCH (s)-[r:USES_TYPE|INSTANTIATES|DEPENDS_ON|HANDLES|HAS_API|INHERITED_API]->(n:Symbol)
            WHERE NOT n.uid IN $visited
              AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
            OPTIONAL MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(n)
            RETURN n.uid AS neighbor_uid,
                   n.name AS name,
                   coalesce(n.kind, '') AS symbol_kind,
                   coalesce(f.path, '<unknown>') AS file_path,
                   coalesce(f.hash, '') AS file_hash,
                   coalesce(n.token_estimate, 0) AS token_estimate,
                   coalesce(c.range, n.range, [0, 0]) AS range,
                   type(r) AS rel_type,
                   coalesce(n.derived_primary_role, '') AS primary_role,
                   coalesce(n.derived_supporting_roles_json, '') AS supporting_roles_json
            ORDER BY CASE type(r)
                WHEN 'USES_TYPE' THEN 0
                WHEN 'INSTANTIATES' THEN 1
                WHEN 'HANDLES' THEN 2
                WHEN 'HAS_API' THEN 3
                WHEN 'INHERITED_API' THEN 4
                WHEN 'DEPENDS_ON' THEN 5
                ELSE 6
            END,
            coalesce(n.token_estimate, 0) ASC
            LIMIT $limit
        }
        RETURN s.uid AS parent_uid,
               neighbor_uid AS uid,
               name,
               symbol_kind,
               file_path,
               file_hash,
               token_estimate,
               range,
               rel_type,
               primary_role,
               supporting_roles_json
        """
        grouped: dict[str, list[dict]] = {uid: [] for uid in unique_uids}
        try:
            with self.db.driver.session() as session:
                rows = session.run(
                    query,
                    uids=unique_uids,
                    visited=list(visited),
                    workspace_id=self.workspace_id,
                    limit=limit,
                )
                for row in rows:
                    parent_uid = str(row.get("parent_uid") or "")
                    if parent_uid not in grouped:
                        continue
                    grouped[parent_uid].append(dict(row))
        except Exception:
            return {
                uid: self._structural_contract_neighbor_rows(
                    uid,
                    visited=visited,
                    limit=limit,
                )
                for uid in unique_uids
            }
        return grouped

    def _structural_contract_candidates(
        self,
        target: SubgraphNode,
        *,
        required_roles: list[str] | None = None,
        max_depth: int = 5,
        per_node_limit: int = 32,
        per_role_limit: int = 4,
        total_limit: int = 48,
    ) -> list[Candidate]:
        """Role-bearing candidates from the target's structural closure.

        Generic BFS is score-limited and can be saturated by high-fan callers
        before it reaches a local contract path. The adaptive role planner
        already observes those paths; this source turns the same structural
        facts into selectable candidates. Selection remains role/topology based:
        no query text, symbol names, file stems, benchmark IDs, or framework
        packs author these candidates.
        """
        if not target.uid:
            return []

        target_roles = set(self.role_fulfilment.roles_of(target))
        role_plan = (
            normalize_roles(required_roles)
            if required_roles is not None
            else self.role_fulfilment.adaptive_role_plan(target=target)
        )
        desired_roles = [
            role
            for role in role_plan
            if role not in target_roles
            and role
            not in {
                "docs_or_concept",
                "impact_runtime",
                "impact_public_api",
                "impact_test_surface",
                "orphan",
            }
        ]
        if not desired_roles:
            return []

        desired = set(desired_roles)
        role_order = {role: idx for idx, role in enumerate(desired_roles)}
        seen: set[str] = {target.uid}
        frontier: list[str] = [target.uid]
        rows_by_uid: dict[str, tuple[dict, int, list[str]]] = {}

        for depth in range(1, max_depth + 1):
            if not frontier:
                break
            next_frontier: list[str] = []
            rows_by_parent = self._structural_contract_neighbor_rows_by_parent(
                frontier,
                visited=seen,
                limit=per_node_limit,
            )
            for uid in frontier:
                for row in rows_by_parent.get(uid, []):
                    neighbor_uid = str(row.get("uid") or "")
                    if not neighbor_uid or neighbor_uid in seen:
                        continue
                    seen.add(neighbor_uid)
                    next_frontier.append(neighbor_uid)

                    roles = self.role_fulfilment.pass1_roles_for_symbol_uid(neighbor_uid)
                    if not roles or not (set(roles) & desired):
                        continue
                    file_path = str(row.get("file_path") or "")
                    if _path_is_noisy(file_path):
                        continue
                    rows_by_uid.setdefault(neighbor_uid, (row, depth, roles))
            frontier = next_frontier

        ranked_rows: list[tuple[tuple, dict, int, list[str], list[str]]] = []
        for row, depth, roles in rows_by_uid.values():
            matched = [role for role in desired_roles if role in roles]
            if not matched:
                continue
            primary = normalize_roles([str(row.get("primary_role") or "")])
            primary_match = bool(primary and primary[0] in matched)
            token_estimate = int(row.get("token_estimate") or 0)
            rank_key = (
                0 if primary_match else 1,
                min(role_order[role] for role in matched),
                depth,
                token_estimate,
            )
            ranked_rows.append((rank_key, row, depth, roles, matched))

        ranked_rows.sort(key=lambda item: item[0])

        selected: list[Candidate] = []
        selected_uids: set[str] = set()
        role_counts: Counter[str] = Counter()
        for _, row, depth, _roles, matched in ranked_rows:
            uid = str(row.get("uid") or "")
            if not uid or uid in selected_uids:
                continue
            if all(role_counts[role] >= per_role_limit for role in matched):
                continue

            token_cost = int(row.get("token_estimate") or 0)
            range_ = row.get("range") or [0, 0]
            if token_cost <= 0:
                token_cost = self._estimate_tokens_range(range_)
            compact = token_cost > 512
            rel_type = str(row.get("rel_type") or "USES_TYPE")
            graph_score = max(0.45, 1.35 - (0.12 * depth))
            selected.append(
                Candidate(
                    kind="symbol",
                    uid=uid,
                    token_cost=120 if compact else token_cost,
                    graph_score=graph_score,
                    name=str(row.get("name") or ""),
                    symbol_kind=str(row.get("symbol_kind") or ""),
                    file_path=str(row.get("file_path") or "<unknown>"),
                    range=range_,
                    render_mode="signature_only" if compact else "full",
                    relation=rel_type,
                    direction=self._direction(rel_type, True),
                    depth=depth,
                    file_hash=str(row.get("file_hash") or ""),
                    provenance=[
                        f"graph:{rel_type},depth={depth},contract_chain",
                        f"structural-contract:roles={','.join(matched)}",
                    ],
                    chain_kind="contract",
                )
            )
            selected_uids.add(uid)
            for role in matched:
                role_counts[role] += 1
            if len(selected) >= total_limit:
                break

        return selected

    def _graph_candidates_impl(
        self,
        target_uid: str,
        pool_size: int,
        intent: Intent | None = None,
        *,
        target: SubgraphNode | None = None,
        query: str = "",
    ) -> list[Candidate]:
        """BFS from target, collecting up to pool_size candidates without token budget.

        When ``intent`` is in ``_CHAIN_PURSUIT_INTENTS`` and the edge being
        traversed is an outgoing CALLS_* edge, the distance penalty is
        cut so the chain can be followed deeper. Other edges and other
        intents keep the original scoring.

        For the same intents, a class target's outgoing ``HAS_API`` /
        ``INHERITED_API`` hops start a *registration chain*: subsequent
        outgoing ``CALLS_*``, ``USES_TYPE``, and ``INJECTS`` hops keep the
        softened penalty so ``Class → api_route → APIRoute`` survives BFS
        pruning even when the artifact class is large.

        For sync/async endpoint execution questions, an *execution chain*
        softens distance/token along runtime handler hops (including large
        ``get_request_handler`` reached via ``USES_TYPE`` in the same module).

        For CommonJS / barrel-style object facades, an outgoing ``REFERENCES``
        hop can start the same chain: ``exports.response = res`` reaches the
        real owner object, then outgoing ``HAS_API`` exposes the owner's methods.

        ``chase_chains`` is derived per-question from the question shape
        (see ``modulate_shape``): the base intent's ``chase_chains`` flag is
        toggled on when the query carries a flow verb (``resolve`` /
        ``dispatch`` / ``flow``) or names two or more components. So an
        EXPLORATION question "How does X work?" stays at the base shape
        (no chain pursuit) while "How does X resolve Y to Z?" turns it on.
        """
        if intent is None:
            chain_pursuit = False
            max_depth: int | None = None
            direction_filter: tuple[str, ...] | None = None
        else:
            q_shape = extract_question_shape(query) if query else QuestionShape()
            effective = modulate_shape(intent, q_shape)
            chain_pursuit = effective.chase_chains
            max_depth = effective.max_depth
            direction_filter = effective.direction

        def _direction_keeps(outgoing: bool) -> bool:
            if direction_filter is None:
                return True
            if "self" in direction_filter:
                return False  # ("self",) — don't walk at all
            if outgoing and "forward" in direction_filter:
                return True
            return not outgoing and "backward" in direction_filter
        visited = {target_uid}
        candidates: list[Candidate] = []
        pending_provenance: dict[str, list[str]] = {}
        # Tuple shape:
        # (-score, push_seq, uid, neighbor_dict, rel_type, outgoing, distance,
        #  reg_chain, marker_chain)
        frontier: list[tuple[float, int, str, dict, str, bool, int, bool, bool]] = []
        push_seq = 0

        def _provenance_steps(
            neighbor: dict,
            rel_type: str,
            outgoing: bool,
            distance: int,
            reg_chain: bool,
            marker_chain: bool,
        ) -> list[str]:
            chain_tag = ""
            if marker_chain:
                chain_tag = ",marker_chain"
            elif chain_pursuit and self._is_outgoing_call(rel_type, outgoing):
                chain_tag = ",chain"
            elif reg_chain:
                chain_tag = ",reg_chain"
            steps = [f"graph:{rel_type},depth={distance}{chain_tag}"]
            if (
                rel_type in self._API_ENTRY_RELATIONS
                and int(neighbor.get("outgoing_call_count") or 0) > 0
            ):
                steps.append(f"api-behavior:outcalls={int(neighbor['outgoing_call_count'])}")
            return steps

        def _remember_provenance(uid: str, steps: list[str]) -> None:
            remembered = pending_provenance.setdefault(uid, [])
            for step in steps:
                if step not in remembered:
                    remembered.append(step)

        initial_neighbors = [
            *self._get_neighbors(target_uid, visited, distance=1),
            *self._get_external_coref_neighbors(target_uid, visited, limit=12),
            *self._get_integrates_with_neighbors(target_uid, visited, limit=8),
        ]
        for n in initial_neighbors:
            if not _direction_keeps(n["outgoing"]):
                continue
            reg_chain = chain_pursuit and (
                self._is_api_entry_edge(n["rel_type"], n["outgoing"])
                or self._is_alias_entry_edge(n["rel_type"], n["outgoing"])
            )
            marker_chain = (
                chain_pursuit
                and self._is_marker_surface_uid(target_uid)
                and self._is_marker_consumer_edge(n["rel_type"], n["outgoing"])
            )
            score = self._raw_graph_score(
                n,
                distance=1,
                chain_pursuit=chain_pursuit,
                registration_chain=reg_chain or marker_chain,
            ) * intent_edge_boost(intent, n["rel_type"])
            _remember_provenance(
                n["uid"],
                _provenance_steps(n, n["rel_type"], n["outgoing"], 1, reg_chain, marker_chain),
            )
            heappush(
                frontier,
                (
                    -score,
                    push_seq,
                    n["uid"],
                    n,
                    n["rel_type"],
                    n["outgoing"],
                    1,
                    reg_chain,
                    marker_chain,
                ),
            )
            push_seq += 1

        while frontier and len(candidates) < pool_size:
            (
                neg_score,
                _seq,
                uid,
                neighbor,
                rel_type,
                outgoing,
                distance,
                reg_chain,
                marker_chain,
            ) = heappop(frontier)
            score = -neg_score
            if uid in visited:
                continue
            visited.add(uid)

            token_cost = neighbor.get("token_estimate", 0) or self._estimate_tokens_range(
                neighbor.get("range", [0, 0])
            )
            provenance = pending_provenance.pop(uid, None) or _provenance_steps(
                neighbor,
                rel_type,
                outgoing,
                distance,
                reg_chain,
                marker_chain,
            )
            c = Candidate(
                kind="symbol",
                uid=uid,
                token_cost=token_cost,
                graph_score=score,
                name=neighbor["name"],
                symbol_kind=neighbor.get("symbol_kind", ""),
                file_path=neighbor["file_path"],
                range=neighbor.get("range", [0, 0]),
                relation=rel_type,
                direction=self._direction(rel_type, outgoing),
                depth=distance,
                file_hash=neighbor.get("file_hash", ""),
                provenance=provenance,
                chain_kind="registration" if (reg_chain or marker_chain) else "",
            )
            if c.relation == "MANDATORY_CALLEE":
                c.chain_kind = upgrade_chain_kind(c.chain_kind, "mandatory")
            candidates.append(c)

            if (
                intent != Intent.IMPACT_ANALYSIS
                and _path_is_noisy(c.file_path)
                and not (reg_chain or marker_chain)
            ):
                continue

            # INTEGRATES_COREF is terminal: the candidate represents a sibling
            # file the seed only reaches via shared external imports. Letting
            # BFS walk that symbol's structural neighbours would drag in the
            # entire sibling's call cone — fine for tightly-scoped fastapi
            # questions where the integrating file is small (concurrency.py),
            # catastrophic for celery where every internal module is a coref
            # partner of every other and the cone overlaps the pool budget.
            if rel_type == "INTEGRATES_COREF":
                continue

            for nn in self._get_neighbors(uid, visited, distance=distance + 1):
                # Direction filter is intentionally applied only at distance 1
                # (the first hop from the target above). The "environment" of
                # a symbol — its owner class, siblings via HAS_API, subclasses
                # via INHERITED_API, the framework dispatcher that registered
                # it — often sits at the far end of a *forward* hop from an
                # intermediate container, so a rigid per-edge filter cuts off
                # exactly the paths an IMPACT_ANALYSIS asks for. Past the
                # first hop we let BFS walk freely; the first hop's direction
                # has already set the orientation.
                child_reg_chain = chain_pursuit and reg_chain and self._is_registration_chain_edge(
                    nn["rel_type"],
                    nn["outgoing"],
                )
                child_marker_chain = chain_pursuit and (
                    (
                        marker_chain
                        and self._is_marker_chain_edge(nn["rel_type"], nn["outgoing"])
                    )
                    or (
                        self._is_marker_surface_uid(uid)
                        and self._is_marker_consumer_edge(nn["rel_type"], nn["outgoing"])
                    )
                )
                # Per-intent max_depth ceiling. The intent's TraversalShape
                # declares how far the answer should reasonably reach (2 for
                # NAVIGATION's surface lookup, 4 for EXPLORATION's mechanism
                # explain, 10 for IMPACT_ANALYSIS's transitive ripple). A
                # registration / marker chain edge stays allowed beyond the
                # ceiling so a deep-but-on-chain step (e.g. a registration
                # hop reached past the configured depth) can still surface.
                if (
                    max_depth is not None
                    and distance + 1 > max_depth
                    and not (child_reg_chain or child_marker_chain)
                ):
                    continue
                ns = self._raw_graph_score(
                    nn,
                    distance=distance + 1,
                    chain_pursuit=chain_pursuit,
                    registration_chain=child_reg_chain or child_marker_chain,
                ) * intent_edge_boost(intent, nn["rel_type"])
                _remember_provenance(
                    nn["uid"],
                    _provenance_steps(
                        nn,
                        nn["rel_type"],
                        nn["outgoing"],
                        distance + 1,
                        child_reg_chain,
                        child_marker_chain,
                    ),
                )
                heappush(
                    frontier,
                    (
                        -ns,
                        push_seq,
                        nn["uid"],
                        nn,
                        nn["rel_type"],
                        nn["outgoing"],
                        distance + 1,
                        child_reg_chain,
                        child_marker_chain,
                    ),
                )
                push_seq += 1

        return candidates

    @staticmethod
    def _is_outgoing_call(rel_type: str, outgoing: bool) -> bool:
        return outgoing and rel_type in (
            "CALLS",
            "CALLS_DIRECT",
            "CALLS_SCOPED",
            "CALLS_IMPORTED",
            "CALLS_DYNAMIC",
            "CALLS_INFERRED",
            "CALLS_GUESS",
        )

    @classmethod
    def _is_api_entry_edge(cls, rel_type: str, outgoing: bool) -> bool:
        return outgoing and rel_type in cls._API_ENTRY_RELATIONS

    @classmethod
    def _is_alias_entry_edge(cls, rel_type: str, outgoing: bool) -> bool:
        return outgoing and rel_type in cls._ALIAS_ENTRY_RELATIONS

    @classmethod
    def _is_registration_chain_edge(cls, rel_type: str, outgoing: bool) -> bool:
        return outgoing and rel_type in cls._REGISTRATION_CHAIN_RELATIONS

    @staticmethod
    def _is_marker_consumer_edge(rel_type: str, outgoing: bool) -> bool:
        return rel_type == "USES_TYPE" and not outgoing

    @classmethod
    def _is_marker_chain_edge(cls, rel_type: str, outgoing: bool) -> bool:
        if rel_type == "USES_TYPE":
            return outgoing
        return outgoing and rel_type in cls._MARKER_CHAIN_RELATIONS

    def _is_marker_surface_uid(self, uid: str) -> bool:
        roles = set(self.role_fulfilment.pass1_roles_for_symbol_uid(uid))
        if not roles:
            return False
        if not roles & {"api_surface", "config_surface", "representation_surface"}:
            return False
        fan = self._structural_fan_by_uid.get(uid, {})
        call_fan_out = float(fan.get("call_fan_out", 0.0) or 0.0)
        type_fan_in = float(fan.get("type_fan_in", 0.0) or 0.0)
        return call_fan_out <= 1.5 and (
            type_fan_in > 0.0 or bool(roles & {"api_surface", "config_surface"})
        )

    def _doc_candidates(self, query: str, limit: int) -> list[Candidate]:
        return cast(list[Candidate], self.vector_candidate_source.doc_candidates(query, limit))

    def _doc_candidates_impl(self, query: str, limit: int) -> list[Candidate]:
        raw = self._filter_doc_hits_to_workspace(
            self.vector.search_docs(query, limit=limit, workspace_id=self.workspace_id)
        )
        return [
            Candidate(
                kind="doc",
                uid=r["chunk_id"],
                token_cost=max(1, len(r["content"]) // 4),
                semantic_score=r["score"],
                name=r["chunk_id"],
                file_path=r["file_path"],
                content=r["content"],
                provenance=[f"vector:docs,sim={r['score']:.2f}"],
            )
            for r in raw
        ]

    def _target_concept_fallback_candidate(
        self,
        target: SubgraphNode,
        *,
        query: str,
    ) -> Candidate | None:
        """Tiny pseudo-doc fallback when no real docs are retrievable."""
        name = (target.name or "").strip()
        file_path = (target.file_path or "").strip()
        if not name and not file_path:
            return None
        summary = f"Concept note: {name} ({file_path})"
        if query:
            summary = f"{summary}. Query focus: {query[:220]}"
        return Candidate(
            kind="doc",
            uid=f"doc-fallback:{target.uid or name or 'target'}",
            token_cost=90,
            semantic_score=0.22,
            name=f"{name or 'target'}:concept",
            file_path=file_path or "<unknown>",
            content=summary,
            provenance=["fallback:target-concept-note"],
        )

    def _sym_vec_candidates(self, query: str, limit: int) -> list[Candidate]:
        return cast(list[Candidate], self.vector_candidate_source.sym_vec_candidates(query, limit))

    def _sym_vec_candidates_impl(self, query: str, limit: int) -> list[Candidate]:
        raw = self._filter_symbol_hits_to_workspace(
            self.vector.search_symbols(query, limit=limit, workspace_id=self.workspace_id)
        )
        return [
            Candidate(
                kind="symbol",
                uid=r["uid"],
                token_cost=0,  # filled later by _fill_token_costs
                semantic_score=r["score"],
                name=r["name"],
                file_path=r["file_path"],
                provenance=[f"vector:sym,sim={r['score']:.2f}"],
            )
            for r in raw
        ]

    def _doc_bridge_candidates(
        self,
        seed_uids: set[str],
        excluded: set[str],
        limit: int = 15,
        min_strength: int = 1,
        hop_decay: float = 1.0,
    ) -> list[Candidate]:
        """Symbols co-mentioned with seeds in the same DocAnchor(s).

        Static call/depends edges miss semantic relationships between marker
        APIs and runtime consumers. Doc anchors already record these by name
        when ``_extract_identifiers`` saw both names in the same chunk and
        ``COVERS`` was created for each.

        ``min_strength`` filters out single-anchor co-occurrences where the
        co-mention is more likely incidental than a real semantic link.
        Default 1 keeps everything; raise to 2 to cut single-mention noise.

        ``hop_decay`` multiplies the resulting graph_score. Use 1.0 for the
        first hop (target's direct doc-siblings), and lower values like
        0.5 for a transitive second hop where the bridge is weaker.

        Returns symbol candidates whose ``graph_score`` reflects how
        strongly they co-occur with the seeds (number of distinct
        anchors). Token cost is filled later by ``_fill_token_costs``.
        """
        if not seed_uids:
            return []
        query = """
        MATCH (a:DocAnchor)-[seed_edge:COVERS]->(s:Symbol)
        WHERE s.uid IN $seed_uids
          AND coalesce(a.workspace_id, $workspace_id) = $workspace_id
        MATCH (a)-[other_edge:COVERS]->(other:Symbol)
        WHERE NOT other.uid IN $excluded
        OPTIONAL MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(other)
        WITH other,
             coalesce(f.path, '<unknown>') AS file_path,
             coalesce(c.range, other.range, [0, 0]) AS range,
             coalesce(other.token_estimate, 0) AS token_estimate,
             coalesce(f.hash, '') AS file_hash,
             count(DISTINCT a) AS bridge_strength,
             max(
                coalesce(other_edge.confidence, seed_edge.confidence, 0.65)
                * coalesce(other_edge.primary_bias, seed_edge.primary_bias, 0.7)
                * CASE coalesce(other_edge.anchor_type, seed_edge.anchor_type, 'reference')
                    WHEN 'definition' THEN 1.0
                    WHEN 'warning' THEN 0.95
                    WHEN 'deprecated' THEN 0.85
                    WHEN 'example' THEN 0.45
                    ELSE 0.65
                  END
             ) AS anchor_quality
        WHERE bridge_strength >= $min_strength
        RETURN other.uid AS uid,
               other.name AS name,
               file_path,
               range,
               token_estimate,
               file_hash,
               bridge_strength,
               anchor_quality
        ORDER BY bridge_strength DESC, anchor_quality DESC
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                rows = list(
                    session.run(
                        query,
                        seed_uids=list(seed_uids),
                        excluded=list(excluded),
                        workspace_id=self.workspace_id,
                        limit=limit,
                        min_strength=min_strength,
                    )
                )
        except Exception:
            return []

        candidates = []
        for r in rows:
            strength = int(r["bridge_strength"])
            # log1p so 1 anchor → 0.69, 3 anchors → 1.39, 10 → 2.40 (pre-norm).
            # hop_decay shrinks the contribution for transitive (2-hop) bridges.
            quality = max(0.0, min(1.0, float(r.get("anchor_quality") or 0.0)))
            score = math.log1p(strength) * (0.7 + (0.5 * quality)) * hop_decay
            token_cost = int(r["token_estimate"]) or self._estimate_tokens_range(
                r.get("range") or [0, 0]
            )
            hop_label = "h1" if hop_decay >= 1.0 else "h2"
            depth = 2 if hop_decay >= 1.0 else 4  # 2 hops vs 4 (seed→anchor→sym→anchor→sym)
            candidates.append(
                Candidate(
                    kind="symbol",
                    uid=r["uid"],
                    token_cost=token_cost,
                    graph_score=score,
                    name=r["name"],
                    file_path=r["file_path"],
                    range=r.get("range") or [0, 0],
                    relation="DOC_BRIDGE",
                    direction="bridge",
                    depth=depth,
                    file_hash=r.get("file_hash") or "",
                    provenance=[
                        f"doc-bridge:{hop_label},strength={strength},anchor_q={quality:.2f}"
                    ],
                )
            )
        return candidates

    def _filter_doc_hits_to_workspace(self, hits: list[dict]) -> list[dict]:
        """Keep only doc hits whose file belongs to the active workspace."""
        paths = sorted({path for hit in hits if isinstance((path := hit.get("file_path")), str)})
        if not paths:
            return hits
        query = """
        MATCH (f:File {workspace_id: $workspace_id})
        WHERE f.path IN $paths
        RETURN f.path AS path
        """
        try:
            with self.db.driver.session() as session:
                allowed = {
                    record["path"]
                    for record in session.run(query, workspace_id=self.workspace_id, paths=paths)
                }
        except Exception:
            return hits
        return [hit for hit in hits if hit.get("file_path") in allowed]

    def _filter_symbol_hits_to_workspace(self, hits: list[dict]) -> list[dict]:
        """Keep only symbol vector hits that are present in the active workspace."""
        uids = sorted({uid for hit in hits if isinstance((uid := hit.get("uid")), str)})
        if not uids:
            return hits
        query = """
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
        WHERE s.uid IN $uids
        RETURN DISTINCT s.uid AS uid
        """
        try:
            with self.db.driver.session() as session:
                allowed = {
                    record["uid"]
                    for record in session.run(query, workspace_id=self.workspace_id, uids=uids)
                }
        except Exception:
            return hits
        return [hit for hit in hits if hit.get("uid") in allowed]

    # ------------------------------------------------------------------
    # Fusion
    # ------------------------------------------------------------------

    def _fuse(
        self,
        graph: list[Candidate],
        docs: list[Candidate],
        sym_vec: list[Candidate],
        target_uid: str,
        bridge_pool: list[Candidate] | None = None,
    ) -> list[Candidate]:
        pool: dict[str, Candidate] = {}

        for c in graph:
            pool[c.uid] = c

        # Merge semantic symbol hits — add score to existing or create new
        for c in sym_vec:
            if c.uid == target_uid:
                continue
            if c.uid in pool:
                existing = pool[c.uid]
                existing.semantic_score = c.semantic_score
                existing.provenance = existing.provenance + c.provenance
            else:
                pool[c.uid] = c

        # Add doc-bridge symbols. If a bridge target was already pulled by
        # graph BFS or sym_vec, take the max graph_score and merge
        # provenance — bridge strength shouldn't overwrite a real
        # call-graph relevance.
        for c in bridge_pool or []:
            if c.relation == "DOC_BRIDGE":
                c.graph_score = min(1.0, c.graph_score + 0.15)

            if c.uid == target_uid:
                continue
            if c.uid in pool:
                existing = pool[c.uid]
                existing.graph_score = max(existing.graph_score, c.graph_score)
                existing.provenance = existing.provenance + c.provenance
            else:
                pool[c.uid] = c

        # Add doc hits
        for c in docs:
            if c.uid not in pool:
                pool[c.uid] = c
            else:
                existing = pool[c.uid]
                existing.semantic_score = max(existing.semantic_score, c.semantic_score)
                existing.provenance = existing.provenance + c.provenance

        # Boost doc graph_score via COVERS edges
        doc_ids = [c.uid for c in docs if c.uid in pool]
        pooled_sym_uids = {uid for uid, c in pool.items() if c.kind == "symbol"}
        if doc_ids and pooled_sym_uids:
            for link in self._get_covers_links(doc_ids, pooled_sym_uids):
                chunk_id = link["chunk_id"]
                sym_uid = link["sym_uid"]
                if chunk_id in pool and sym_uid in pool:
                    doc_c = pool[chunk_id]
                    quality = anchor_edge_quality(
                        link["anchor_type"],
                        link["confidence"],
                        link["primary_bias"],
                    )
                    linked = pool[sym_uid].graph_score
                    boost = linked * (0.35 + (0.65 * quality))
                    doc_c.graph_score = max(doc_c.graph_score, boost)
                    if quality > anchor_edge_quality(
                        doc_c.anchor_type,
                        doc_c.anchor_confidence,
                        doc_c.primary_bias or 1.0,
                    ):
                        doc_c.anchor_type = link["anchor_type"]
                        doc_c.anchor_confidence = link["confidence"]
                        doc_c.primary_bias = link["primary_bias"]
                    doc_c.provenance.append(
                        "graph:COVERS->"
                        f"{sym_uid[:8]},type={link['anchor_type']},conf={link['confidence']:.2f}"
                    )

        return list(pool.values())

    def _fill_token_costs(self, pool: list[Candidate]) -> None:
        """Batch-fetch token estimates for vector-only symbols (token_cost == 0)."""
        missing_uids = [c.uid for c in pool if c.kind == "symbol" and c.token_cost == 0]
        if not missing_uids:
            return
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE s.uid IN $uids
        RETURN s.uid AS uid,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(c.range, s.range, [0, 0]) AS range,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash
        """
        try:
            details: dict[str, dict] = {}
            with self.db.driver.session() as session:
                result = session.run(query, uids=missing_uids, workspace_id=self.workspace_id)
                for r in result:
                    details[r["uid"]] = {
                        "token_estimate": r["token_estimate"],
                        "range": r["range"],
                        "file_path": r["file_path"],
                        "file_hash": r["file_hash"],
                    }
        except Exception:
            details = {}

        for c in pool:
            if c.kind == "symbol" and c.token_cost == 0:
                d = details.get(c.uid)
                if d:
                    c.token_cost = d["token_estimate"] or self._estimate_tokens_range(d["range"])
                    if not c.file_path or c.file_path == "<unknown>":
                        c.file_path = d["file_path"]
                    if not c.file_hash:
                        c.file_hash = d["file_hash"]
                else:
                    c.token_cost = 200  # conservative fallback

    def _merge_role_backfill(
        self, pool: list[Candidate], backfill: list[Candidate]
    ) -> list[Candidate]:
        return cast(list[Candidate], self.role_backfill.merge_role_backfill(pool, backfill))

    def _merge_role_backfill_impl(
        self, pool: list[Candidate], backfill: list[Candidate]
    ) -> list[Candidate]:
        merged: dict[str, Candidate] = {candidate.uid: candidate for candidate in pool}
        for candidate in backfill:
            existing = merged.get(candidate.uid)
            if existing is None:
                merged[candidate.uid] = candidate
                continue
            existing.graph_score = max(existing.graph_score, candidate.graph_score)
            existing.provenance = existing.provenance + candidate.provenance
            if candidate.render_mode == "signature_only":
                existing.render_mode = "signature_only"
            if candidate.token_cost:
                if existing.token_cost > 0:
                    existing.token_cost = min(existing.token_cost, candidate.token_cost)
                else:
                    existing.token_cost = candidate.token_cost
            if candidate.file_hash and not existing.file_hash:
                existing.file_hash = candidate.file_hash
            if candidate.evidence_role and not existing.evidence_role:
                existing.evidence_role = candidate.evidence_role
            if candidate.supporting_roles:
                existing.supporting_roles = normalize_roles(
                    list(getattr(existing, "supporting_roles", []))
                    + list(candidate.supporting_roles)
                )
        return list(merged.values())

    def _roles_needing_backfill(
        self,
        target: SubgraphNode,
        pool: list[Candidate],
        required_roles: list[str],
    ) -> list[str]:
        target_roles = set(self._roles_of(target))
        needed: list[str] = []
        for role in required_roles:
            if role == "docs_or_concept":
                continue
            if role in target_roles:
                continue
            candidates = [candidate for candidate in pool if role in self._roles_of(candidate)]
            if not candidates:
                needed.append(role)
                continue
            best = max(candidates, key=self._role_candidate_quality)
            if not self._role_selection_ready(best):
                needed.append(role)
        return needed

    def _role_candidate_quality(self, candidate: Candidate) -> float:
        graph_score = max(candidate.graph_score, 0.0)
        semantic_score = max(candidate.semantic_score, 0.0)
        readiness_bonus = 0.3 if self._has_role_backfill(candidate) else 0.0
        token_penalty = min(candidate.token_cost, 1500) / 1000.0
        return max(graph_score, semantic_score) + readiness_bonus - token_penalty

    def _role_selection_ready(self, candidate: Candidate) -> bool:
        if candidate.token_cost <= 0:
            return False
        if self._has_role_backfill(candidate):
            return True
        signal = max(candidate.graph_score, candidate.semantic_score)
        if candidate.token_cost <= 160 and signal >= 0.15:
            return True
        if candidate.token_cost <= 260 and candidate.graph_score >= 0.25:
            return True
        if candidate.token_cost <= 260 and candidate.semantic_score >= 0.8:
            return True
        return False

    @staticmethod
    def _has_role_backfill(candidate: Candidate) -> bool:
        return candidate.relation == "ROLE_BACKFILL" or any(
            str(step).startswith("role-backfill:") for step in candidate.provenance
        )

    @staticmethod
    def _has_marker_chain(candidate: Candidate) -> bool:
        return any("marker_chain" in str(step) for step in candidate.provenance)

    def _role_backfill_candidates(
        self,
        mechanism: str,
        missing_roles: list[str],
        *,
        excluded_uids: set[str],
    ) -> list[Candidate]:
        def _row_matches_spec_name(row: dict, wanted: str) -> bool:
            if row.get("name") == wanted:
                return True
            qn = str(row.get("qualified_name") or "")
            if not qn:
                return False
            # Accept exact qualified tail segment match: "...foo.Bar" for wanted "Bar".
            return qn.endswith(f".{wanted}")

        specs_by_role = role_backfill_specs_for_mechanism(
            mechanism,
            role_catalog=self.role_catalog or None,
        )
        if not specs_by_role:
            return []

        requested_specs: list[tuple[str, dict[str, str | float]]] = []
        for role in missing_roles:
            for spec in specs_by_role.get(role, []):
                requested_specs.append((role, spec))
        if not requested_specs:
            return []

        requested_names = sorted({str(spec["name"]) for _, spec in requested_specs})
        requested_path_hints = sorted(
            {
                str(spec.get("path_hint", ""))
                for _, spec in requested_specs
                if str(spec.get("path_hint", "")).strip()
            }
        )
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
          AND (
            s.name IN $names
            OR any(hint IN $path_hints WHERE f.path CONTAINS hint)
          )
        OPTIONAL MATCH ()-[cr:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->(s)
        WHERE coalesce(cr.workspace_id, $workspace_id) = $workspace_id
        WITH s, f, c, count(DISTINCT cr) AS inbound_edges
        OPTIONAL MATCH (s)-[or:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->()
        WHERE coalesce(or.workspace_id, $workspace_id) = $workspace_id
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               count(DISTINCT or) AS outbound_edges
        """
        try:
            with self.db.driver.session() as session:
                rows = list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        names=requested_names,
                        path_hints=requested_path_hints,
                        excluded_uids=list(excluded_uids),
                    )
                )
        except Exception:
            return []

        candidates: list[Candidate] = []
        for role, spec in requested_specs:
            best_row = None
            best_score = float("-inf")
            name = str(spec["name"])
            path_hint = str(spec.get("path_hint", ""))
            priority = float(spec.get("priority", 0.8))
            for row in rows:
                if not _row_matches_spec_name(row, name):
                    continue
                file_path = row["file_path"]
                path_bonus = 0.25 if path_hint and file_path.endswith(path_hint) else 0.0
                path_bonus += 0.15 if path_hint and path_hint in file_path else 0.0
                score = (
                    priority
                    + path_bonus
                    + 0.08 * math.log1p(float(row.get("inbound_edges", 0) or 0))
                    + 0.10 * math.log1p(float(row.get("outbound_edges", 0) or 0))
                )
                if score > best_score:
                    best_score = score
                    best_row = row
            if best_row is None:
                continue
            token_cost = int(best_row["token_estimate"]) or self._estimate_tokens_range(
                best_row.get("range") or [0, 0]
            )
            candidate = Candidate(
                kind="symbol",
                uid=best_row["uid"],
                token_cost=min(token_cost, 120),
                graph_score=1.2 + best_score,
                name=best_row["name"],
                file_path=best_row["file_path"],
                range=best_row.get("range") or [0, 0],
                render_mode="signature_only",
                relation="ROLE_BACKFILL",
                direction="backfill",
                depth=2,
                file_hash=best_row.get("file_hash") or "",
                evidence_role=role,
                supporting_roles=[],
                provenance=[f"role-backfill:{role}"],
            )
            candidate.symbol_kind = best_row.get("symbol_kind", "")
            candidates.append(candidate)
        return candidates

    # --- Delegates to ranker submodules ---
    def _blended(self, c: Candidate) -> float:
        return self.scoring.blended(c)

    def _normalize(self, pool: list[Candidate]) -> None:
        self.scoring.normalize(pool)

    def _intent_priors(self, intent: Intent) -> dict[str, float]:
        return self.scoring.intent_priors(intent)

    def _normalize_intent_policy(
        self,
        intent: Intent,
        *,
        intent_policy: IntentPolicy | None,
        ambiguous: bool,
        secondary_intent: Intent | None,
    ) -> IntentPolicy:
        if intent_policy is not None:
            return intent_policy
        distribution = {intent.value: 1.0}
        if ambiguous and secondary_intent is not None and secondary_intent != intent:
            distribution = {intent.value: 0.55, secondary_intent.value: 0.45}
        return IntentClassifier.policy_from_signal(
            IntentSignal(
                primary=intent,
                distribution=distribution,
                confidence=max(distribution.values()),
                ambiguous=ambiguous,
            )
        )

    @staticmethod
    def _apply_intent_policy_roles(
        required_roles: list[str], intent_policy: IntentPolicy | None
    ) -> list[str]:
        if not intent_policy or not intent_policy.supplemental_roles:
            return normalize_roles(required_roles)
        return normalize_roles([*required_roles, *intent_policy.supplemental_roles])

    def _intent_priors_for_policy(
        self, intent: Intent, intent_policy: IntentPolicy | None
    ) -> dict[str, float]:
        if not intent_policy or len(intent_policy.active_intents) <= 1:
            return self._intent_priors(intent)

        totals = {"symbol": 0.0, "doc": 0.0}
        weight_total = 0.0
        for active_intent in intent_policy.active_intents:
            weight = intent_policy.weight(active_intent)
            if weight <= 0:
                continue
            priors = self._intent_priors(active_intent)
            totals["symbol"] += weight * priors.get("symbol", 0.3)
            totals["doc"] += weight * priors.get("doc", 0.3)
            weight_total += weight
        if weight_total <= 0:
            return self._intent_priors(intent)
        return {kind: score / weight_total for kind, score in totals.items()}

    def _intent_policy_floor(
        self, intent: Intent, intent_policy: IntentPolicy | None
    ) -> int | None:
        if not intent_policy or len(intent_policy.active_intents) <= 1:
            return None
        weighted_floor = 0.0
        weight_total = 0.0
        for active_intent in intent_policy.active_intents:
            weight = intent_policy.weight(active_intent)
            if weight <= 0:
                continue
            weighted_floor += weight * self._INTENT_FLOORS.get(active_intent, 1200)
            weight_total += weight
        if weight_total <= 0:
            return None
        primary_floor = self._INTENT_FLOORS.get(intent, 1200)
        return max(primary_floor, int(weighted_floor / weight_total))

    def _topic_focus_factor(
        self,
        candidate: Candidate,
        target: SubgraphNode,
        *,
        query: str,
        mechanism: str,
        intent: Intent,
        required_roles: list[str],
    ) -> float:
        return self.scoring.topic_focus_factor(
            candidate,
            target,
            query=query,
            mechanism=mechanism,
            intent=intent,
            required_roles=required_roles,
        )

    def _candidate_matches_query_topic(
        self,
        candidate: Candidate | SubgraphNode,
        target: SubgraphNode,
        *,
        query: str,
    ) -> bool:
        return self.scoring.candidate_matches_query_topic(candidate, target, query=query)

    @staticmethod
    def _focus_query_terms(text: str) -> list[str]:
        return RankerScoring.focus_query_terms(text)

    def _raw_graph_score(
        self,
        neighbor: dict,
        distance: int,
        *,
        chain_pursuit: bool = False,
        registration_chain: bool = False,
    ) -> float:
        return self.scoring.raw_graph_score(
            neighbor,
            distance,
            chain_pursuit=chain_pursuit,
            registration_chain=registration_chain,
        )

    def _direction(self, rel_type: str, outgoing: bool) -> str:
        return self.scoring.direction(rel_type, outgoing)

    def _infer_role(self, c: Candidate | SubgraphNode) -> str:
        return self.role_fulfilment.infer_role(c)

    def _role_of(self, c: Candidate | SubgraphNode) -> str:
        return self.role_fulfilment.role_of(c)

    def _supporting_roles_of(self, c: Candidate | SubgraphNode) -> list[str]:
        return self.role_fulfilment.supporting_roles_of(c)

    def _roles_of(self, c: Candidate | SubgraphNode) -> list[str]:
        return self.role_fulfilment.roles_of(c)

    def _selection_roles(
        self,
        c: Candidate,
        target: SubgraphNode,
        *,
        query: str,
        mechanism: str,
        intent: Intent,
        required_roles: list[str],
    ) -> list[str]:
        return self.role_fulfilment.selection_roles(
            c,
            target,
            query=query,
            mechanism=mechanism,
            intent=intent,
            required_roles=required_roles,
        )

    def _candidate_matches_any_role(
        self,
        c: Candidate | SubgraphNode,
        required_roles: list[str],
    ) -> bool:
        return self.role_fulfilment.candidate_matches_any_role(c, required_roles)

    def _determine_mechanism_structural(self, target: SubgraphNode) -> str:
        return self.role_fulfilment.determine_mechanism_structural(target)

    def _determine_mechanism(self, target: SubgraphNode, query: str = "") -> str:
        return self.role_fulfilment.determine_mechanism(target, query=query)

    def _get_required_roles(self, mechanism: str, *, target=None) -> list[str]:
        return self.role_fulfilment.get_required_roles(mechanism, target=target)

    def _strategy_role_plan(self) -> list[str]:
        return self.role_fulfilment.strategy_role_plan()

    def _role_supply_counts(self):
        return self.role_fulfilment.role_supply_counts()

    def _filter_roles_by_workspace_supply(self, roles: list[str]) -> list[str]:
        return self.role_fulfilment.filter_roles_by_workspace_supply(roles)

    def _target_role_supply_counts(self, target):
        return self.role_fulfilment.target_role_supply_counts(target)

    def _filter_roles_by_target_supply(self, roles: list[str], target) -> list[str]:
        return self.role_fulfilment.filter_roles_by_target_supply(roles, target)

    def _adaptive_role_plan(self, *, target=None) -> list[str]:
        return self.role_fulfilment.adaptive_role_plan(target=target)

    def _canonical_role_for_symbol_uid(self, uid: str) -> str:
        return self.role_fulfilment.canonical_role_for_symbol_uid(uid)

    def _one_hop_connected_symbol_uids(self, target_uid: str, *, limit: int = 48) -> list[str]:
        return self.role_fulfilment.one_hop_connected_symbol_uids(target_uid, limit=limit)

    def _calculate_marginal_gain(
        self,
        c: Candidate,
        chosen: list[Candidate],
        target: SubgraphNode,
        *,
        intent: Intent | None = None,
        mechanism: str = "",
        query: str = "",
        required_roles: list[str],
        candidate_roles: list[str] | None = None,
    ) -> float:
        return self.budget_selector.calculate_marginal_gain(
            c=c,
            chosen=chosen,
            target=target,
            intent=intent,
            mechanism=mechanism,
            query=query,
            required_roles=required_roles,
            candidate_roles=candidate_roles,
        )

    def _calculate_marginal_gain_impl(
        self,
        c: Candidate,
        chosen: list[Candidate],
        target: SubgraphNode,
        *,
        intent: Intent | None = None,
        mechanism: str = "",
        query: str = "",
        required_roles: list[str],
        candidate_roles: list[str] | None = None,
    ) -> float:
        """marginal_gain = base_score + role_bonus + coverage_bonus + bridge_bonus - redundancy_penalty"""
        base_score = self.scoring.blended(c)

        # 1. Role Bonus: Does this symbol fulfill a missing requirement for the mechanism?
        role_bonus = 0.0
        roles_for_gain = [
            role
            for role in (
                candidate_roles if candidate_roles is not None else self.role_fulfilment.roles_of(c)
            )
            if role in required_roles
        ]
        if roles_for_gain:
            chosen_roles = set(self.role_fulfilment.roles_of(target))
            for chosen_candidate in chosen:
                chosen_roles.update(self.role_fulfilment.roles_of(chosen_candidate))
            if any(role not in chosen_roles for role in roles_for_gain):
                role_bonus = 0.5  # High-priority evidence signal

        # 2. Coverage Bonus: Does this symbol complete a structural chain?
        # Boost symbols that are semantically hinted or are direct
        # implementations of the target's interfaces.
        coverage_bonus = 0.0
        if "SEMANTIC_HINT" in (c.relation or ""):
            coverage_bonus += 0.2
        if c.relation == "ROLE_BACKFILL" or self._has_role_backfill(c):
            coverage_bonus += 0.25
        if self.role_fulfilment.marker_chain_roles_are_relevant(c, required_roles):
            coverage_bonus += 0.2
        if c.relation in ("IMPLEMENTS", "OVERRIDES"):
            coverage_bonus += 0.15

        # 3. Bridge Bonus: Boost symbols discovered via DocBridge co-occurrence
        # as they often represent runtime connections static analysis misses.
        bridge_bonus = 0.1 if "doc-bridge" in "".join(c.provenance) else 0.0

        # 4. Redundancy Penalty: Diminishing returns for many symbols in the same file.
        same_file_count = sum(1 for cc in chosen if cc.file_path == c.file_path)
        redundancy_penalty = min(0.4, 0.15 * same_file_count)

        return (
            base_score
            + role_bonus
            + coverage_bonus
            + bridge_bonus
            - redundancy_penalty
        )

    def _load_repository_profile(self) -> dict:
        get_profile = getattr(self.db, "get_repository_profile", None)
        if not callable(get_profile):
            return {}
        try:
            profile = get_profile(workspace_id=self.workspace_id)
        except Exception:
            return {}
        return profile if isinstance(profile, dict) else {}

    def _load_role_catalog(self) -> dict:
        """Load the index-time role catalog produced by Pass 1."""
        from sidecar.indexer.role_clustering import get_role_catalog

        try:
            catalog = get_role_catalog(self.db, self.workspace_id)
        except Exception:
            return {}
        return catalog if isinstance(catalog, dict) else {}

    def _load_derived_role_state_for_catalog(self, role_catalog: dict) -> dict:
        """Read persisted Pass-1 role/fan state in one workspace scan."""
        empty = {"primary": {}, "supporting": {}, "fan": {}}
        if not role_catalog:
            return empty
        try:
            with self.db.driver.session() as session:
                rows = session.run(
                    """
                    MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
                    RETURN s.uid AS uid,
                           coalesce(s.derived_primary_role, '') AS primary_role,
                           coalesce(s.derived_supporting_roles_json, '') AS supporting_roles_json,
                           coalesce(s.call_fan_in, 0.0) AS call_fan_in,
                           coalesce(s.call_fan_out, 0.0) AS call_fan_out,
                           coalesce(s.type_fan_in, 0.0) AS type_fan_in
                    """,
                    workspace_id=self.workspace_id,
                )
                primary: dict[str, str] = {}
                supporting: dict[str, list[str]] = {}
                fan: dict[str, dict[str, float]] = {}
                for row in rows:
                    uid = row.get("uid")
                    if not uid:
                        continue
                    uid = str(uid)
                    primary_role = str(row.get("primary_role") or "")
                    if primary_role:
                        primary[uid] = primary_role
                    payload = row.get("supporting_roles_json") or ""
                    if payload:
                        try:
                            parsed = json.loads(payload)
                        except (TypeError, json.JSONDecodeError):
                            parsed = []
                        if isinstance(parsed, list):
                            supporting[uid] = [str(item) for item in parsed if item]
                    fan[uid] = {
                        "call_fan_in": float(row["call_fan_in"] or 0.0),
                        "call_fan_out": float(row["call_fan_out"] or 0.0),
                        "type_fan_in": float(row["type_fan_in"] or 0.0),
                    }
                return {"primary": primary, "supporting": supporting, "fan": fan}
        except Exception:
            return empty

    def _load_derived_primary_role_map(self) -> dict[str, str]:
        return self._load_derived_primary_role_map_for_catalog(self.role_catalog)

    def _load_derived_primary_role_map_for_catalog(self, role_catalog: dict) -> dict[str, str]:
        """Read Pass-1 primary roles persisted on Symbol nodes."""
        if not role_catalog:
            return {}
        try:
            with self.db.driver.session() as session:
                rows = session.run(
                    """
                    MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
                    WHERE s.derived_primary_role IS NOT NULL
                      AND s.derived_primary_role <> ''
                    RETURN s.uid AS uid, s.derived_primary_role AS role
                    """,
                    workspace_id=self.workspace_id,
                )
                return {r["uid"]: str(r["role"]) for r in rows if r.get("uid") and r.get("role")}
        except Exception:
            return {}

    def _load_derived_supporting_roles_map(self) -> dict[str, list[str]]:
        return self._load_derived_supporting_roles_map_for_catalog(self.role_catalog)

    def _load_derived_supporting_roles_map_for_catalog(
        self, role_catalog: dict
    ) -> dict[str, list[str]]:
        """Read Pass-1 supporting roles persisted on Symbol nodes."""
        if not role_catalog:
            return {}
        try:
            with self.db.driver.session() as session:
                rows = session.run(
                    """
                    MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
                    WHERE s.derived_supporting_roles_json IS NOT NULL
                    RETURN s.uid AS uid, s.derived_supporting_roles_json AS payload
                    """,
                    workspace_id=self.workspace_id,
                )
                result: dict[str, list[str]] = {}
                for row in rows:
                    uid = row.get("uid")
                    payload = row.get("payload")
                    if not uid or not payload:
                        continue
                    try:
                        parsed = json.loads(payload)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(parsed, list):
                        result[str(uid)] = [str(item) for item in parsed if item]
                return result
        except Exception:
            return {}

    def _load_structural_fan_map(self) -> dict[str, dict[str, float]]:
        return self._load_structural_fan_map_for_catalog(self.role_catalog)

    def _load_structural_fan_map_for_catalog(
        self, role_catalog: dict
    ) -> dict[str, dict[str, float]]:
        """Read Pass-1 structural fan profiles persisted on Symbol nodes.

        Keep the workspace-wide load lean. Heavier relation-derived fan values
        (alias API, proxy attr resolution, etc.) are only needed for the public
        target surface and are loaded on demand per symbol.
        """
        if not role_catalog:
            return {}
        try:
            with self.db.driver.session() as session:
                rows = session.run(
                    """
                    MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
                    RETURN s.uid AS uid,
                           coalesce(s.call_fan_in, 0.0) AS call_fan_in,
                           coalesce(s.call_fan_out, 0.0) AS call_fan_out,
                           coalesce(s.type_fan_in, 0.0) AS type_fan_in
                    """,
                    workspace_id=self.workspace_id,
                )
                return {
                    r["uid"]: {
                        "call_fan_in": float(r["call_fan_in"] or 0.0),
                        "call_fan_out": float(r["call_fan_out"] or 0.0),
                        "type_fan_in": float(r["type_fan_in"] or 0.0),
                    }
                    for r in rows
                    if r["uid"]
                }
        except Exception:
            return {}

    def _structural_fan_for_uid(self, uid: str) -> dict[str, float]:
        fan = self._structural_fan_by_uid.setdefault(uid, {})
        if fan.get("_extended_loaded"):
            return fan
        query = """
        MATCH (s:Symbol {uid: $uid})
        CALL {
            WITH s
            OPTIONAL MATCH (s)<-[h:HANDLES]-(:Symbol)
            WHERE coalesce(h.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT h) AS handle_fan_in
        }
        CALL {
            WITH s
            OPTIONAL MATCH (s)-[ref:REFERENCES]->(:Symbol)-[api:HAS_API|INHERITED_API]->(:Symbol)
            WHERE coalesce(ref.workspace_id, $workspace_id) = $workspace_id
              AND coalesce(api.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT api) AS alias_api_fan_out
        }
        CALL {
            WITH s
            OPTIONAL MATCH (s)-[api_out:HAS_API|INHERITED_API]->(:Symbol)
            WHERE coalesce(api_out.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT api_out) AS api_fan_out
        }
        CALL {
            WITH s
            OPTIONAL MATCH (:Symbol)-[api_in:HAS_API|INHERITED_API]->(s)
            WHERE coalesce(api_in.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT api_in) AS api_fan_in
        }
        CALL {
            WITH s
            OPTIONAL MATCH (s)-[ref:REFERENCES_EXTERNAL]->(:ExternalPkg)<-[construct:CALLS_EXTERNAL]-(:Symbol)
            WHERE coalesce(ref.workspace_id, $workspace_id) = $workspace_id
              AND coalesce(construct.workspace_id, $workspace_id) = $workspace_id
              AND coalesce(construct.kind, '') = 'construct'
            RETURN count(DISTINCT construct) AS external_construct_coref_fan_out
        }
        CALL {
            WITH s
            OPTIONAL MATCH (s)-[pa:RESOLVES_ATTR]->(:Symbol)
            WHERE coalesce(pa.workspace_id, $workspace_id) = $workspace_id
            RETURN count(DISTINCT pa) AS proxy_attr_resolve_fan_out
        }
        RETURN coalesce(s.call_fan_in, 0.0) AS call_fan_in,
               coalesce(s.call_fan_out, 0.0) AS call_fan_out,
               coalesce(s.type_fan_in, 0.0) AS type_fan_in,
               handle_fan_in,
               alias_api_fan_out,
               api_fan_out,
               api_fan_in,
               external_construct_coref_fan_out,
               proxy_attr_resolve_fan_out
        """
        try:
            with self.db.driver.session() as session:
                row = session.run(
                    query,
                    uid=uid,
                    workspace_id=self.workspace_id,
                ).single()
        except Exception:
            row = None
        if row:
            fan.update(
                {
                    "call_fan_in": float(row["call_fan_in"] or 0.0),
                    "call_fan_out": float(row["call_fan_out"] or 0.0),
                    "type_fan_in": float(row["type_fan_in"] or 0.0),
                    "handle_fan_in": float(row["handle_fan_in"] or 0.0),
                    "alias_api_fan_out": float(row["alias_api_fan_out"] or 0.0),
                    "api_fan_out": float(row["api_fan_out"] or 0.0),
                    "api_fan_in": float(row["api_fan_in"] or 0.0),
                    "external_construct_coref_fan_out": float(
                        row["external_construct_coref_fan_out"] or 0.0
                    ),
                    "proxy_attr_resolve_fan_out": float(
                        row["proxy_attr_resolve_fan_out"] or 0.0
                    ),
                }
            )
        fan["_extended_loaded"] = 1.0
        return fan

    # Neo4j helpers
    # ------------------------------------------------------------------

    def _get_neighbors(self, uid: str, visited: set, distance: int) -> list[dict]:
        query = """
        MATCH (s:Symbol {uid: $uid})-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT|HAS_API|INHERITED_API|DECORATED_BY|USES_TYPE|INJECTS|INSTANTIATES|HANDLES|RESOLVES_ATTR]-(n:Symbol)
        WHERE NOT n.uid IN $visited
          AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
        OPTIONAL MATCH (fn:File {workspace_id: $workspace_id})-[c:CONTAINS]->(n)
        WITH n, fn, c, r, startNode(r) = s AS outgoing
        RETURN n.uid AS uid,
               n.name AS name,
               coalesce(n.kind, '') AS symbol_kind,
               coalesce(fn.path, '<unknown>') AS file_path,
               coalesce(fn.hash, '') AS file_hash,
               coalesce(n.token_estimate, 0) AS token_estimate,
               coalesce(c.range, n.range, [0, 0]) AS range,
               type(r) AS rel_type,
               coalesce(r.kind, '') AS rel_kind,
               outgoing,
               coalesce(n.call_fan_in, 0.0) AS caller_count,
               coalesce(n.call_fan_out, 0.0) AS outgoing_call_count
        """
        try:
            with self.db.driver.session() as session:
                result = session.run(
                    query,
                    uid=uid,
                    visited=list(visited),
                    workspace_id=self.workspace_id,
                )
                return [
                    {
                        "uid": r["uid"],
                        "name": r["name"],
                        "symbol_kind": r["symbol_kind"],
                        "file_path": r["file_path"],
                        "file_hash": r["file_hash"],
                        "token_estimate": r["token_estimate"],
                        "range": r["range"],
                        "rel_type": r["rel_type"],
                        "rel_kind": r["rel_kind"],
                        "outgoing": r["outgoing"],
                        "caller_count": r["caller_count"],
                        "outgoing_call_count": r["outgoing_call_count"],
                    }
                    for r in result
                ]
        except Exception:
            return []

    def _get_integrates_with_neighbors(
        self,
        uid: str,
        visited: set,
        *,
        limit: int = 12,
    ) -> list[dict]:
        """Symbols in workspace files that integration-coref with the target's file.

        ``(:File)-[:INTEGRATES_WITH]-(:File)`` is materialised at index time
        for file pairs that share >=2 non-plumbing external imports. From a
        symbol, surface a small bounded set of role-bearing symbols in the
        coref'd files — they collaborate around the same external boundary
        and would otherwise be unreachable by structural call/type traversal
        (fastapi/concurrency.py from fastapi/routing.py's
        ``run_endpoint_function`` is the canonical case: both files import
        starlette and anyio, but neither calls into the other's symbols).

        Returned as ``rel_type='INTEGRATES_COREF'`` so scoring can apply a
        distance penalty appropriate to the indirect link.
        """
        query = """
        MATCH (s:Symbol {uid: $uid})
        MATCH (sf:File)-[:CONTAINS]->(s)
        WHERE coalesce(sf.workspace_id, $workspace_id) = $workspace_id
        MATCH (sf)-[iw:INTEGRATES_WITH]-(cf:File)
        WHERE coalesce(iw.workspace_id, $workspace_id) = $workspace_id
          AND cf <> sf
          AND NOT cf.path CONTAINS '/test/'
          AND NOT cf.path CONTAINS '/tests/'
          AND NOT cf.path CONTAINS '/__tests__/'
          AND NOT cf.path CONTAINS '/spec/'
        MATCH (cf)-[c:CONTAINS]->(n:Symbol)
        WHERE NOT n.uid IN $visited
          AND n.uid <> $uid
          AND coalesce(n.kind, '') IN ['function', 'method', 'class']
        WITH n, cf, c, iw,
             coalesce(n.token_estimate, 0) AS tokens,
             coalesce(n.derived_primary_role, '') AS primary_role,
             CASE WHEN coalesce(n.derived_primary_role, '') IN
               ['api_surface','runtime_surface','integration_surface',
                'orchestrator','executor','request_router','registration_step',
                'core_runtime','binding_surface','factory_surface']
               THEN 0 ELSE 1 END AS role_priority
        ORDER BY
          iw.shared DESC,
          role_priority ASC,
          tokens ASC
        // Diversify: one best symbol per integrating file. Otherwise a few
        // partner files crowd the limit with their many small core_runtime
        // helpers and a small file like `fastapi/concurrency.py` (with a
        // single 192-token coroutine wrapper) never surfaces.
        WITH cf, iw, head(collect({n: n, c: c, tokens: tokens})) AS pick
        WITH pick.n AS n, cf, pick.c AS c, iw, pick.tokens AS tokens
        ORDER BY iw.shared DESC, tokens ASC
        LIMIT $limit
        RETURN n.uid AS uid,
               n.name AS name,
               coalesce(n.kind, '') AS symbol_kind,
               coalesce(cf.path, '<unknown>') AS file_path,
               coalesce(cf.hash, '') AS file_hash,
               tokens AS token_estimate,
               coalesce(c.range, n.range, [0, 0]) AS range,
               'INTEGRATES_COREF' AS rel_type,
               '' AS rel_kind,
               true AS outgoing,
               coalesce(n.call_fan_in, 0.0) AS caller_count,
               coalesce(n.call_fan_out, 0.0) AS outgoing_call_count
        """
        try:
            with self.db.driver.session() as session:
                result = session.run(
                    query,
                    uid=uid,
                    visited=list(visited),
                    workspace_id=self.workspace_id,
                    limit=limit,
                )
                return [
                    {
                        "uid": r["uid"],
                        "name": r["name"],
                        "symbol_kind": r["symbol_kind"],
                        "file_path": r["file_path"],
                        "file_hash": r["file_hash"],
                        "token_estimate": r["token_estimate"],
                        "range": r["range"],
                        "rel_type": r["rel_type"],
                        "rel_kind": r["rel_kind"],
                        "outgoing": r["outgoing"],
                        "caller_count": r["caller_count"],
                        "outgoing_call_count": r["outgoing_call_count"],
                    }
                    for r in result
                ]
        except Exception:
            return []

    def _get_external_coref_neighbors(
        self,
        uid: str,
        visited: set,
        *,
        limit: int = 12,
    ) -> list[dict]:
        """Symbols coupled to a target through the same external boundary root.

        This is a ranker-only derived hop, not a persisted graph edge: a public
        alias that references an ``ExternalPkg`` can reach local code that calls
        or constructs that same package root. It keeps CommonJS external facades
        connected without inventing framework-specific links.
        """
        query = """
        MATCH (s:Symbol {uid: $uid})-[ref:REFERENCES_EXTERNAL]->(e:ExternalPkg)<-[r:CALLS_EXTERNAL]-(n:Symbol)
        WHERE NOT n.uid IN $visited
          AND n.uid <> $uid
          AND coalesce(ref.workspace_id, $workspace_id) = $workspace_id
          AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
        MATCH (fn:File {workspace_id: $workspace_id})-[c:CONTAINS]->(n)
        OPTIONAL MATCH ()-[cr:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS]->(n)
        WHERE coalesce(cr.workspace_id, $workspace_id) = $workspace_id
        OPTIONAL MATCH (n)-[out_call:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS]->(:Symbol)
        WHERE coalesce(out_call.workspace_id, $workspace_id) = $workspace_id
        RETURN n.uid AS uid,
               n.name AS name,
               coalesce(n.kind, '') AS symbol_kind,
               coalesce(fn.path, '<unknown>') AS file_path,
               coalesce(fn.hash, '') AS file_hash,
               coalesce(n.token_estimate, 0) AS token_estimate,
               coalesce(c.range, n.range, [0, 0]) AS range,
               'EXTERNAL_COREF' AS rel_type,
               coalesce(r.kind, '') AS rel_kind,
               true AS outgoing,
               count(DISTINCT cr) AS caller_count,
               count(DISTINCT out_call) AS outgoing_call_count
        ORDER BY
          CASE WHEN coalesce(r.kind, '') = 'construct' THEN 0 ELSE 1 END,
          coalesce(n.token_estimate, 0) ASC
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                result = session.run(
                    query,
                    uid=uid,
                    visited=list(visited),
                    workspace_id=self.workspace_id,
                    limit=limit,
                )
                return [
                    {
                        "uid": r["uid"],
                        "name": r["name"],
                        "symbol_kind": r["symbol_kind"],
                        "file_path": r["file_path"],
                        "file_hash": r["file_hash"],
                        "token_estimate": r["token_estimate"],
                        "range": r["range"],
                        "rel_type": r["rel_type"],
                        "rel_kind": r["rel_kind"],
                        "outgoing": r["outgoing"],
                        "caller_count": r["caller_count"],
                        "outgoing_call_count": r["outgoing_call_count"],
                    }
                    for r in result
                ]
        except Exception:
            return []

    def _get_covers_links(self, chunk_ids: list[str], symbol_uids: set[str]) -> list[dict]:
        if not chunk_ids or not symbol_uids:
            return []
        query = """
        MATCH (a:DocAnchor {workspace_id: $workspace_id})-[r:COVERS]->(s:Symbol)
        WHERE a.chunk_id IN $chunk_ids AND s.uid IN $symbol_uids
        RETURN a.chunk_id AS chunk_id,
               s.uid AS sym_uid,
               coalesce(r.anchor_type, 'reference') AS anchor_type,
               coalesce(r.confidence, 0.65) AS confidence,
               coalesce(r.primary_bias, 0.7) AS primary_bias,
               coalesce(r.resolver, 'legacy') AS resolver
        """
        try:
            with self.db.driver.session() as session:
                result = session.run(
                    query,
                    chunk_ids=chunk_ids,
                    symbol_uids=list(symbol_uids),
                    workspace_id=self.workspace_id,
                )
                return [
                    {
                        "chunk_id": r["chunk_id"],
                        "sym_uid": r["sym_uid"],
                        "anchor_type": r["anchor_type"],
                        "confidence": float(r["confidence"] or 0.0),
                        "primary_bias": float(r["primary_bias"] or 0.0),
                        "resolver": r["resolver"],
                    }
                    for r in result
                ]
        except Exception:
            return []

    @staticmethod
    def _estimate_tokens_range(range_: list) -> int:
        if not range_ or len(range_) < 2:
            return 0
        return max(1, int((int(range_[1]) - int(range_[0]) + 1) * 8))
