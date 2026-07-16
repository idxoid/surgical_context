"""Role-driven retrieval primitive.

Sits between L4 roles and the actual `/ask`-style consumer. Given a role
name (and optionally a free-text query), returns ranked candidate
symbols from a workspace whose persisted L3 contracts satisfy that role.

The axis pipeline is the default ``/ask`` provider (the legacy
``unified_ranker`` / ranking cascade was removed 2026-06-15). This module is
the axis role-retrieval entry point. The ranking is intentionally simple — vector
distance for semantic narrowing plus a small structural boost when more
contracts in the role fire on the symbol — so the role-match dimension
is observable in the result ordering instead of buried inside a black
box.

Workflow:

  1. Caller picks a role (``routing_surface``, ``binding_surface``, …).
     The L4 role map names the contracts that satisfy it.
  2. We scan the workspace's Lance symbol rows, parse each
     ``axis_contracts_json``, and keep rows whose contract set
     intersects the role.
  3. (Optional) embed the query text and reweight by L2 vector
     distance — symbols semantically close to the query rise.

This is read-only; no graph writes, no Lance mutations.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, cast

import lancedb

from context_engine.axis.role_resolver import ROLE_EVIDENCE_MAP
from context_engine.database.lancedb_client import DB_PATH

_SCAN_CACHE_ENABLED = os.getenv("LANCEDB_WORKSPACE_SCAN_CACHE", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_SCAN_CACHE: dict[tuple[str, str, bool, bool], WorkspaceScan] = {}


@dataclass(frozen=True)
class RoleCandidate:
    """One symbol satisfying a role, with both evidence channels (the
    L3 contracts and the L2 container kinds that fired) plus the
    ranking score components."""

    uid: str
    name: str
    file_path: str
    role: str
    satisfying_contracts: tuple[str, ...]
    satisfying_kinds: tuple[str, ...]
    contract_count: int
    kind_count: int
    vector_distance: float | None
    score: float
    qualified_name: str = ""
    depth: int | None = None
    edge_type: str = ""
    utility_score: float | None = None
    # Raw query↔node cosine similarity in [-1, 1].  ``None`` means that
    # semantic relevance has not yet been folded into ``score``.  Keeping
    # this separate prevents the final pool reranker from applying the same
    # signal twice to role/vector/lookahead seeds.
    query_similarity: float | None = None
    # Structural component before semantic blending, when the producer has
    # one.  Older/specialised producers may leave it unset; the pool reranker
    # then treats their current ``score`` as the structural baseline.
    graph_score: float | None = None
    # Retrieval-only provenance. Semantic fragments resolve to their owner
    # symbol; these absolute source intervals remain priors for the later
    # within-symbol line ranker and benchmark telemetry.
    retrieval_channels: tuple[str, ...] = ()
    retrieval_spans: tuple[tuple[int, int], ...] = ()
    exact_symbol_match: bool = False
    # Query-time IDF-weighted term coverage of the best lexical body windows.
    # Kept separate from structural/query-vector score until span gold proves
    # it is selective enough to influence seed ordering or reserves.
    lexical_span_score: float | None = None
    # Selection-only provenance accumulated when the same UID is supported by
    # several intent/pseudo-role pools.  The historical flattening pass kept
    # only the first role, which hid a useful pre-graph consensus signal.
    supporting_roles: tuple[str, ...] = ()
    # Stable, low-cardinality reason tags attached by the pre-graph selector.
    # They are telemetry rather than ranking inputs for later graph/token
    # stages, so a benchmark can explain why a seed survived the cap.
    selection_reasons: tuple[str, ...] = ()
    # Experimental post-selection rank signal.  Kept explicit so benchmark
    # telemetry can distinguish cross-role consensus from the producer's raw
    # structural/query score.
    role_consensus_bonus: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "file_path": self.file_path,
            "role": self.role,
            "satisfying_contracts": list(self.satisfying_contracts),
            "satisfying_kinds": list(self.satisfying_kinds),
            "contract_count": self.contract_count,
            "kind_count": self.kind_count,
            "vector_distance": self.vector_distance,
            "score": self.score,
            "qualified_name": self.qualified_name,
            "depth": self.depth,
            "edge_type": self.edge_type,
            "utility_score": self.utility_score,
            "query_similarity": self.query_similarity,
            "graph_score": self.graph_score,
            "retrieval_channels": list(self.retrieval_channels),
            "retrieval_spans": list(self.retrieval_spans),
            "exact_symbol_match": self.exact_symbol_match,
            "lexical_span_score": self.lexical_span_score,
            "supporting_roles": list(self.supporting_roles),
            "selection_reasons": list(self.selection_reasons),
            "role_consensus_bonus": self.role_consensus_bonus,
        }


def _structural_score(
    matched_contracts: int,
    matched_kinds: int,
    total_contracts: int,
    total_kinds: int,
) -> float:
    """``[0, 1]`` proportion of the role's evidence that fired on this
    symbol, computed across both contracts and kinds.

    Contracts are weighted slightly higher (1.0) than kinds (0.6)
    because contracts include the use-proof side of binding while kinds
    are existence-only. A symbol with a contract match outranks a
    symbol that only matches by kind, when both are otherwise tied.
    """
    contract_weight = 1.0
    kind_weight = 0.6
    total = total_contracts * contract_weight + total_kinds * kind_weight
    if total <= 0:
        return 0.0
    matched = matched_contracts * contract_weight + matched_kinds * kind_weight
    return min(1.0, matched / total)


def _semantic_score(distance: float | None) -> float:
    """Map unit-vector L2 distance to cosine similarity in ``[0, 1]``.

    SentenceTransformer symbol vectors are L2-normalised, therefore
    ``cos(a, b) = 1 - ||a-b||²/2``.  The shifted value keeps the public score
    range stable while preserving cosine ordering.
    """
    if distance is None:
        return 0.0
    cosine = _cosine_from_l2_distance(float(distance))
    return max(0.0, min(1.0, (1.0 + cosine) / 2.0))


def _cosine_from_l2_distance(distance: float | None) -> float | None:
    """Return raw cosine similarity for L2-normalised embeddings."""
    if distance is None:
        return None
    return max(-1.0, min(1.0, 1.0 - float(distance) ** 2 / 2.0))


def _combined_score(
    structural: float,
    semantic: float,
    has_query: bool,
) -> float:
    """If a query was supplied, weight equally; otherwise structural only."""
    if not has_query:
        return structural
    return 0.5 * structural + 0.5 * semantic


# Structural file-tier ranking weights (see docs/file_tier_signal.md). The
# ``test`` tier never reaches the seed ranker — it is fenced out of the
# scan (or routed to the impact pass) — so these tables cover only the
# non-test tiers that DO compete for seed slots. ``core`` (the
# answer-bearing default) is the anchor at 1.0; the rest are demoted so an
# example app / re-export / stub cannot out-rank real library code in seed
# retrieval. For impact / trace modes the demotion is relaxed: an
# "what examples/docs are affected" question legitimately wants them.
_TIER_WEIGHT_DEMOTE: dict[str, float] = {
    "core": 1.0,
    "reexport": 0.5,
    "stub": 0.5,
    "doc": 0.3,
    "example": 0.2,
    "test": 0.0,
}
_TIER_WEIGHT_MODE: dict[str, float] = {
    "core": 1.0,
    "reexport": 0.5,
    "stub": 0.5,
    "doc": 0.6,
    "example": 0.6,
    "test": 1.0,
}
_MODE_ROLES = frozenset({"impact_analysis", "trace_dependency"})


def _tier_weight(tier: str | None, *, impact_mode: bool) -> float:
    table = _TIER_WEIGHT_MODE if impact_mode else _TIER_WEIGHT_DEMOTE
    return table.get(tier or "core", 1.0)


@dataclass
class WorkspaceScan:
    """One workspace's symbol rows from a single Lance scan.

    ``rows`` are metadata-only dicts (NO vector) carrying pre-parsed
    ``_contracts`` / ``_kinds`` sets and a ``_idx`` aligned to
    ``vectors``. ``vectors`` is an ``(N, dim)`` numpy matrix (row ``i``
    ↔ ``rows[i]``) or ``None`` when vectors were not requested. Keeping
    the 384-dim vectors in a contiguous numpy matrix instead of Python
    lists is the whole point — distance is one vectorised pass, not a
    per-row Python loop, and ``to_pylist`` no longer materialises the
    heavy column.
    """

    rows: list[dict]
    vectors: Any | None  # np.ndarray | None — Any to avoid a hard numpy import at module scope
    signature_vectors: Any | None = None  # np.ndarray | None — optional signature facet
    rows_by_uid: dict[str, dict] = field(default_factory=dict)
    contract_index: dict[str, tuple[int, ...]] = field(default_factory=dict)
    kind_index: dict[str, tuple[int, ...]] = field(default_factory=dict)
    lexical_index: Any | None = None

    def __post_init__(self) -> None:
        for index, row in enumerate(self.rows):
            row.setdefault("_idx", index)
        if not self.rows_by_uid:
            self.rows_by_uid = {
                str(row.get("uid") or ""): row for row in self.rows if row.get("uid")
            }
        if self.rows and not self.contract_index:
            contract_hits: dict[str, list[int]] = defaultdict(list)
            kind_hits: dict[str, list[int]] = defaultdict(list)
            for i, row in enumerate(self.rows):
                for contract in row.get("_contracts") or ():
                    contract_hits[contract].append(i)
                for kind in row.get("_kinds") or ():
                    kind_hits[kind].append(i)
            self.contract_index = {k: tuple(v) for k, v in contract_hits.items()}
            self.kind_index = {k: tuple(v) for k, v in kind_hits.items()}


@dataclass(frozen=True)
class QueryScoringContext:
    """Request-local query vector and vectorised query↔symbol scores.

    The workspace matrix is scanned once per request.  Every retrieval pass
    then performs an O(1) uid lookup instead of embedding the same question
    and recomputing the full distance array independently.
    """

    scan: WorkspaceScan
    query_vector: Any
    distances: Any
    similarities: Any

    def distance_for(self, uid: str) -> float | None:
        row = self.scan.rows_by_uid.get(uid)
        if row is None:
            return None
        idx = row.get("_idx")
        if idx is None:
            return None
        return float(self.distances[int(idx)])

    def similarity_for(self, uid: str) -> float | None:
        row = self.scan.rows_by_uid.get(uid)
        if row is None:
            return None
        idx = row.get("_idx")
        if idx is None:
            return None
        return float(self.similarities[int(idx)])


def invalidate_workspace_scan_cache(workspace_id: str | None = None) -> None:
    """Drop cached Lance scans after index mutations."""
    if workspace_id is None:
        _SCAN_CACHE.clear()
        return
    for key in [k for k in _SCAN_CACHE if k[0] == workspace_id]:
        del _SCAN_CACHE[key]


def _scan_cache_key(
    workspace_id: str,
    lance_db_path: str,
    include_tests: bool,
    with_vector: bool,
) -> tuple[str, str, bool, bool]:
    return (workspace_id, lance_db_path, include_tests, with_vector)


def _row_indices_for_evidence(scan: WorkspaceScan, evidence) -> tuple[int, ...]:
    seen: set[int] = set()
    for contract in evidence.contracts:
        seen.update(scan.contract_index.get(contract, ()))
    for kind in evidence.kinds:
        seen.update(scan.kind_index.get(kind, ()))
    return tuple(sorted(seen))


def _cached_workspace_scan(cache_key: tuple[str, str, bool, bool]) -> WorkspaceScan | None:
    if not _SCAN_CACHE_ENABLED:
        return None
    return _SCAN_CACHE.get(cache_key)


def _store_workspace_scan(
    cache_key: tuple[str, str, bool, bool], scan: WorkspaceScan
) -> WorkspaceScan:
    if _SCAN_CACHE_ENABLED:
        _SCAN_CACHE[cache_key] = scan
    return scan


def _workspace_filter_sql(workspace_id: str) -> str:
    ws_quoted = workspace_id.replace("'", "''")
    return f"workspace_id = '{ws_quoted}'"


def _open_scan_symbols_table(
    workspace_id: str,
    lance_db_path: str,
    lance: Any | None,
) -> tuple[Any, str | None]:
    from context_engine.database.lance_workspace_tables import (
        workspace_partition_table_exists,
        workspace_partition_table_name,
        workspace_partitioned_enabled,
    )
    from context_engine.index_profile import active_index_profile

    profile = active_index_profile()
    partitioned = workspace_partitioned_enabled()

    if lance is not None and hasattr(lance, "symbols_table"):
        table = lance.symbols_table(workspace_id)
        return table, None

    db = lancedb.connect(lance_db_path)
    base_table = profile.symbols_table
    if partitioned and workspace_partition_table_exists(db, base_table, workspace_id):
        return db.open_table(workspace_partition_table_name(base_table, workspace_id)), None
    return db.open_table(base_table), _workspace_filter_sql(workspace_id)


def _schema_has_column(table, name: str) -> bool:
    try:
        return name in set(table.schema.names)
    except Exception:
        return False


def _scan_reader_columns(table, *, with_vector: bool) -> list[str]:
    columns = [
        "uid",
        "name",
        "file_path",
        "axis_contracts_json",
        "axis_container_kinds_json",
        "workspace_id",
    ]
    if _schema_has_column(table, "qualified_name"):
        columns.append("qualified_name")
    if _schema_has_column(table, "file_tier"):
        columns.append("file_tier")
    if _schema_has_column(table, "symbol_kind"):
        columns.append("symbol_kind")
    if with_vector:
        columns.append("vector")
        if _schema_has_column(table, "signature_vector"):
            columns.append("signature_vector")
    return columns


def _read_scan_arrow(table, columns: list[str], table_filter: str | None):
    lance_reader = table.to_lance()
    if table_filter:
        return lance_reader.to_table(columns=columns, filter=table_filter)
    return lance_reader.to_table(columns=columns)


def _vector_matrix_from_arrow(arrow, col_name: str):
    if col_name not in arrow.column_names or not arrow.num_rows:
        return None
    try:
        import numpy as np

        col = arrow.column(col_name).combine_chunks()
        return np.asarray(col.values.to_numpy(zero_copy_only=False)).reshape(arrow.num_rows, -1)
    except Exception:
        return None


def _extract_scan_vector_matrices(arrow, *, with_vector: bool) -> tuple[Any, Any, list[str]]:
    if not with_vector or "vector" not in arrow.column_names or not arrow.num_rows:
        return None, None, []
    drop_cols = ["vector"]
    vectors_all = _vector_matrix_from_arrow(arrow, "vector")
    signature_vectors_all = None
    if "signature_vector" in arrow.column_names:
        signature_vectors_all = _vector_matrix_from_arrow(arrow, "signature_vector")
        drop_cols.append("signature_vector")
    return vectors_all, signature_vectors_all, drop_cols


def _parse_axis_contract_kinds(row: dict) -> tuple[set[str], set[str]]:
    try:
        contract_objs = json.loads(row.get("axis_contracts_json") or "[]")
    except json.JSONDecodeError:
        contract_objs = []
    try:
        kind_objs = json.loads(row.get("axis_container_kinds_json") or "[]")
    except json.JSONDecodeError:
        kind_objs = []
    contracts = {str(c.get("contract") or "") for c in contract_objs}
    kinds = {str(k.get("kind") or "") for k in kind_objs}
    return contracts, kinds


def _build_kept_scan_rows(meta: list[dict], *, include_tests: bool) -> tuple[list[dict], list[int]]:
    from context_engine.axis.test_file_filter import is_test_path

    kept_rows: list[dict] = []
    kept_idx: list[int] = []
    for i, row in enumerate(meta):
        if not include_tests and is_test_path(str(row.get("file_path") or "")):
            continue
        contracts, kinds = _parse_axis_contract_kinds(row)
        row["_contracts"] = contracts
        row["_kinds"] = kinds
        row["_idx"] = len(kept_rows)
        kept_rows.append(row)
        kept_idx.append(i)
    return kept_rows, kept_idx


def _subset_scan_matrix(matrix, kept_idx: list[int]):
    if matrix is None or not kept_idx:
        return None
    return matrix[kept_idx]


def scan_workspace_rows(
    workspace_id: str,
    *,
    lance_db_path: str = DB_PATH,
    lance: Any | None = None,
    include_tests: bool = False,
    with_vector: bool = True,
) -> WorkspaceScan:
    """One workspace-scoped Lance scan, JSON parsed once, vectors kept
      in a numpy matrix.

      The retrieval layer used to scan the *entire* symbol table (every
      workspace, 384-dim vector column included) and filter in Python —
      once per role, three+ times per question. This reads only the
      workspace's rows (``workspace_id`` pushed down into Lance as a C++
      bitmask), runs the test fence once, parses each row's
      contracts/kinds JSON once into sets, and — critically — extracts
      the vector column as a contiguous numpy matrix WITHOUT
      materialising it into Python dicts. Metadata ``to_pylist`` then
      touches only light columns.

    When ``LANCEDB_WORKSPACE_PARTITIONED`` is on (default), the scan opens
    the per-workspace physical table directly — no ``workspace_id`` filter.

    Repeated reads for the same workspace reuse an in-process scan cache
    (``LANCEDB_WORKSPACE_SCAN_CACHE``, default on) until the index mutates.
    """
    cache_key = _scan_cache_key(workspace_id, lance_db_path, include_tests, with_vector)
    cached = _cached_workspace_scan(cache_key)
    if cached is not None:
        return cached

    table, table_filter = _open_scan_symbols_table(workspace_id, lance_db_path, lance)
    columns = _scan_reader_columns(table, with_vector=with_vector)
    arrow = _read_scan_arrow(table, columns, table_filter)
    vectors_all, signature_vectors_all, drop_cols = _extract_scan_vector_matrices(
        arrow,
        with_vector=with_vector,
    )
    meta = arrow.drop(drop_cols).to_pylist() if drop_cols else arrow.to_pylist()
    kept_rows, kept_idx = _build_kept_scan_rows(meta, include_tests=include_tests)
    scan = WorkspaceScan(
        rows=kept_rows,
        vectors=_subset_scan_matrix(vectors_all, kept_idx),
        signature_vectors=_subset_scan_matrix(signature_vectors_all, kept_idx),
    )
    return _store_workspace_scan(cache_key, scan)


def _candidate_from_role_row(
    row: dict,
    *,
    role: str,
    evidence,
    distance: float | None,
    has_query: bool,
    impact_mode: bool,
) -> RoleCandidate | None:
    matched_contracts = sorted(row["_contracts"] & evidence.contracts)
    matched_kinds = sorted(row["_kinds"] & evidence.kinds)
    if not (matched_contracts or matched_kinds):
        return None
    total_contracts = len(evidence.contracts)
    total_kinds = len(evidence.kinds)
    structural = _structural_score(
        len(matched_contracts),
        len(matched_kinds),
        total_contracts,
        total_kinds,
    )
    semantic = _semantic_score(distance)
    query_similarity = _cosine_from_l2_distance(distance)
    tier_w = _tier_weight(row.get("file_tier"), impact_mode=impact_mode)
    return RoleCandidate(
        uid=str(row.get("uid") or ""),
        name=str(row.get("name") or ""),
        qualified_name=str(row.get("qualified_name") or ""),
        file_path=str(row.get("file_path") or ""),
        role=role,
        satisfying_contracts=tuple(matched_contracts),
        satisfying_kinds=tuple(matched_kinds),
        contract_count=len(matched_contracts),
        kind_count=len(matched_kinds),
        vector_distance=(float(distance) if distance is not None else None),
        score=_combined_score(structural, semantic, has_query) * tier_w,
        query_similarity=query_similarity,
        graph_score=structural * tier_w,
    )


def _candidates_for_role(
    scan: WorkspaceScan,
    role: str,
    *,
    limit: int,
    distances,
    has_query: bool,
    impact_mode: bool,
) -> list[RoleCandidate]:
    evidence = ROLE_EVIDENCE_MAP.get(role)
    if evidence is None or (not evidence.contracts and not evidence.kinds):
        return []
    rows = scan.rows
    candidates: list[RoleCandidate] = []
    for idx in _row_indices_for_evidence(scan, evidence):
        row = rows[idx]
        distance = None if distances is None else distances[row.get("_idx")]
        candidate = _candidate_from_role_row(
            row,
            role=role,
            evidence=evidence,
            distance=distance,
            has_query=has_query,
            impact_mode=impact_mode,
        )
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda c: (c.score, c.uid), reverse=True)
    return candidates[:limit]


def find_symbols_by_roles(
    workspace_id: str,
    roles: list[str],
    *,
    query_text: str | None = None,
    limit: int = 25,
    lance_db_path: str = DB_PATH,
    embed_fn=None,
    include_tests: bool = False,
    prescanned: WorkspaceScan | None = None,
    query_scoring: QueryScoringContext | None = None,
) -> dict[str, list[RoleCandidate]]:
    """Batch role retrieval off a single scan.

    Distributes the pre-scanned, pre-parsed workspace rows to each role
    by set intersection, embeds the query once, and caches per-uid
    distances across roles. Equivalent to calling ``find_symbols_by_role``
    per role but with one scan + one embed instead of N of each.
    """
    scan = (
        prescanned
        if prescanned is not None
        else scan_workspace_rows(
            workspace_id,
            lance_db_path=lance_db_path,
            include_tests=include_tests,
        )
    )
    has_query = bool(query_text and embed_fn is not None)
    distances = (
        query_scoring.distances
        if has_query and query_scoring is not None
        else (_scan_distances(scan, query_text, embed_fn) if has_query else None)
    )
    impact_mode = bool(_MODE_ROLES & set(roles))
    return {
        role: _candidates_for_role(
            scan,
            role,
            limit=limit,
            distances=distances,
            has_query=has_query,
            impact_mode=impact_mode,
        )
        for role in roles
    }


def find_symbols_by_role(
    workspace_id: str,
    role: str,
    *,
    query_text: str | None = None,
    limit: int = 25,
    lance_db_path: str = DB_PATH,
    embed_fn=None,
    include_tests: bool = False,
) -> list[RoleCandidate]:
    """Return symbols satisfying ``role`` in ``workspace_id``, ranked.

    Pipeline:

      1. Structural filter — scan the workspace's Lance symbol rows,
         keep only those whose persisted ``axis_contracts_json`` contains
         ≥1 contract from the role's contract set.
      2. (Optional) vector rerank — when ``query_text`` + ``embed_fn``
         are supplied, compute the L2 distance between the query
         embedding and each candidate's stored vector, and fold the
         normalised distance into the score.

    Structural narrowing comes FIRST so vector top-N doesn't drown out
    role-satisfying-but-rare symbols. The trade-off is one Lance table
    scan per call — acceptable for workspaces in the thousands; if it
    becomes a bottleneck, a dedicated ``axis_roles`` Lance column will
    let the filter run as a Lance prefilter instead.
    """
    # Thin wrapper over the batch path — one role, one scan (now with
    # workspace predicate pushdown). Kept for the many existing callers
    # and tests that retrieve a single role.
    return find_symbols_by_roles(
        workspace_id,
        [role],
        query_text=query_text,
        limit=limit,
        lance_db_path=lance_db_path,
        embed_fn=embed_fn,
        include_tests=include_tests,
    ).get(role, [])


def find_seeds_by_vector(
    workspace_id: str,
    query_text: str,
    *,
    embed_fn,
    limit: int = 12,
    min_similarity: float = 0.0,
    max_seeds: int | None = None,
    lance_db_path: str = DB_PATH,
    include_tests: bool = False,
    impact_mode: bool = False,
    prescanned: WorkspaceScan | None = None,
    query_scoring: QueryScoringContext | None = None,
) -> list[RoleCandidate]:
    """Role-AGNOSTIC vector seed retrieval — top-``limit`` symbols by
    embedding similarity, with NO role/kind filter.

    This is the seed source that keeps the intent classifier out of
    structure selection. ``find_symbols_by_role`` gates Lance rows by a
    role's evidence kinds/contracts; when intent picks the wrong role
    (django ``proxy_mechanism`` for a QuerySet, click ``proxy_mechanism``
    for ``Context.parse_args``) the gate discards the right nodes. Pure
    similarity does not gate — it finds the structurally-nearest symbols
    regardless of role, and the reactive traversal + intent ranking take
    it from there.

    Top-``limit`` is selected by ``argpartition`` over the workspace's
    vector matrix — no per-row Python distance loop. Returns candidates
    tagged ``role="vector_seed"``.

    ``min_similarity`` > 0 switches to an ADAPTIVE qsim gate: instead of a
    flat top-``limit``, every row whose cosine (``1 - d²/2`` for unit
    vectors) clears the threshold is seeded, ordered by tier-adjusted
    distance. The top-``limit`` prefix is always kept as a floor (the gate
    never returns fewer seeds than the fixed channel would), and
    ``max_seeds`` caps the total as a safety ceiling. This gives more seeds
    to queries whose answer symbols are semantically retrievable and fewer
    to queries where the gold is absent from the embedding neighbourhood —
    unlike a fixed K, which over-seeds easy queries and under-seeds hard
    ones. The gate is opt-in; ``min_similarity`` == 0 preserves the exact
    top-``limit`` behaviour.
    """
    if not query_text or embed_fn is None:
        return []
    scan = (
        prescanned
        if prescanned is not None
        else scan_workspace_rows(
            workspace_id,
            lance_db_path=lance_db_path,
            include_tests=include_tests,
        )
    )
    if not scan.rows or scan.vectors is None:
        return []
    distances = (
        query_scoring.distances
        if query_scoring is not None
        else _scan_distances(scan, query_text, embed_fn)
    )
    if distances is None:
        return []

    import numpy as np

    n = len(scan.rows)
    k = min(limit, n)
    # Structural file-tier penalty BEFORE top-k selection: an example app
    # or stub must not consume a seed slot a core file should hold. Inflate
    # the effective distance by 1/tier_weight so demoted tiers fall back in
    # the ranking; the true ``vector_distance`` is preserved for downstream.
    weights = np.array(
        [
            _tier_weight(
                str(scan.rows[i].get("file_tier") or "core"),
                impact_mode=impact_mode,
            )
            for i in range(n)
        ],
        dtype=float,
    )
    weights = np.where(weights <= 0.0, 1e-6, weights)
    adjusted = distances / weights
    if min_similarity > 0.0:
        # ADAPTIVE qsim gate: keep every row clearing the cosine threshold,
        # ordered by tier-adjusted distance, with the top-`k` prefix pinned as
        # a floor so the gate is never sparser than the fixed channel. Full
        # argsort is O(n log n) but runs only on this opt-in path.
        cosines = 1.0 - np.square(distances) / 2.0
        order = np.argsort(adjusted)
        keep = cosines[order] >= min_similarity
        keep[:k] = True  # floor: always retain the top-k by adjusted distance
        nearest = order[keep]
        if max_seeds is not None and max_seeds > 0:
            nearest = nearest[:max_seeds]
    else:
        # argpartition for the k nearest by ADJUSTED distance, then sort those k.
        nearest = np.argpartition(adjusted, k - 1)[:k] if k < n else np.arange(n)
        nearest = nearest[np.argsort(adjusted[nearest])]

    out: list[RoleCandidate] = []
    for idx in nearest:
        row = scan.rows[int(idx)]
        distance = float(distances[int(idx)])
        tier_w = float(weights[int(idx)])
        out.append(
            RoleCandidate(
                uid=str(row.get("uid") or ""),
                name=str(row.get("name") or ""),
                qualified_name=str(row.get("qualified_name") or ""),
                file_path=str(row.get("file_path") or ""),
                role="vector_seed",
                satisfying_contracts=(),
                satisfying_kinds=(),
                contract_count=0,
                kind_count=0,
                vector_distance=distance,
                score=_semantic_score(distance) * tier_w,
                query_similarity=(
                    query_scoring.similarity_for(str(row.get("uid") or ""))
                    if query_scoring is not None
                    else _cosine_from_l2_distance(distance)
                ),
                graph_score=0.0,
                retrieval_channels=("vector",),
            )
        )
    return out


def _workspace_lexical_index(scan: WorkspaceScan):
    if scan.lexical_index is None:
        from context_engine.search.lexical import FieldedBM25Index

        scan.lexical_index = FieldedBM25Index(scan.rows)
    return scan.lexical_index


def find_seeds_by_lexical(
    workspace_id: str,
    query_text: str,
    *,
    limit: int = 16,
    lance_db_path: str = DB_PATH,
    include_tests: bool = False,
    impact_mode: bool = False,
    prescanned: WorkspaceScan | None = None,
) -> list[RoleCandidate]:
    """Fielded BM25 + exact identifier retrieval over cached symbol metadata."""
    if not query_text or limit <= 0:
        return []
    scan = (
        prescanned
        if prescanned is not None
        else scan_workspace_rows(
            workspace_id,
            lance_db_path=lance_db_path,
            include_tests=include_tests,
        )
    )
    if not scan.rows:
        return []
    hits = _workspace_lexical_index(scan).search(query_text, limit=limit)
    if not hits:
        return []
    ceiling = max(float(hit.score) for hit in hits) or 1.0
    output: list[RoleCandidate] = []
    for hit in hits:
        row = scan.rows[hit.row_index]
        tier_w = _tier_weight(str(row.get("file_tier") or "core"), impact_mode=impact_mode)
        output.append(
            RoleCandidate(
                uid=str(row.get("uid") or ""),
                name=str(row.get("name") or ""),
                qualified_name=str(row.get("qualified_name") or ""),
                file_path=str(row.get("file_path") or ""),
                role="lexical_seed",
                satisfying_contracts=(),
                satisfying_kinds=(),
                contract_count=0,
                kind_count=0,
                vector_distance=None,
                score=min(1.0, float(hit.score) / ceiling) * tier_w,
                graph_score=0.0,
                retrieval_channels=("lexical",),
                exact_symbol_match=bool(hit.exact),
            )
        )
    return output


def find_seeds_by_semantic_chunk(
    workspace_id: str,
    query_text: str,
    *,
    embed_fn,
    limit: int = 12,
    chunk_oversample: int = 4,
    include_tests: bool = False,
    impact_mode: bool = False,
    prescanned: WorkspaceScan | None = None,
    query_scoring: QueryScoringContext | None = None,
    lance=None,
) -> list[RoleCandidate]:
    """Vector-search AST chunks, aggregate by owner symbol, retain spans."""
    if not query_text or embed_fn is None or limit <= 0 or lance is None:
        return []
    search_chunks = getattr(lance, "search_symbol_chunks_by_vector", None)
    if not callable(search_chunks):
        return []
    scan = prescanned or scan_workspace_rows(
        workspace_id,
        include_tests=include_tests,
    )
    try:
        query_vector = (
            query_scoring.query_vector if query_scoring is not None else embed_fn(query_text)
        )
        rows = search_chunks(
            query_vector,
            workspace_id=workspace_id,
            limit=max(limit, limit * max(1, chunk_oversample)),
        )
    except Exception:
        return []
    by_owner: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        owner_uid = str(row.get("owner_uid") or "")
        owner = scan.rows_by_uid.get(owner_uid)
        if not owner:
            continue
        distance = float(row.get("distance", 1.0))
        state = by_owner.setdefault(
            owner_uid,
            {"owner": owner, "distance": distance, "spans": set()},
        )
        state["distance"] = min(float(state["distance"]), distance)
        start_line = int(row.get("start_line") or 0)
        end_line = int(row.get("end_line") or 0)
        if start_line > 0 and end_line >= start_line:
            state["spans"].add((start_line, end_line))

    ranked = sorted(
        by_owner.items(),
        key=lambda item: (float(item[1]["distance"]), item[0]),
    )[:limit]
    output: list[RoleCandidate] = []
    for owner_uid, state in ranked:
        owner = state["owner"]
        distance = float(state["distance"])
        tier_w = _tier_weight(
            str(owner.get("file_tier") or "core"),
            impact_mode=impact_mode,
        )
        output.append(
            RoleCandidate(
                uid=owner_uid,
                name=str(owner.get("name") or ""),
                qualified_name=str(owner.get("qualified_name") or ""),
                file_path=str(owner.get("file_path") or ""),
                role="semantic_chunk_seed",
                satisfying_contracts=(),
                satisfying_kinds=(),
                contract_count=0,
                kind_count=0,
                vector_distance=distance,
                score=_semantic_score(distance) * tier_w,
                query_similarity=_cosine_from_l2_distance(distance),
                graph_score=0.0,
                retrieval_channels=("semantic_chunk",),
                retrieval_spans=tuple(sorted(state["spans"])),
            )
        )
    return output


def _merge_retrieval_spans(candidates: list[RoleCandidate]) -> tuple[tuple[int, int], ...]:
    spans = {
        (int(start), int(end))
        for candidate in candidates
        for start, end in candidate.retrieval_spans
        if int(start) > 0 and int(end) >= int(start)
    }
    return tuple(sorted(spans))


def fuse_seed_channels(
    channels: dict[str, list[RoleCandidate]],
    *,
    limit: int = 12,
    rrf_k: int = 60,
    weights: dict[str, float] | None = None,
) -> list[RoleCandidate]:
    """Weighted reciprocal-rank fusion, deduplicated by owner symbol uid."""
    if limit <= 0:
        return []
    channel_weights = {"vector": 1.0, "lexical": 1.15, "semantic_chunk": 1.0}
    if weights:
        channel_weights.update(weights)
    scores: dict[str, float] = defaultdict(float)
    evidence: dict[str, list[RoleCandidate]] = defaultdict(list)
    for channel, candidates in channels.items():
        weight = max(0.0, float(channel_weights.get(channel, 1.0)))
        for rank, candidate in enumerate(candidates, start=1):
            if not candidate.uid:
                continue
            scores[candidate.uid] += weight / (max(0, int(rrf_k)) + rank)
            evidence[candidate.uid].append(candidate)
    if not scores:
        return []
    ceiling = (
        sum(
            max(0.0, float(channel_weights.get(channel, 1.0))) / (max(0, int(rrf_k)) + 1)
            for channel, candidates in channels.items()
            if candidates
        )
        or 1.0
    )
    ranked_uids = sorted(scores, key=lambda uid: (scores[uid], uid), reverse=True)[:limit]
    output: list[RoleCandidate] = []
    for uid in ranked_uids:
        sources = evidence[uid]
        exemplar = max(
            sources,
            key=lambda candidate: (
                candidate.exact_symbol_match,
                bool(candidate.retrieval_spans),
                candidate.score,
            ),
        )
        similarities = [
            float(candidate.query_similarity)
            for candidate in sources
            if candidate.query_similarity is not None
        ]
        distances = [
            float(candidate.vector_distance)
            for candidate in sources
            if candidate.vector_distance is not None
        ]
        source_channels = tuple(
            channel
            for channel, candidates in channels.items()
            if any(c.uid == uid for c in candidates)
        )
        output.append(
            replace(
                exemplar,
                role="hybrid_seed",
                score=min(1.0, scores[uid] / ceiling),
                vector_distance=min(distances) if distances else None,
                query_similarity=max(similarities) if similarities else None,
                graph_score=0.0,
                retrieval_channels=source_channels,
                retrieval_spans=_merge_retrieval_spans(sources),
                exact_symbol_match=any(candidate.exact_symbol_match for candidate in sources),
            )
        )
    return output


def _l2_distance(a, b) -> float:
    """Plain L2 distance between two flat float sequences."""
    import math

    if a is None or b is None:
        return float("inf")
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b, strict=False)))


def _vectorised_distances(vectors, query_text, embed_fn):
    """L2 distance from the query embedding to every row of the
    ``(N, dim)`` matrix in one numpy pass. Returns a length-N float
    array (row i = distance to ``vectors[i]``) or ``None`` when there is
    no query / no matrix."""
    if vectors is None or not query_text or embed_fn is None:
        return None
    try:
        import numpy as np

        qv = embed_fn(query_text)
        if hasattr(qv, "tolist"):
            qv = qv.tolist()
        qv = np.asarray(qv, dtype=vectors.dtype)
        return np.linalg.norm(vectors - qv, axis=1)
    except Exception:
        return None


def _scan_scores_from_vector(scan: WorkspaceScan, query_vector):
    """Return dual-facet ``(distance, cosine)`` arrays for one query vector.

    Indexed SentenceTransformer vectors are L2-normalised, so one fast
    matrix-vector product produces cosine directly; L2 follows as
    ``sqrt(2 - 2*cos)``.  Non-normalised query vectors (legacy/test embedders)
    use the general L2 fallback.
    """
    if scan.vectors is None or query_vector is None:
        return None
    try:
        import numpy as np

        qv = query_vector.tolist() if hasattr(query_vector, "tolist") else query_vector
        qv = np.asarray(qv, dtype=scan.vectors.dtype)
        query_norm = float(np.linalg.norm(qv))

        def _looks_normalised(matrix) -> bool:
            sample_step = max(1, int(matrix.shape[0]) // 64)
            sample = matrix[::sample_step][:64]
            return bool(np.all(np.abs(np.linalg.norm(sample, axis=1) - 1.0) <= 1e-3))

        sig = scan.signature_vectors
        normalised_facets = _looks_normalised(scan.vectors) and (
            sig is None or _looks_normalised(sig)
        )
        if abs(query_norm - 1.0) <= 1e-3 and normalised_facets:
            similarities = scan.vectors @ qv
            if sig is not None and getattr(sig, "shape", (0,))[0] == similarities.shape[0]:
                similarities = np.maximum(similarities, sig @ qv)
            similarities = np.clip(similarities, -1.0, 1.0)
            distances = np.sqrt(np.maximum(0.0, 2.0 - 2.0 * similarities))
            return distances, similarities

        body_distances = np.linalg.norm(scan.vectors - qv, axis=1)
        distances = body_distances
        if sig is not None and getattr(sig, "shape", (0,))[0] == body_distances.shape[0]:
            sig_distances = np.linalg.norm(sig - qv, axis=1)
            distances = np.minimum(body_distances, sig_distances)
        if query_norm <= 1e-12:
            similarities = np.zeros_like(distances, dtype=float)
        else:
            body_norms = np.linalg.norm(scan.vectors, axis=1) * query_norm
            similarities = np.divide(
                scan.vectors @ qv,
                body_norms,
                out=np.zeros_like(distances, dtype=float),
                where=body_norms > 1e-12,
            )
            if sig is not None and getattr(sig, "shape", (0,))[0] == distances.shape[0]:
                sig_norms = np.linalg.norm(sig, axis=1) * query_norm
                sig_similarities = np.divide(
                    sig @ qv,
                    sig_norms,
                    out=np.zeros_like(distances, dtype=float),
                    where=sig_norms > 1e-12,
                )
                similarities = np.maximum(similarities, sig_similarities)
        return distances, np.clip(similarities, -1.0, 1.0)
    except Exception:
        return None


def build_query_scoring_context(
    scan: WorkspaceScan,
    query_text: str,
    embed_fn,
) -> QueryScoringContext | None:
    """Embed the question once and score the workspace matrices once."""
    if scan.vectors is None or not query_text or embed_fn is None:
        return None
    try:
        query_vector = embed_fn(query_text)
    except Exception:
        return None
    scores = _scan_scores_from_vector(scan, query_vector)
    if scores is None:
        return None
    distances, similarities = scores
    return QueryScoringContext(
        scan=scan,
        query_vector=query_vector,
        distances=distances,
        similarities=similarities,
    )


def _scan_distances(scan: WorkspaceScan, query_text, embed_fn):
    """Dual-facet L2 distance: the element-wise MINIMUM of the body-vector
    distance and the signature-vector distance, embedding the query once.

    A symbol matches if EITHER its body or its signature is close to the
    query — so a signature/API-shaped question reaches a symbol whose large
    body diluted the body vector, without weakening behavioural matches.
    Falls back to the body distance when the signature facet is absent
    (pre-facet index)."""
    context = build_query_scoring_context(scan, query_text, embed_fn)
    return None if context is None else context.distances


def _doc_anchor_lance_client(lance):
    if lance is not None:
        return lance
    try:
        from context_engine.database.lancedb_client import LanceDBClient
        from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE

        return LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)
    except Exception:
        return None


def _doc_anchor_query_vector(query_text, embed_fn):
    import numpy as np

    try:
        qv = embed_fn(query_text)
        if hasattr(qv, "tolist"):
            qv = qv.tolist()
        return np.asarray(qv, dtype=np.float32)
    except Exception:
        return None


def _search_doc_anchor_rows(lance, qv_arr, workspace_id: str, limit: int) -> list[dict]:
    search_docs = getattr(lance, "search_doc_anchors", None)
    if not callable(search_docs):
        return []
    try:
        return cast("list[dict]", search_docs(qv_arr, workspace_id=workspace_id, limit=limit))
    except Exception:
        return []


def _scan_doc_anchor_rows(lance, qv_arr, workspace_id: str, limit: int) -> list[dict]:
    import numpy as np

    scan_docs = getattr(lance, "scan_doc_anchors_workspace", None)
    if not callable(scan_docs):
        return []
    try:
        all_rows = scan_docs(workspace_id)
    except Exception:
        return []
    if not all_rows:
        return []
    vectors: list = []
    kept: list[dict] = []
    for row in all_rows:
        vector = row.get("vector")
        owner_uid = str(row.get("owner_uid") or "").strip()
        if not owner_uid or vector is None:
            continue
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        if not vector:
            continue
        kept.append(row)
        vectors.append(vector)
    if not kept:
        return []
    matrix = np.asarray(vectors, dtype=np.float32)
    distances = np.linalg.norm(matrix - qv_arr, axis=1)
    n = len(kept)
    k = min(limit, n)
    nearest = np.argpartition(distances, k - 1)[:k] if k < n else np.arange(n)
    nearest = nearest[np.argsort(distances[nearest])]
    rows: list[dict] = []
    for idx in nearest:
        row = dict(kept[int(idx)])
        row["_distance"] = float(distances[int(idx)])
        rows.append(row)
    return rows


def _doc_anchor_row_distance(row: dict, qv_arr) -> float:
    import numpy as np

    distance = row.get("_distance")
    if distance is not None:
        return float(distance)
    vector = row.get("vector")
    if vector is None:
        return float("inf")
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    return float(np.linalg.norm(np.asarray(vector, dtype=np.float32) - qv_arr))


def _score_doc_anchor_rows(
    rows: list[dict],
    scan: WorkspaceScan,
    qv_arr,
    *,
    impact_mode: bool,
) -> list[tuple[float, float, dict]]:
    scored: list[tuple[float, float, dict]] = []
    for row in rows:
        owner_uid = str(row.get("owner_uid") or "")
        owner = scan.rows_by_uid.get(owner_uid) or {}
        distance = _doc_anchor_row_distance(row, qv_arr)
        tier_w = _tier_weight(
            str(owner.get("file_tier") or "core"),
            impact_mode=impact_mode,
        )
        adjusted = distance / max(tier_w, 1e-6)
        scored.append((adjusted, distance, row))
    scored.sort(key=lambda item: item[0])
    return scored


def _doc_anchor_candidate(
    row: dict,
    scan: WorkspaceScan,
    distance: float,
    *,
    impact_mode: bool,
) -> RoleCandidate:
    owner_uid = str(row.get("owner_uid") or "")
    owner = scan.rows_by_uid.get(owner_uid) or {}
    tier_w = _tier_weight(
        str(owner.get("file_tier") or "core"),
        impact_mode=impact_mode,
    )
    return RoleCandidate(
        uid=owner_uid,
        name=str(owner.get("name") or ""),
        qualified_name=str(owner.get("qualified_name") or ""),
        file_path=str(row.get("file_path") or owner.get("file_path") or ""),
        role="doc_anchor",
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=distance,
        score=_semantic_score(distance) * tier_w,
        query_similarity=_cosine_from_l2_distance(distance),
        graph_score=0.0,
    )


def find_seeds_by_doc_anchor(
    workspace_id: str,
    query_text: str,
    *,
    embed_fn,
    limit: int = 12,
    lance_db_path: str = DB_PATH,
    include_tests: bool = False,
    impact_mode: bool = False,
    prescanned: WorkspaceScan | None = None,
    lance=None,
    query_scoring: QueryScoringContext | None = None,
) -> list[RoleCandidate]:
    """Vector-search in-code docstring anchors → owner symbol seeds."""
    if not query_text or embed_fn is None:
        return []
    lance = _doc_anchor_lance_client(lance)
    if lance is None:
        return []
    scan_docs = getattr(lance, "scan_doc_anchors_workspace", None)
    search_docs = getattr(lance, "search_doc_anchors", None)
    if not callable(scan_docs) and not callable(search_docs):
        return []

    scan = (
        prescanned
        if prescanned is not None
        else scan_workspace_rows(
            workspace_id,
            lance_db_path=lance_db_path,
            include_tests=include_tests,
        )
    )

    qv_arr = _doc_anchor_query_vector(
        query_text,
        (lambda _text: query_scoring.query_vector) if query_scoring is not None else embed_fn,
    )
    if qv_arr is None:
        return []

    rows = _search_doc_anchor_rows(lance, qv_arr, workspace_id, limit)
    if not rows:
        rows = _scan_doc_anchor_rows(lance, qv_arr, workspace_id, limit)
    if not rows:
        return []

    scored = _score_doc_anchor_rows(rows, scan, qv_arr, impact_mode=impact_mode)
    k = min(limit, len(scored))
    return [
        _doc_anchor_candidate(row, scan, distance, impact_mode=impact_mode)
        for _adjusted, distance, row in scored[:k]
    ]


__all__ = [
    "QueryScoringContext",
    "RoleCandidate",
    "WorkspaceScan",
    "build_query_scoring_context",
    "find_seeds_by_lexical",
    "find_seeds_by_semantic_chunk",
    "find_seeds_by_doc_anchor",
    "find_seeds_by_vector",
    "find_symbols_by_role",
    "find_symbols_by_roles",
    "invalidate_workspace_scan_cache",
    "fuse_seed_channels",
    "scan_workspace_rows",
]
