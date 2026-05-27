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

import math
import re
from heapq import heappop, heappush
from typing import cast

from sidecar.context.intent_classifier import Intent, IntentClassifier, IntentPolicy, IntentSignal
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
from sidecar.context.ranker.recovery import StructuralRecovery
from sidecar.context.ranker.role_fulfilment import RoleFulfilment
from sidecar.context.ranker.scoring import RankerScoring
from sidecar.context.ranker.signal_constants import (
    EXPLORATION_NOISE_FACTOR as _EXPLORATION_NOISE_FACTOR,
)
from sidecar.context.ranker.signal_constants import (
    HOOK_FLOW_PATH_TOKENS as _HOOK_FLOW_PATH_TOKENS,
)
from sidecar.context.ranker.signal_constants import (
    IMPACT_TOPIC_STOPWORDS as _IMPACT_TOPIC_STOPWORDS,
)
from sidecar.context.ranker.signal_constants import (
    LOW_SIGNAL_DOC_PATH_PATTERNS as _LOW_SIGNAL_DOC_PATH_PATTERNS,
)
from sidecar.context.ranker.signal_constants import (
    NOISE_FACTOR as _NOISE_FACTOR,
)
from sidecar.context.ranker.signal_constants import (
    NOISE_NAME_PREFIXES as _NOISE_NAME_PREFIXES,
)
from sidecar.context.ranker.signal_constants import (
    NOISE_NAME_SUBSTRINGS as _NOISE_NAME_SUBSTRINGS,
)
from sidecar.context.ranker.signal_constants import (
    NOISE_PATH_PATTERNS as _NOISE_PATH_PATTERNS,
)
from sidecar.context.ranker.signal_constants import (
    REGISTRATION_FLOW_PATH_TOKENS as _REGISTRATION_FLOW_PATH_TOKENS,
)
from sidecar.context.role_taxonomy import normalize_roles
from sidecar.context.types import DocChunk, Subgraph, SubgraphNode
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
        "CALLS_out": 1.0,
        "CALLS_in": 1.2,
        "SEMANTIC_HINT_out": 1.3,
        "SEMANTIC_HINT_in": 1.3,
        "HAS_API_out": 1.45,
        "HAS_API_in": 1.2,
        "INHERITED_API_out": 1.35,
        "INHERITED_API_in": 1.15,
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
        self.repository_profile = self._load_repository_profile()
        self.strategy_profile = self.repository_profile.get("strategy_profile", {})
        self.role_catalog = self._load_role_catalog()
        self._derived_role_by_uid = self._load_derived_role_map()
        self._cluster_to_role = self._build_cluster_to_role_map()
        self.role_fulfilment = RoleFulfilment(self)
        self.scoring = RankerScoring(self)
        self.structural_recovery = StructuralRecovery(self)
        self.budget_pruner = BudgetPruner(self)
        self.target_selector = TargetSelector(self)
        self.graph_candidate_source = GraphCandidateSource(self)
        self.vector_candidate_source = VectorCandidateSource(self)
        self.role_backfill = RoleBackfill(self)
        self.budget_selector = BudgetSelector(self)
        self.subgraph_assembler = SubgraphAssembler(self)
        self._workspace_root = None

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
        WITH s, f, c, outgoing_edges, incoming_edges, total_edges,
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
               total_edges
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
        """Resolve ``Class.method`` via MRO API edges or qualified-name tail."""
        qualified_suffix = f".{class_name}.{method_name}"
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol {name: $method_name})
        WHERE EXISTS {
            MATCH (:Symbol {name: $class_name, kind: 'class'})
                  -[:HAS_API|INHERITED_API {workspace_id: $workspace_id}]->(s)
        }
           OR coalesce(s.qualified_name, '') ENDS WITH $qualified_suffix
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
        WITH s, f, c, outgoing_edges, incoming_edges, total_edges
        ORDER BY
          CASE
            WHEN EXISTS {
              MATCH (:Symbol {name: $class_name, kind: 'class'})
                    -[:HAS_API {workspace_id: $workspace_id}]->(s)
            } THEN 0
            WHEN EXISTS {
              MATCH (:Symbol {name: $class_name, kind: 'class'})
                    -[:INHERITED_API {workspace_id: $workspace_id}]->(s)
            } THEN 1
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
        query_bonus = self._target_query_bonus(
            query=query,
            kind=kind,
            role=role,
            file_path=file_path,
            qualified_name=row.get("qualified_name", ""),
            intent=intent,
        )
        components = {
            "path": self._target_path_bonus(file_path),
            "role": self._target_role_bonus(role),
            "edges": min(1.4, 0.22 * outgoing_edges + 0.08 * incoming_edges + 0.05 * total_edges),
            "query": query_bonus,
            "kind": self._target_kind_bonus(kind, intent=intent),
            "size_penalty": -min(0.6, token_estimate / 6000.0),
        }
        score = sum(components.values())
        return score, {"role": role, "components": components}

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
        file_lc = file_path.lower().replace("\\", "/")
        if any(file_lc.endswith(s) for s in ("/main.py", "/index.py", "/app.py", "/base.py")):
            return 0.55
        return 0.35

    def _target_role_bonus(self, role: str) -> float:
        role_weights = {
            "api_surface": 1.2,
            "executor": 1.0,
            "orchestrator": 0.95,
            "construction_surface": 0.9,
            "validator_handle": 0.95,
            "serializer_handle": 0.9,
            "binding_surface": 0.9,
            "runtime_surface": 0.85,
            "factory_surface": 0.8,
            "schema_builder": 0.75,
            "error_surface": 0.75,
            "representation_surface": 0.6,
            "core_runtime": 0.55,
            "config_surface": 0.3,
            "compat_bridge": 0.45,
            "supporting_surface": 0.4,
            "docs_or_concept": -0.4,
        }
        return role_weights.get(role, 0.35)

    def _target_kind_bonus(self, kind: str, *, intent: Intent | None = None) -> float:
        if kind == "function":
            return 0.35 if intent != Intent.DESIGN_QUESTION else 0.2
        if kind == "class":
            return 0.1 if intent != Intent.EXPLORATION else 0.0
        return 0.0

    def _target_query_bonus(
        self,
        *,
        query: str,
        kind: str,
        role: str,
        file_path: str,
        qualified_name: str,
        intent: Intent | None = None,
    ) -> float:
        if not query:
            return 0.0
        query_lower = query.lower()
        bonus = 0.0
        if kind == "function" and any(
            phrase in query_lower
            for phrase in ("how does", "before", "called", "register", "run", "resolved")
        ):
            bonus += 0.5
        if kind == "class" and any(
            phrase in query_lower
            for phrase in ("class", "type", "parameter", "config", "marker", "annotation")
        ):
            bonus += 0.35
        if role == "api_surface" and any(
            phrase in query_lower for phrase in ("how does", "resolved", "before", "called")
        ):
            bonus += 0.35
        if role == "config_surface" and any(
            phrase in query_lower for phrase in ("parameter", "annotation", "config", "marker")
        ):
            bonus += 0.25
        if role == "schema_builder" and "schema" in query_lower:
            bonus += 0.25
        if role in ("validator_handle", "core_runtime") and any(
            phrase in query_lower for phrase in ("validate", "validation", "validated", "core")
        ):
            bonus += 0.2
        if role == "serializer_handle" and any(
            phrase in query_lower for phrase in ("dump", "serialize", "serialization", "json")
        ):
            bonus += 0.2
        if "high-level api" in query_lower or "high level api" in query_lower:
            bonus += 0.25
        # Suppress api_surface siblings not mentioned in the query: if the query
        # names a specific class via a query term overlap, penalise other top-level
        # classes in the same role whose name does not appear in the query at all.
        if role == "api_surface" and kind == "class" and qualified_name:
            name_lc = qualified_name.lower().rsplit(".", 1)[-1]
            if name_lc and name_lc not in query_lower and len(name_lc) >= 5:
                query_terms_set = set(self._query_terms(query))
                if query_terms_set and not any(t in name_lc for t in query_terms_set):
                    bonus -= 0.35

        query_terms = self._query_terms(query)
        haystack = f"{file_path.lower()} {qualified_name.lower()}"
        overlap = sum(1 for term in query_terms if term in haystack)
        bonus += min(0.4, 0.1 * overlap)

        if intent == Intent.NAVIGATION and kind == "class":
            bonus += 0.1
        return bonus

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
        # 1. Collect graph BFS candidates (pool-size-limited, not budget-limited)
        graph_pool = self._graph_candidates(target.uid, pool_size=graph_pool_size, intent=intent)

        # 2. Collect vector candidates for docs and symbols
        doc_pool = self._doc_candidates(query, limit=vector_limit)
        sym_vec_pool = self._sym_vec_candidates(query, limit=vector_limit)

        # 3. Doc-bridge: semantic relationships static graph edges cannot see.
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

        # 4. Fuse into unified pool, boosting docs linked via COVERS
        pool = self._fuse(graph_pool, doc_pool, sym_vec_pool, target.uid, bridge_pool=bridge_pool)

        # 5. Fill missing token costs for vector-only symbols before we
        # decide whether a role is genuinely selection-ready.
        self._fill_token_costs(pool)

        # If retrieval produced no docs at all, synthesize one tiny concept
        # anchor from target metadata so docs_or_concept is not impossible.
        if not any(c.kind == "doc" for c in pool):
            fallback_doc = self._target_concept_fallback_candidate(target, query=query)
            if fallback_doc is not None:
                pool.append(fallback_doc)

        # 6. Mechanism-aware role backfill for sparse framework graphs.
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
            impact_reference_anchors = self.structural_recovery.impact_reference_anchor_candidates(
                target,
                query=query,
                excluded_uids={target.uid},
                pool=pool,
            )
            if impact_reference_anchors:
                pool = self._merge_role_backfill(pool, impact_reference_anchors)
        required_roles = self._apply_intent_policy_roles(required_roles, intent_policy)
        roles_for_backfill = self._roles_needing_backfill(
            target,
            pool,
            required_roles,
        )
        if roles_for_backfill:
            backfill = self._role_backfill_candidates(
                mechanism,
                roles_for_backfill,
                excluded_uids={target.uid},
            )
            if backfill:
                pool = self._merge_role_backfill(pool, backfill)
                roles_for_backfill = self._roles_needing_backfill(
                    target,
                    pool,
                    required_roles,
                )
        recovery_roles = (
            roles_for_backfill
            if intent == Intent.IMPACT_ANALYSIS
            else required_roles
            if self._needs_structural_recovery(target)
            else roles_for_backfill
        )
        if recovery_roles:
            recovery = self._generic_role_recovery_candidates(
                target,
                recovery_roles,
                excluded_uids={target.uid},
            )
            if recovery:
                pool = self._merge_role_backfill(pool, recovery)

        # 6c. Trace-style dependency questions: same imported modules as generic
        # recovery (graph IMPORTS + filesystem-resolved relatives), but relax seating
        # when strict role overlap fails so hub symbols in sibling modules still land.
        trace_import_anchors = self._trace_dependency_import_anchor_candidates(
            target,
            query=query,
            mechanism=mechanism,
            required_roles=required_roles,
            excluded_uids={target.uid},
            pool=pool,
        )
        if trace_import_anchors:
            pool = self._merge_role_backfill(pool, trace_import_anchors)

        trace_routing_anchors = (
            self.structural_recovery.trace_routing_composition_anchor_candidates(
                target,
                query=query,
                mechanism=mechanism,
                required_roles=required_roles,
                excluded_uids={target.uid},
                pool=pool,
            )
        )
        if trace_routing_anchors:
            pool = self._merge_role_backfill(pool, trace_routing_anchors)

        module_composition_anchors = self._module_composition_anchor_candidates(
            target,
            query=query,
            mechanism=mechanism,
            required_roles=required_roles,
            excluded_uids={target.uid},
            pool=pool,
        )
        if module_composition_anchors:
            pool = self._merge_role_backfill(pool, module_composition_anchors)

        if intent != Intent.IMPACT_ANALYSIS:
            query_topic_anchors = self.structural_recovery.query_topic_anchor_candidates(
                target,
                query=query,
                excluded_uids={target.uid},
                limit=16,
            )
            if query_topic_anchors:
                pool = self._merge_role_backfill(pool, query_topic_anchors)

        if intent == Intent.IMPACT_ANALYSIS:
            impact_topic_anchors = self.structural_recovery.impact_topic_anchor_candidates(
                target,
                query=query,
                excluded_uids={target.uid},
                pool=pool,
            )
            if impact_topic_anchors:
                pool = self._merge_role_backfill(pool, impact_topic_anchors)

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
        trace_mode_for_sort = RankerScoring.trace_dependency_gain_mode(mechanism, query)
        trace_focus_text = f"{target.name or ''} {query or ''}".lower()
        dependency_trace_sort = any(
            token in trace_focus_text
            for token in ("depend", "dependency", "dependencies", "inject", "provider", "container")
        )

        def _sort_key(c: Candidate) -> tuple:
            base = self._blended(c)
            roles = set(self._roles_of(c))
            path_lc = (c.file_path or "").lower().replace("\\", "/")
            name_lc = f"{c.name or ''} {c.qualified_name or ''}".lower()
            trace_name_focus = any(
                token in name_lc
                for token in (
                    "depend",
                    "dependant",
                    "dependency",
                    "dependencies",
                    "inject",
                    "provider",
                    "container",
                    "resolve",
                    "solve",
                )
            )
            trace_focus_rank = 0
            if trace_mode_for_sort and dependency_trace_sort:
                if trace_name_focus and "/dependencies/" in path_lc:
                    trace_focus_rank = 3
                elif trace_name_focus:
                    trace_focus_rank = 2
                elif "/dependencies/" in path_lc:
                    trace_focus_rank = 1
            is_trace_topic_anchor = trace_mode_for_sort and any(
                step == "trace-topic-anchor" for step in c.provenance
            )
            is_query_topic_anchor = any(step == "query-topic-anchor" for step in c.provenance)
            # Subsystem-isolated candidates (noise_factor driven to ~0.15 by
            # topic_focus_factor) should not claim the top role-filler tier —
            # they are off-topic even if their cluster role overlaps required.
            is_subsystem_isolated = c.noise_factor < 0.2 and c.kind != "doc"
            # Tier 0 (best): candidates that fill a missing required role the
            # target itself doesn't cover. These must beat raw doc-relevance
            # so a large/weak role-filler still seats before unrelated docs.
            if roles & unfilled_required:
                if is_subsystem_isolated:
                    return (0.5, trace_focus_rank, base)
                return (2, trace_focus_rank, base)
            if is_trace_topic_anchor:
                trace_topic_tier = 1.5 if dependency_trace_sort else 2.5
                return (trace_topic_tier, trace_focus_rank, base)
            if is_query_topic_anchor:
                return (1.25, trace_focus_rank, base)
            if roles & non_trivial_required:
                return (1, trace_focus_rank, base)
            return (0, trace_focus_rank, base)

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

    def _module_composition_anchor_candidates(
        self,
        target: SubgraphNode,
        *,
        query: str,
        mechanism: str,
        required_roles: list[str],
        excluded_uids: set[str],
        pool: list[Candidate],
        limit: int = 12,
    ) -> list[Candidate]:
        haystack = f"{target.name} {target.file_path} {query} {mechanism}".lower()
        if "module" not in haystack:
            return []
        if not any(
            term in haystack
            for term in (
                "compose",
                "composition",
                "controller",
                "decorator",
                "export",
                "feature",
                "import",
                "provider",
            )
        ):
            return []

        rows = self._module_composition_symbol_rows(
            excluded_uids={target.uid, *excluded_uids, *(c.uid for c in pool if c.uid)},
            limit=limit * 4,
        )
        if not rows:
            return []

        scoped = set(
            normalize_roles([*required_roles, "composition_surface", "integration_surface"])
        )
        candidates: list[Candidate] = []
        for row in rows[:limit]:
            candidate = self._recovery_candidate_from_row(
                row,
                origin="module_composition_anchor",
                scoped_roles=scoped,
                target=target,
            )
            if candidate is None:
                continue
            candidate.graph_score += 0.35
            candidate.provenance.append("module-composition-anchor")
            candidates.append(candidate)
        return candidates

    def _module_composition_symbol_rows(
        self,
        *,
        excluded_uids: set[str],
        limit: int,
    ) -> list[dict]:
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
          AND NOT f.path CONTAINS '/test/'
          AND NOT f.path CONTAINS '/tests/'
          AND NOT f.path CONTAINS '/integration/'
          AND NOT f.path CONTAINS '/sample/'
          AND NOT f.path CONTAINS '/samples/'
          AND (
            f.path CONTAINS '/module'
            OR f.path CONTAINS 'module.'
            OR f.path CONTAINS 'metadata-scanner'
            OR f.path CONTAINS '/scanner'
            OR toLower(s.name) IN ['imports', 'controllers', 'providers', 'exports']
            OR toLower(s.name) CONTAINS 'metadata'
            OR toLower(s.name) CONTAINS 'scanner'
          )
        OPTIONAL MATCH ()-[cr:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->(s)
        WHERE coalesce(cr.workspace_id, $workspace_id) = $workspace_id
        WITH s, f, c, count(DISTINCT cr) AS inbound_edges
        OPTIONAL MATCH (s)-[or:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->()
        WHERE coalesce(or.workspace_id, $workspace_id) = $workspace_id
        WITH s, f, c, inbound_edges, count(DISTINCT or) AS outbound_edges
        WITH s, f, c, inbound_edges, outbound_edges,
             CASE
               WHEN f.path CONTAINS 'metadata-scanner' THEN 3.0
               WHEN f.path CONTAINS '/injector/module' THEN 2.8
               WHEN f.path CONTAINS '/scanner' THEN 2.3
               WHEN f.path CONTAINS '/module' OR f.path CONTAINS 'module.' THEN 1.8
               ELSE 0.0
             END
             + CASE
               WHEN toLower(s.name) IN ['imports', 'controllers', 'providers', 'exports'] THEN 1.2
               WHEN toLower(s.name) CONTAINS 'metadata' THEN 0.8
               WHEN toLower(s.name) CONTAINS 'scanner' THEN 0.8
               ELSE 0.0
             END AS anchor_score
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               outbound_edges
        ORDER BY anchor_score DESC, inbound_edges + outbound_edges DESC, size(file_path) ASC
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        excluded_uids=list(excluded_uids),
                        limit=limit,
                    )
                )
        except Exception:
            return []

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
    _CHAIN_PURSUIT_INTENTS = frozenset({Intent.DESIGN_QUESTION, Intent.EXPLORATION})

    def _graph_candidates(
        self,
        target_uid: str,
        pool_size: int,
        intent: Intent | None = None,
    ) -> list[Candidate]:
        return cast(
            list[Candidate],
            self.graph_candidate_source.graph_candidates(target_uid, pool_size, intent=intent),
        )

    def _graph_candidates_impl(
        self,
        target_uid: str,
        pool_size: int,
        intent: Intent | None = None,
    ) -> list[Candidate]:
        """BFS from target, collecting up to pool_size candidates without token budget.

        When ``intent`` is in ``_CHAIN_PURSUIT_INTENTS`` and the edge being
        traversed is an outgoing CALLS_* edge, the distance penalty is
        cut so the chain can be followed deeper. Other edges and other
        intents keep the original scoring.
        """
        chain_pursuit = intent in self._CHAIN_PURSUIT_INTENTS if intent else False
        visited = {target_uid}
        candidates: list[Candidate] = []
        # Tuple shape: (-score, push_seq, uid, neighbor_dict, rel_type, outgoing, distance)
        # ``push_seq`` is a monotonic counter that breaks ties before Python
        # has to compare the dict fields (which raises TypeError).
        frontier: list[tuple[float, int, str, dict, str, bool, int]] = []
        push_seq = 0

        for n in self._get_neighbors(target_uid, visited, distance=1):
            score = self._raw_graph_score(n, distance=1, chain_pursuit=chain_pursuit)
            heappush(
                frontier,
                (-score, push_seq, n["uid"], n, n["rel_type"], n["outgoing"], 1),
            )
            push_seq += 1

        while frontier and len(candidates) < pool_size:
            neg_score, _seq, uid, neighbor, rel_type, outgoing, distance = heappop(frontier)
            score = -neg_score
            if uid in visited:
                continue
            visited.add(uid)

            token_cost = neighbor.get("token_estimate", 0) or self._estimate_tokens_range(
                neighbor.get("range", [0, 0])
            )
            chain_tag = ""
            if chain_pursuit and self._is_outgoing_call(rel_type, outgoing):
                chain_tag = ",chain"
            c = Candidate(
                kind="symbol",
                uid=uid,
                token_cost=token_cost,
                graph_score=score,
                name=neighbor["name"],
                file_path=neighbor["file_path"],
                range=neighbor.get("range", [0, 0]),
                relation=rel_type,
                direction=self._direction(rel_type, outgoing),
                depth=distance,
                file_hash=neighbor.get("file_hash", ""),
                provenance=[f"graph:{rel_type},depth={distance}{chain_tag}"],
            )
            candidates.append(c)

            for nn in self._get_neighbors(uid, visited, distance=distance + 1):
                ns = self._raw_graph_score(nn, distance=distance + 1, chain_pursuit=chain_pursuit)
                heappush(
                    frontier,
                    (-ns, push_seq, nn["uid"], nn, nn["rel_type"], nn["outgoing"], distance + 1),
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
    def _generic_role_recovery_candidates(
        self,
        target: SubgraphNode,
        roles: list[str],
        *,
        excluded_uids: set[str],
    ):
        return self.structural_recovery.generic_role_recovery_candidates(
            target, roles, excluded_uids=excluded_uids
        )

    def _trace_routing_composition_anchor_candidates(
        self,
        target: SubgraphNode,
        *,
        query: str,
        mechanism: str,
        required_roles: list[str],
        excluded_uids: set[str],
        pool: list[Candidate],
    ):
        return self.structural_recovery.trace_routing_composition_anchor_candidates(
            target,
            query=query,
            mechanism=mechanism,
            required_roles=required_roles,
            excluded_uids=excluded_uids,
            pool=pool,
        )

    def _trace_dependency_import_anchor_candidates(
        self,
        target: SubgraphNode,
        *,
        query: str,
        mechanism: str,
        required_roles: list[str],
        excluded_uids: set[str],
        pool: list[Candidate],
    ):
        return self.structural_recovery.trace_dependency_import_anchor_candidates(
            target,
            query=query,
            mechanism=mechanism,
            required_roles=required_roles,
            excluded_uids=excluded_uids,
            pool=pool,
        )

    def _needs_structural_recovery(self, target: SubgraphNode) -> bool:
        return self.structural_recovery.needs_structural_recovery(target)

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
    ) -> float:
        return self.scoring.raw_graph_score(neighbor, distance, chain_pursuit=chain_pursuit)

    def _direction(self, rel_type: str, outgoing: bool) -> str:
        return self.scoring.direction(rel_type, outgoing)

    @staticmethod
    def _trace_dependency_gain_mode(mechanism: str, query: str) -> bool:
        return RankerScoring.trace_dependency_gain_mode(mechanism, query)

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

    def _roles_for_auto_mechanism(self, archetype: str) -> list[str]:
        return self.role_fulfilment.roles_for_auto_mechanism(archetype)

    def _auto_mechanism_from_strategy(self, target: SubgraphNode, query: str = "") -> str:
        return self.role_fulfilment.auto_mechanism_from_strategy(target, query=query)

    def _canonical_role_for_symbol_uid(self, uid: str) -> str:
        return self.role_fulfilment.canonical_role_for_symbol_uid(uid)

    def _one_hop_connected_symbol_uids(self, target_uid: str, *, limit: int = 48) -> list[str]:
        return self.role_fulfilment.one_hop_connected_symbol_uids(target_uid, limit=limit)

    # StructuralRecovery entrypoints (tests may patch these on UnifiedRanker)
    def _same_file_symbol_rows(self, file_path: str, *, excluded_uids: set[str]):
        return self.structural_recovery.same_file_symbol_rows(
            file_path, excluded_uids=excluded_uids
        )

    def _imported_symbol_rows(self, file_path: str, *, excluded_uids: set[str]):
        return self.structural_recovery.imported_symbol_rows(file_path, excluded_uids=excluded_uids)

    def _recovery_candidate_from_row(
        self,
        row: dict,
        *,
        origin: str,
        scoped_roles: set[str],
        target: SubgraphNode,
    ):
        return self.structural_recovery.recovery_candidate_from_row(
            row,
            origin=origin,
            scoped_roles=scoped_roles,
            target=target,
        )

    def _trace_dependency_runtime_symbol_rows(
        self,
        target: SubgraphNode,
        *,
        excluded_uids: set[str],
    ):
        return self.structural_recovery.trace_dependency_runtime_symbol_rows(
            target,
            excluded_uids=excluded_uids,
        )

    def _resolve_filesystem_import_paths(self, file_path: str) -> list[str]:
        return self.structural_recovery.resolve_filesystem_import_paths(file_path)

    def _resolve_intra_repo_package_import_paths(self, file_path: str) -> list[str]:
        return self.structural_recovery.resolve_intra_repo_package_import_paths(file_path)

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
        if c.relation in ("IMPLEMENTS", "OVERRIDES"):
            coverage_bonus += 0.15

        # 3. Bridge Bonus: Boost symbols discovered via DocBridge co-occurrence
        # as they often represent runtime connections static analysis misses.
        bridge_bonus = 0.1 if "doc-bridge" in "".join(c.provenance) else 0.0

        # 3b. For dependency tracing, reward breadth across files so we
        # improve file-level recall instead of overfitting one module.
        file_coverage_bonus = 0.0
        trace_dependency_mode = RankerScoring.trace_dependency_gain_mode(mechanism, query)
        if trace_dependency_mode and c.kind != "doc":
            path_lc = (c.file_path or "").lower().replace("\\", "/")
            eligible_trace_breadth = c.noise_factor >= 1.0 or intent == Intent.IMPACT_ANALYSIS
            if "/dependencies/" in path_lc:
                file_coverage_bonus += 0.14
            if any(token in path_lc for token in _HOOK_FLOW_PATH_TOKENS):
                file_coverage_bonus += 0.10
            if "registration_flow" in (mechanism or "").lower() and any(
                token in path_lc for token in _REGISTRATION_FLOW_PATH_TOKENS
            ):
                file_coverage_bonus += 0.12
            if c.file_path and c.file_path != target.file_path:
                chosen_files = {cc.file_path for cc in chosen}
                if c.file_path not in chosen_files and eligible_trace_breadth:
                    file_coverage_bonus += 0.22
            if c.relation in ("DEPENDS_ON", "CALLS_DIRECT", "CALLS_SCOPED", "CALLS_IMPORTED"):
                file_coverage_bonus += 0.06
            if "registration_flow" in (mechanism or "").lower() and "runtime_surface" in (
                candidate_roles or self.role_fulfilment.roles_of(c)
            ):
                file_coverage_bonus += 0.08

        # 4. Redundancy Penalty: Diminishing returns for many symbols in the same file.
        same_file_count = sum(1 for cc in chosen if cc.file_path == c.file_path)
        redundancy_penalty = min(0.4, 0.15 * same_file_count)

        return (
            base_score
            + role_bonus
            + coverage_bonus
            + bridge_bonus
            + file_coverage_bonus
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

    def _load_derived_role_map(self) -> dict[str, int]:
        """Read every Symbol's `derived_role_id` for this workspace."""
        if not self.role_catalog:
            return {}
        try:
            with self.db.driver.session() as session:
                rows = session.run(
                    """
                    MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
                    WHERE s.derived_role_id IS NOT NULL
                    RETURN s.uid AS uid, s.derived_role_id AS cid
                    """,
                    workspace_id=self.workspace_id,
                )
                return {r["uid"]: int(r["cid"]) for r in rows if r["uid"] is not None}
        except Exception:
            return {}

    def _build_cluster_to_role_map(self) -> dict[int, str]:
        """Pick a single primary canonical role per cluster.

        For every canonical role in the catalog, take its top resolved
        cluster (`resolve_role_clusters` already preserves archetype
        preference order). When multiple roles claim the same cluster as
        their top match, the role with the highest confidence wins;
        sort is stable so ties break on catalog iteration order.
        """
        if not self.role_catalog:
            return {}
        from sidecar.indexer.role_clustering import resolve_role_clusters

        cluster_claims: dict[int, list[tuple[str, float]]] = {}
        for role in self.role_catalog.get("role_to_archetypes") or []:
            matches = resolve_role_clusters(self.role_catalog, role)
            if not matches:
                continue
            top = matches[0]
            cluster_claims.setdefault(int(top["cluster_id"]), []).append(
                (role, float(top["confidence"]))
            )
        result: dict[int, str] = {}
        for cid, claims in cluster_claims.items():
            claims.sort(key=lambda item: item[1], reverse=True)
            result[cid] = claims[0][0]
        return result

    # Neo4j helpers
    # ------------------------------------------------------------------

    def _get_neighbors(self, uid: str, visited: set, distance: int) -> list[dict]:
        query = """
        MATCH (s:Symbol {uid: $uid})-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT|HAS_API|INHERITED_API]-(n:Symbol)
        WHERE NOT n.uid IN $visited
          AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
        OPTIONAL MATCH ()-[cr:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS]->(n)
        WHERE coalesce(cr.workspace_id, $workspace_id) = $workspace_id
        OPTIONAL MATCH (fn:File {workspace_id: $workspace_id})-[c:CONTAINS]->(n)
        WITH n, fn, c, r, startNode(r) = s AS outgoing, count(cr) AS caller_count
        RETURN n.uid AS uid,
               n.name AS name,
               coalesce(fn.path, '<unknown>') AS file_path,
               coalesce(fn.hash, '') AS file_hash,
               coalesce(n.token_estimate, 0) AS token_estimate,
               coalesce(c.range, n.range, [0, 0]) AS range,
               type(r) AS rel_type,
               outgoing,
               caller_count
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
                        "file_path": r["file_path"],
                        "file_hash": r["file_hash"],
                        "token_estimate": r["token_estimate"],
                        "range": r["range"],
                        "rel_type": r["rel_type"],
                        "outgoing": r["outgoing"],
                        "caller_count": r["caller_count"],
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
