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
from dataclasses import dataclass, field
from heapq import heappop, heappush

from sidecar.context.intent_classifier import Intent
from sidecar.context.types import DocChunk, SubgraphNode, Subgraph
from sidecar.workspace import DEFAULT_WORKSPACE_ID


@dataclass
class RankerWeights:
    alpha: float = 1.0    # graph structural score
    beta: float = 0.8     # semantic similarity score
    gamma: float = 0.4    # intent tier prior
    delta: float = 0.5    # overlap bonus (both signals fired)
    epsilon: float = 0.5  # token cost penalty per 100 tokens


DEFAULT_WEIGHTS = RankerWeights()


@dataclass
class Candidate:
    kind: str                              # "symbol" | "doc"
    uid: str                               # symbol UID or doc chunk_id
    token_cost: int
    graph_score: float = 0.0
    semantic_score: float = 0.0
    intent_weight: float = 0.0
    provenance: list[str] = field(default_factory=list)
    # symbol metadata
    name: str = ""
    file_path: str = ""
    range: list[int] = field(default_factory=lambda: [0, 0])
    relation: str = ""
    direction: str = ""
    depth: int = 0
    file_hash: str = ""
    # doc metadata
    content: str = ""

    @property
    def overlap(self) -> bool:
        return self.graph_score > 0 and self.semantic_score > 0


class VectorSearcher:
    """Thin wrapper around LanceDB for use by UnifiedRanker."""

    def __init__(self, lancedb_client):
        self.db = lancedb_client

    def search_docs(self, query: str, limit: int = 30) -> list[dict]:
        raw = self.db.search(query, limit)
        return [
            {
                "chunk_id": r.get("id", f"{r['file_path']}::chunk"),
                "file_path": r["file_path"],
                "content": r["chunk"],
                "score": float(r.get("score") or 0.0),
            }
            for r in raw
        ]

    def search_symbols(self, query: str, limit: int = 30) -> list[dict]:
        # threshold=1.0 means accept all distances (we normalize later)
        return self.db.search_symbols(query, limit=limit, threshold=1.0)


class UnifiedRanker:
    """Merge graph BFS candidates and vector search candidates into one ranked pool."""

    PREAMBLE_TOKENS = 100

    # Copied from GraphExpander to keep UnifiedRanker self-contained.
    _RELATION_PRIOR: dict[str, float] = {
        "CALLS_DIRECT_out": 1.0,  "CALLS_DIRECT_in": 1.2,
        "CALLS_DYNAMIC_out": 0.7, "CALLS_DYNAMIC_in": 0.9,
        "CALLS_INFERRED_out": 0.4,"CALLS_INFERRED_in": 0.5,
        "CALLS_SCOPED_out": 0.9,  "CALLS_SCOPED_in": 1.1,
        "CALLS_IMPORTED_out": 0.85,"CALLS_IMPORTED_in": 1.0,
        "CALLS_GUESS_out": 0.4,   "CALLS_GUESS_in": 0.5,
        "IMPLEMENTS": 1.1, "OVERRIDES": 1.1,
        "REFERENCES": 0.3, "DEPENDS_ON": 0.8, "IMPORTS": 0.6,
        "CALLS_out": 1.0,  "CALLS_in": 1.2,
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_target(self, symbol_name: str) -> SubgraphNode | None:
        """Fetch the primary symbol from Neo4j. Returns None if not found."""
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol {name: $name})
        RETURN s, coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range
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
        s = result["s"]
        token_cost = s.get("token_estimate", 0) or self._estimate_tokens_range(
            result.get("range") or s.get("range", [0, 0])
        )
        return SubgraphNode(
            uid=s["uid"],
            name=s["name"],
            file_path=result["file_path"],
            range=result.get("range") or s.get("range", [0, 0]),
            token_estimate=token_cost,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
            file_hash=result.get("file_hash", ""),
        )

    def rank(
        self,
        target: SubgraphNode,
        query: str,
        intent: Intent,
        budget: int,
        graph_pool_size: int = 50,
        vector_limit: int = 30,
    ) -> tuple[list[Candidate], dict]:
        """Return budget-fitting candidates sorted by blended score.

        Returns (candidates, budget_info).  The primary symbol itself is not
        in the returned list — the caller holds it separately.
        """
        # 1. Collect graph BFS candidates (pool-size-limited, not budget-limited)
        graph_pool = self._graph_candidates(target.uid, pool_size=graph_pool_size)

        # 2. Collect vector candidates for docs and symbols
        doc_pool = self._doc_candidates(query, limit=vector_limit)
        sym_vec_pool = self._sym_vec_candidates(query, limit=vector_limit)

        # 3. Fuse into unified pool, boosting docs linked via COVERS
        pool = self._fuse(graph_pool, doc_pool, sym_vec_pool, target.uid)

        # 4. Fill missing token costs for vector-only symbols
        self._fill_token_costs(pool)

        # 5. Assign intent weights
        intent_priors = self._intent_priors(intent)
        for c in pool:
            c.intent_weight = intent_priors.get(c.kind, 0.3)

        # 6. Normalize each track to [0, 1]
        self._normalize(pool)

        # 7. Sort by blended score
        pool.sort(key=self._blended, reverse=True)

        # 8. Greedy budget fill
        chosen: list[Candidate] = []
        spent = self.PREAMBLE_TOKENS + target.token_estimate
        pruned = 0

        for c in pool:
            if spent + c.token_cost > budget:
                pruned += 1
                continue
            chosen.append(c)
            spent += c.token_cost

        budget_info = {
            "limit": budget,
            "spent": spent,
            "reserved": self.PREAMBLE_TOKENS,
            "pruned": pruned,
            "pool_size": len(pool),
        }
        return chosen, budget_info

    def candidates_to_subgraph(
        self, target: SubgraphNode, candidates: list[Candidate], budget_info: dict
    ) -> tuple[Subgraph, list[DocChunk]]:
        """Split ranked candidates back into Subgraph + DocChunks for PromptCompiler."""
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
                        file_hash=c.file_hash,
                    )
                )
            else:
                docs.append(
                    DocChunk(
                        source_file=c.file_path,
                        chunk_id=c.uid,
                        content=c.content,
                        score=self._blended(c),
                        provenance=c.provenance,
                    )
                )
        return Subgraph(primary=target, nodes=nodes, budget=budget_info), docs

    # ------------------------------------------------------------------
    # Candidate collection
    # ------------------------------------------------------------------

    def _graph_candidates(self, target_uid: str, pool_size: int) -> list[Candidate]:
        """BFS from target, collecting up to pool_size candidates without token budget."""
        visited = {target_uid}
        candidates: list[Candidate] = []
        frontier: list[tuple[float, str, dict, str, bool, int]] = []

        for n in self._get_neighbors(target_uid, visited, distance=1):
            score = self._raw_graph_score(n, distance=1)
            heappush(frontier, (-score, n["uid"], n, n["rel_type"], n["outgoing"], 1))

        while frontier and len(candidates) < pool_size:
            neg_score, uid, neighbor, rel_type, outgoing, distance = heappop(frontier)
            score = -neg_score
            if uid in visited:
                continue
            visited.add(uid)

            token_cost = neighbor.get("token_estimate", 0) or self._estimate_tokens_range(
                neighbor.get("range", [0, 0])
            )
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
                provenance=[f"graph:{rel_type},depth={distance}"],
            )
            candidates.append(c)

            for nn in self._get_neighbors(uid, visited, distance=distance + 1):
                ns = self._raw_graph_score(nn, distance=distance + 1)
                heappush(frontier, (-ns, nn["uid"], nn, nn["rel_type"], nn["outgoing"], distance + 1))

        return candidates

    def _doc_candidates(self, query: str, limit: int) -> list[Candidate]:
        raw = self.vector.search_docs(query, limit=limit)
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

    def _sym_vec_candidates(self, query: str, limit: int) -> list[Candidate]:
        raw = self.vector.search_symbols(query, limit=limit)
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

    # ------------------------------------------------------------------
    # Fusion
    # ------------------------------------------------------------------

    def _fuse(
        self,
        graph: list[Candidate],
        docs: list[Candidate],
        sym_vec: list[Candidate],
        target_uid: str,
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
            for chunk_id, sym_uid in self._get_covers_links(doc_ids, pooled_sym_uids):
                if chunk_id in pool and sym_uid in pool:
                    doc_c = pool[chunk_id]
                    linked = pool[sym_uid].graph_score
                    doc_c.graph_score = max(doc_c.graph_score, linked * 0.6)
                    doc_c.provenance.append(f"graph:COVERS→{sym_uid[:8]}")

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
                result = session.run(
                    query, uids=missing_uids, workspace_id=self.workspace_id
                )
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

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    def _blended(self, c: Candidate) -> float:
        w = self.weights
        overlap_bonus = w.delta if c.overlap else 0.0
        return (
            w.alpha * c.graph_score
            + w.beta * c.semantic_score
            + w.gamma * c.intent_weight
            + overlap_bonus
            - w.epsilon * c.token_cost / 100
        )

    def _normalize(self, pool: list[Candidate]) -> None:
        """Min-max normalize graph_score and semantic_score independently."""
        g_vals = [c.graph_score for c in pool if c.graph_score > 0]
        s_vals = [c.semantic_score for c in pool if c.semantic_score > 0]

        g_min, g_max = (min(g_vals), max(g_vals)) if g_vals else (0.0, 1.0)
        s_min, s_max = (min(s_vals), max(s_vals)) if s_vals else (0.0, 1.0)
        g_range = (g_max - g_min) or 1.0
        s_range = (s_max - s_min) or 1.0

        for c in pool:
            if c.graph_score > 0:
                c.graph_score = (c.graph_score - g_min) / g_range
            if c.semantic_score > 0:
                c.semantic_score = (c.semantic_score - s_min) / s_range

    def _intent_priors(self, intent: Intent) -> dict[str, float]:
        if intent in (Intent.DEBUGGING, Intent.NAVIGATION):
            return {"symbol": 0.6, "doc": 0.2}
        elif intent in (Intent.NEW_FEATURE, Intent.DESIGN_QUESTION):
            return {"symbol": 0.2, "doc": 0.6}
        else:  # EXPLORATION, REFACTORING
            return {"symbol": 0.4, "doc": 0.4}

    def _raw_graph_score(self, neighbor: dict, distance: int) -> float:
        rel_type = neighbor["rel_type"]
        outgoing = neighbor["outgoing"]
        caller_count = neighbor["caller_count"]
        token_estimate = neighbor.get("token_estimate", 0)

        if rel_type in (
            "CALLS_DIRECT", "CALLS_SCOPED", "CALLS_IMPORTED",
            "CALLS_DYNAMIC", "CALLS_INFERRED", "CALLS_GUESS", "CALLS",
        ):
            base = rel_type if rel_type != "CALLS" else "CALLS_DIRECT"
            relation = f"{base}_out" if outgoing else f"{base}_in"
        elif rel_type in ("IMPLEMENTS", "OVERRIDES", "REFERENCES"):
            relation = rel_type
        elif rel_type == "DEPENDS_ON":
            relation = "DEPENDS_ON"
        elif rel_type == "IMPORTS":
            relation = "IMPORTS"
        else:
            relation = "DEPENDS_ON"

        r = self._RELATION_PRIOR.get(relation, 0.5)
        return (
            r
            + 0.3 * math.log1p(caller_count)
            - 0.5 * token_estimate / 100
            - 0.4 * distance
        )

    def _direction(self, rel_type: str, outgoing: bool) -> str:
        if rel_type in (
            "CALLS", "CALLS_DIRECT", "CALLS_SCOPED", "CALLS_IMPORTED",
            "CALLS_DYNAMIC", "CALLS_INFERRED", "CALLS_GUESS",
        ):
            return "callee" if outgoing else "caller"
        elif rel_type == "DEPENDS_ON":
            return "type"
        elif rel_type == "IMPORTS":
            return "import"
        elif rel_type in ("IMPLEMENTS", "OVERRIDES", "REFERENCES"):
            return rel_type.lower()
        return "sibling"

    # ------------------------------------------------------------------
    # Neo4j helpers
    # ------------------------------------------------------------------

    def _get_neighbors(self, uid: str, visited: set, distance: int) -> list[dict]:
        query = """
        MATCH (s:Symbol {uid: $uid})-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES]-(n:Symbol)
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

    def _get_covers_links(
        self, chunk_ids: list[str], symbol_uids: set[str]
    ) -> list[tuple[str, str]]:
        if not chunk_ids or not symbol_uids:
            return []
        query = """
        MATCH (a:DocAnchor {workspace_id: $workspace_id})-[:COVERS]->(s:Symbol)
        WHERE a.chunk_id IN $chunk_ids AND s.uid IN $symbol_uids
        RETURN a.chunk_id AS chunk_id, s.uid AS sym_uid
        """
        try:
            with self.db.driver.session() as session:
                result = session.run(
                    query,
                    chunk_ids=chunk_ids,
                    symbol_uids=list(symbol_uids),
                    workspace_id=self.workspace_id,
                )
                return [(r["chunk_id"], r["sym_uid"]) for r in result]
        except Exception:
            return []

    @staticmethod
    def _estimate_tokens_range(range_: list) -> int:
        if not range_ or len(range_) < 2:
            return 0
        return max(1, int((int(range_[1]) - int(range_[0]) + 1) * 8))
