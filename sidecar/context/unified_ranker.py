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


# Path fragments that almost always signal noise relative to "explain how
# this works" framework questions. Multiplicative downrank — never a hard
# skip, so questions specifically about testing or examples still land.
_NOISE_PATH_PATTERNS = (
    "/tests/",
    "/test_",
    "/__tests__/",
    "/docs_src/",
    "/examples/",
    "/example/",
)
_NOISE_NAME_PREFIXES = ("test_",)
_NOISE_NAME_SUBSTRINGS = ("tutorial",)
_NOISE_FACTOR = 0.15


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


def compute_noise_factor(file_path: str, name: str) -> float:
    """Multiplicative score multiplier in [0, 1].

    Returns 1.0 for clean candidates and ``_NOISE_FACTOR`` for ones that
    look like tests, tutorials, or framework examples — those rarely
    answer "how does X work" questions but otherwise consume budget.
    """
    if _path_is_noisy(file_path) or _name_is_noisy(name):
        return _NOISE_FACTOR
    return 1.0


@dataclass
class RankerWeights:
    alpha: float = 1.0    # graph structural score
    beta: float = 0.8     # semantic similarity score
    gamma: float = 0.4    # intent tier prior
    delta: float = 0.5    # overlap bonus (both signals fired)
    epsilon: float = 0.3  # token cost penalty per 100 tokens


DEFAULT_WEIGHTS = RankerWeights()


@dataclass
class Candidate:
    kind: str                              # "symbol" | "doc"
    uid: str                               # symbol UID or doc chunk_id
    token_cost: int
    graph_score: float = 0.0
    semantic_score: float = 0.0
    intent_weight: float = 0.0
    noise_factor: float = 1.0              # multiplicative downrank for tests/tutorials
    provenance: list[str] = field(default_factory=list)
    # symbol metadata
    name: str = ""
    file_path: str = ""
    range: list[int] = field(default_factory=lambda: [0, 0])
    render_mode: str = "full"
    evidence_role: str = ""
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

    # Per-intent floors: we must not stop based on marginal gain
    # until we hit these minimums to ensure grounding.
    _INTENT_FLOORS = {
        Intent.NAVIGATION: 500,
        Intent.EXPLORATION: 1200,
        Intent.DEBUGGING: 1500,
        Intent.NEW_FEATURE: 2500,
        Intent.REFACTORING: 2500,
        Intent.DESIGN_QUESTION: 3500,
        Intent.IMPACT_ANALYSIS: 3000,
    }

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
        "SEMANTIC_HINT_out": 1.3,
        "SEMANTIC_HINT_in": 1.3,
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
        WITH s, f, c,
             CASE 
               WHEN f.path CONTAINS '/tests/' OR f.path CONTAINS '/test_' THEN 3
               WHEN f.path CONTAINS '/docs/' OR f.path CONTAINS '/examples/' THEN 2
               WHEN f.path CONTAINS '/fastapi/' THEN 0
               ELSE 1 
             END AS priority
        RETURN s, coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range
        ORDER BY priority ASC, size(f.path) ASC
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
        graph_pool_size: int = 200,
        vector_limit: int = 100,
    ) -> tuple[list[Candidate], dict, str, list[dict], list[str]]:
        """Return budget-fitting candidates sorted by blended score.

        Returns (candidates, budget_info).  The primary symbol itself is not
        in the returned list — the caller holds it separately.
        """
        # 1. Collect graph BFS candidates (pool-size-limited, not budget-limited)
        graph_pool = self._graph_candidates(
            target.uid, pool_size=graph_pool_size, intent=intent
        )

        # 2. Collect vector candidates for docs and symbols
        doc_pool = self._doc_candidates(query, limit=vector_limit)
        sym_vec_pool = self._sym_vec_candidates(query, limit=vector_limit)

        # 3. Doc-bridge: framework-semantics edges static graph cannot see.
        # When ``Depends`` (a marker class) and ``solve_dependencies`` (its
        # runtime consumer) are co-mentioned in the same DocAnchor, the
        # bridge surfaces the consumer even when no Symbol→Symbol edge
        # connects them. Seeds are the target plus any strong graph hits.
        bridge_seeds = {target.uid} | {
            c.uid for c in graph_pool if c.graph_score > 0.5
        }
        excluded = {target.uid} | {c.uid for c in graph_pool}
        bridge_pool_h1 = self._doc_bridge_candidates(
            bridge_seeds, excluded, limit=30, hop_decay=1.0
        )

        # 3b. 2-hop bridge is currently disabled by default to minimize noise.
        bridge_pool = bridge_pool_h1

        # 4. Fuse into unified pool, boosting docs linked via COVERS
        pool = self._fuse(
            graph_pool, doc_pool, sym_vec_pool, target.uid, bridge_pool=bridge_pool
        )

        # 5. Fill missing token costs for vector-only symbols
        self._fill_token_costs(pool)

        # 6. Assign intent weights and noise factors
        mechanism = self._determine_mechanism(target)
        required_roles = self._get_required_roles(mechanism)

        intent_priors = self._intent_priors(intent)
        for c in pool:
            c.evidence_role = self._infer_role(c)
            c.intent_weight = intent_priors.get(c.kind, 0.3)
            if intent == Intent.IMPACT_ANALYSIS:
                c.noise_factor = 1.0  # tests/examples are load-bearing for impact questions
            else:
                c.noise_factor = compute_noise_factor(c.file_path, c.name)

        # 7. Normalize each track to [0, 1]
        self._normalize(pool)

        # 8. Sort by blended score
        # Optimization: Sort by blended score + potential to fill a missing role.
        pool.sort(
            key=lambda c: self._blended(c) + (0.5 if c.evidence_role in required_roles else 0.0),
            reverse=True
        )

        # 9. Optimal Context Selection: Mechanism-Specific Evidence Gating
        chosen: list[Candidate] = []
        spent = self.PREAMBLE_TOKENS + target.token_estimate
        pruned_details = []
        chosen_files = {target.file_path}
        fulfilled_roles = {self._infer_role(target)}

        stopped_reason = "pool_exhausted"
        min_floor = self._INTENT_FLOORS.get(intent, 1200)
        min_gain = 0.12  # Threshold for stopping
        low_gain_floor = 0.02  # Protect against pure junk
        useful_candidates_seen = 0

        for c in pool:
            gain = self._calculate_marginal_gain(c, chosen, target)

            # Selection Gating Logic: Mechanism-Aware
            missing_roles = set(required_roles) - fulfilled_roles
            fills_role = c.evidence_role in required_roles and c.evidence_role not in fulfilled_roles
            adds_new_file = c.file_path not in chosen_files
            is_bridge = c.relation in ("DOC_BRIDGE", "SEMANTIC_HINT")
            is_strong_relation = c.relation in ("CALLS_DIRECT", "CALLS_SCOPED", "DEPENDS_ON", "IMPLEMENTS", "OVERRIDES")

            # Determine if this candidate provides any unique reasoning signal
            is_useful = (
                fills_role
                or is_bridge
                or is_strong_relation
                or (self._blended(c) > 0.15)
            )

            if is_useful:
                useful_candidates_seen += 1

            if gain < min_gain:
                # Only break if floor is met AND no required roles are missing
                if spent >= min_floor and not missing_roles:
                    stopped_reason = "marginal_gain_threshold"
                    break

                if not is_useful:
                    continue
                if gain < low_gain_floor:
                    continue

            # OPTIMAL CONTEXT: Tiered snippets
            # If score is low or depth is high, mark for signature-only resolution.
            # We update the cost here so budget accounting remains accurate.
            potential_cost = c.token_cost
            if c.depth >= 2 and gain < 0.25:
                potential_cost = min(c.token_cost, 80)

            if spent + potential_cost > budget:
                pruned_details.append({
                    "name": c.name, "file": c.file_path, "relation": c.relation,
                    "role": c.evidence_role, "gain": round(gain, 3), "tokens": potential_cost,
                    "reason": "budget_exhausted", "provenance": c.provenance
                })
                continue

            if c.depth >= 2 and gain < 0.25:
                c.render_mode = "signature_only"
                c.token_cost = potential_cost

            chosen.append(c)
            spent += potential_cost
            chosen_files.add(c.file_path)
            fulfilled_roles.add(c.evidence_role)

        # If we ran out of useful candidates before hitting the floor, adjust the
        # stopped reason. For sparse targets like `Depends` (marker classes), the floor
        # may be genuinely unachievable from the graph.
        if stopped_reason == "pool_exhausted" and spent < min_floor:
            if useful_candidates_seen < 3:
                stopped_reason = "floor_unfilled_sparse_target"
            else:
                stopped_reason = "floor_unfilled_no_useful_candidates"

        missing_roles = [r for r in required_roles if r not in fulfilled_roles]

        budget_info = {
            "limit": budget,
            "spent": spent,
            "floor": min_floor,
            "reserved": self.PREAMBLE_TOKENS,
            "pool_size": len(pool),
        }
        return chosen, budget_info, stopped_reason, pruned_details, missing_roles

    def candidates_to_subgraph(
        self, target: SubgraphNode, candidates: list[Candidate], budget_info: dict, 
        stopped_reason: str = "", pruned_details: list = None
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
                        render_mode=c.render_mode,
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
        return Subgraph(
            primary=target, 
            nodes=nodes, 
            budget=budget_info, 
            stopped_reason=stopped_reason, 
            pruned_details=pruned_details or []
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
                ns = self._raw_graph_score(
                    nn, distance=distance + 1, chain_pursuit=chain_pursuit
                )
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
        raw = self._filter_doc_hits_to_workspace(
            self.vector.search_docs(query, limit=limit)
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

    def _sym_vec_candidates(self, query: str, limit: int) -> list[Candidate]:
        raw = self._filter_symbol_hits_to_workspace(
            self.vector.search_symbols(query, limit=limit)
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

        Static call/depends edges miss framework-semantics relationships
        (``Depends`` ↔ ``solve_dependencies`` in FastAPI). Doc anchors
        already record these by name when ``_extract_identifiers`` saw
        both names in the same chunk and ``COVERS`` was created for each.

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
        MATCH (a:DocAnchor)-[:COVERS]->(s:Symbol)
        WHERE s.uid IN $seed_uids
          AND coalesce(a.workspace_id, $workspace_id) = $workspace_id
        MATCH (a)-[:COVERS]->(other:Symbol)
        WHERE NOT other.uid IN $excluded
        OPTIONAL MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(other)
        WITH other,
             coalesce(f.path, '<unknown>') AS file_path,
             coalesce(c.range, other.range, [0, 0]) AS range,
             coalesce(other.token_estimate, 0) AS token_estimate,
             coalesce(f.hash, '') AS file_hash,
             count(DISTINCT a) AS bridge_strength
        WHERE bridge_strength >= $min_strength
        RETURN other.uid AS uid,
               other.name AS name,
               file_path,
               range,
               token_estimate,
               file_hash,
               bridge_strength
        ORDER BY bridge_strength DESC
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
            score = math.log1p(strength) * hop_decay
            token_cost = (
                int(r["token_estimate"])
                or self._estimate_tokens_range(r.get("range") or [0, 0])
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
                    provenance=[f"doc-bridge:{hop_label},strength={strength}"],
                )
            )
        return candidates

    def _filter_doc_hits_to_workspace(self, hits: list[dict]) -> list[dict]:
        """Keep only doc hits whose file belongs to the active workspace."""
        paths = sorted({hit.get("file_path") for hit in hits if hit.get("file_path")})
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
                    for record in session.run(
                        query, workspace_id=self.workspace_id, paths=paths
                    )
                }
        except Exception:
            return hits
        return [hit for hit in hits if hit.get("file_path") in allowed]

    def _filter_symbol_hits_to_workspace(self, hits: list[dict]) -> list[dict]:
        """Keep only symbol vector hits that are present in the active workspace."""
        uids = sorted({hit.get("uid") for hit in hits if hit.get("uid")})
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
                    for record in session.run(
                        query, workspace_id=self.workspace_id, uids=uids
                    )
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
        # Noise factor multiplies the *positive* contributions only — the
        # token cost penalty stays full so noisy big symbols are even
        # harder to justify. Equivalent to "noisy candidate has to be
        # ~3× more relevant to break tie with a clean one."
        positive = (
            w.alpha * c.graph_score
            + w.beta * c.semantic_score
            + w.gamma * c.intent_weight
            + overlap_bonus
        )
        return positive * c.noise_factor - w.epsilon * c.token_cost / 100

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
        elif intent == Intent.IMPACT_ANALYSIS:
            return {"symbol": 0.3, "doc": 0.5}  # tests/examples are load-bearing
        else:  # EXPLORATION, REFACTORING
            return {"symbol": 0.4, "doc": 0.4}

    def _raw_graph_score(
        self,
        neighbor: dict,
        distance: int,
        *,
        chain_pursuit: bool = False,
    ) -> float:
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
        elif rel_type == "SEMANTIC_HINT":
            relation = "SEMANTIC_HINT_out" if outgoing else "SEMANTIC_HINT_in"
        else:
            relation = "DEPENDS_ON"

        r = self._RELATION_PRIOR.get(relation, 0.5)

        # Chain pursuit: drop the distance penalty for outgoing CALLS_* so a
        # depth-5 chain can still beat a noisy depth-1 sibling. Other edge
        # types keep the original 0.4 penalty so we don't accidentally pull
        # in distant unrelated symbols.
        # We now include SEMANTIC_HINT in chain pursuit to favor dependency injection links.
        if (chain_pursuit and self._is_outgoing_call(rel_type, outgoing)) or rel_type == "SEMANTIC_HINT":
            distance_penalty = 0.15 * distance
        else:
            distance_penalty = 0.4 * distance

        return (
            r
            + 0.3 * math.log1p(caller_count)
            # DEBT: The previous -0.5 penalty was too aggressive for "God Object"
            # functions (like solve_dependencies). We reduce it here so structural
            # importance can outweigh raw token size during pool collection.
            - 0.1 * token_estimate / 100
            - distance_penalty
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

    def _calculate_marginal_gain(self, c: Candidate, chosen: list[Candidate], target: SubgraphNode) -> float:
        """marginal_gain = base_score + role_bonus + coverage_bonus + bridge_bonus - redundancy_penalty"""
        base_score = self._blended(c)
        
        # 1. Role Bonus: Does this symbol fulfill a missing requirement for the mechanism?
        role_bonus = 0.0
        mechanism = self._determine_mechanism(target)
        required_roles = self._get_required_roles(mechanism)
        if c.evidence_role in required_roles:
            is_fulfilled = any(cc.evidence_role == c.evidence_role for cc in chosen)
            if not is_fulfilled:
                role_bonus = 0.5  # High-priority evidence signal

        # 2. Coverage Bonus: Does this symbol complete a structural chain?
        # Boost symbols that are semantically hinted (FastAPI Depends) 
        # or are direct implementations of the target's interfaces.
        coverage_bonus = 0.0
        if "SEMANTIC_HINT" in (c.relation or ""):
            coverage_bonus += 0.2
        if c.relation in ("IMPLEMENTS", "OVERRIDES"):
            coverage_bonus += 0.15
            
        # 3. Bridge Bonus: Boost symbols discovered via DocBridge co-occurrence
        # as they often represent runtime connections static analysis misses.
        bridge_bonus = 0.1 if "doc-bridge" in "".join(c.provenance) else 0.0

        # 4. Redundancy Penalty: Diminishing returns for many symbols in the same file.
        same_file_count = sum(1 for cc in chosen if cc.file_path == c.file_path)
        redundancy_penalty = min(0.4, 0.15 * same_file_count)

        return base_score + role_bonus + coverage_bonus + bridge_bonus - redundancy_penalty

    def _infer_role(self, c: Candidate | SubgraphNode) -> str:
        """Heuristic to map symbols to framework reasoning roles."""
        if hasattr(c, 'kind') and c.kind == "doc": return "docs_or_concept"
        name = c.name.lower()

        # 1. Dependency Injection Roles (Specific marker checks first)
        if name == "depends" and "fastapi/params.py" in c.file_path:
            return "marker_or_config"
        if name == "security" and "/fastapi/security/" in c.file_path:
            return "marker_or_config"
        if name == "depends": 
            return "public_entrypoint"

        # 2. Route Registration Roles
        if name in ("fastapi", "apirouter"): return "public_entrypoint"
        if name in ("api_route", "add_api_route", "websocket_route"): 
            return "registration_step"
        if name in ("apiroute", "apiwebsocketroute"): 
            return "route_object"

        # 3. Lifecycle & Execution loop
        if name in ("dependant", "get_dependant", "get_flat_dependant"): 
            return "intermediate_model"
        if name == "solve_dependencies": 
            return "dependency_solver"
        if name == "run_endpoint_function": 
            return "runtime_executor"
        if name == "serialize_response": 
            return "response_serializer"
        if name in ("get_request_handler", "get_route_handler", "request_response"): 
            return "handler_or_lifecycle"
            
        return "related_implementation"

    def _determine_mechanism(self, target: SubgraphNode) -> str:
        """Map target symbol to a known framework mechanism."""
        name = target.name.lower()
        if name in ("fastapi", "apirouter", "add_api_route", "api_route"):
            return "fastapi_route_registration"
        if name in ("depends", "get_dependant", "dependant"):
            return "fastapi_dependency_injection"
        if name in ("run_endpoint_function", "serialize_response", "solve_dependencies"):
            return "fastapi_endpoint_execution"
        return "generic"

    def _get_required_roles(self, mechanism: str) -> list[str]:
        """Return the set of evidence roles required for a minimally sufficient context."""
        roles = []
        if mechanism == "fastapi_route_registration":
            roles = ["public_entrypoint", "registration_step", "route_object", "handler_or_lifecycle"]
        elif mechanism == "fastapi_dependency_injection":
            roles = ["public_entrypoint", "marker_or_config", "intermediate_model", "dependency_solver", "handler_or_lifecycle"]
        elif mechanism == "fastapi_endpoint_execution":
            roles = ["runtime_executor", "dependency_solver", "response_serializer"]
        else:
            roles = ["public_entrypoint", "runtime_executor", "handler_or_lifecycle"]

        # Docs are universally useful for grounding concepts
        roles.append("docs_or_concept")
        return roles

    # ------------------------------------------------------------------
    # Neo4j helpers
    # ------------------------------------------------------------------

    def _get_neighbors(self, uid: str, visited: set, distance: int) -> list[dict]:
        query = """
        MATCH (s:Symbol {uid: $uid})-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]-(n:Symbol)
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
