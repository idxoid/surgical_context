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

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import lancedb

from context_engine.axis.role_resolver import ROLE_EVIDENCE_MAP

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
    """Map L2 distance to ``[0, 1]`` (1 = exact match, 0 = far).
    Identity ordering: small distance → high score.
    """
    if distance is None:
        return 0.0
    if distance <= 0:
        return 1.0
    return max(0.0, 1.0 / (1.0 + float(distance)))


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

    def __post_init__(self) -> None:
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


def scan_workspace_rows(
    workspace_id: str,
    *,
    lance_db_path: str = "./data/lancedb",
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
    if _SCAN_CACHE_ENABLED:
        cached = _SCAN_CACHE.get(cache_key)
        if cached is not None:
            return cached

    from context_engine.database.lance_workspace_tables import (
        workspace_partition_table_exists,
        workspace_partition_table_name,
        workspace_partitioned_enabled,
    )
    from context_engine.index_profile import active_index_profile

    profile = active_index_profile()
    partitioned = workspace_partitioned_enabled()
    table_filter: str | None = None

    if lance is not None and hasattr(lance, "symbols_table"):
        table = lance.symbols_table(workspace_id)
        partitioned = partitioned and table is not getattr(lance, "_sym_table", None)
    else:
        db = lancedb.connect(lance_db_path)
        base_table = profile.symbols_table
        if partitioned:
            part_name = workspace_partition_table_name(base_table, workspace_id)
            if workspace_partition_table_exists(db, base_table, workspace_id):
                table = db.open_table(part_name)
            else:
                table = db.open_table(base_table)
                ws_quoted = workspace_id.replace("'", "''")
                table_filter = f"workspace_id = '{ws_quoted}'"
        else:
            table = db.open_table(base_table)
            ws_quoted = workspace_id.replace("'", "''")
            table_filter = f"workspace_id = '{ws_quoted}'"
    columns = [
        "uid",
        "name",
        "file_path",
        "axis_contracts_json",
        "axis_container_kinds_json",
        "workspace_id",
    ]
    try:
        _have_qualified_name = "qualified_name" in set(table.schema.names)
    except Exception:
        _have_qualified_name = False
    if _have_qualified_name:
        columns.append("qualified_name")
    # ``file_tier`` is materialised at index time (schema v5+). Request it
    # only when the table actually carries the column, so the scan still
    # works against an index written before the tier landed (pre-reindex);
    # absent → the ranker reads the default ``core``.
    try:
        _have_tier = "file_tier" in set(table.schema.names)
    except Exception:
        _have_tier = False
    if _have_tier:
        columns.append("file_tier")
    # ``signature_vector`` is the optional signature facet (schema-gated like
    # ``file_tier``). Present → dual-facet retrieval; absent → body-only.
    try:
        _have_sig_vec = "signature_vector" in set(table.schema.names)
    except Exception:
        _have_sig_vec = False
    if with_vector:
        columns.append("vector")
        if _have_sig_vec:
            columns.append("signature_vector")
    lance_reader = table.to_lance()
    if table_filter:
        arrow = lance_reader.to_table(columns=columns, filter=table_filter)
    else:
        arrow = lance_reader.to_table(columns=columns)
    from context_engine.axis.test_file_filter import is_test_path

    def _matrix(col_name: str):
        if col_name not in arrow.column_names or not arrow.num_rows:
            return None
        try:
            import numpy as np

            col = arrow.column(col_name).combine_chunks()
            return np.asarray(col.values.to_numpy(zero_copy_only=False)).reshape(arrow.num_rows, -1)
        except Exception:
            return None

    # Extract the vector columns as numpy matrices without round-tripping
    # them through Python objects; metadata to_pylist drops them.
    vectors_all = None
    signature_vectors_all = None
    drop_cols: list[str] = []
    if with_vector and "vector" in arrow.column_names and arrow.num_rows:
        vectors_all = _matrix("vector")
        drop_cols.append("vector")
        if _have_sig_vec and "signature_vector" in arrow.column_names:
            signature_vectors_all = _matrix("signature_vector")
            drop_cols.append("signature_vector")
    meta = arrow.drop(drop_cols).to_pylist() if drop_cols else arrow.to_pylist()

    kept_rows: list[dict] = []
    kept_idx: list[int] = []
    for i, r in enumerate(meta):
        if not include_tests and is_test_path(str(r.get("file_path") or "")):
            continue
        try:
            contract_objs = json.loads(r.get("axis_contracts_json") or "[]")
        except json.JSONDecodeError:
            contract_objs = []
        try:
            kind_objs = json.loads(r.get("axis_container_kinds_json") or "[]")
        except json.JSONDecodeError:
            kind_objs = []
        r["_contracts"] = {str(c.get("contract") or "") for c in contract_objs}
        r["_kinds"] = {str(k.get("kind") or "") for k in kind_objs}
        r["_idx"] = len(kept_rows)
        kept_rows.append(r)
        kept_idx.append(i)

    kept_vectors = None
    if vectors_all is not None and kept_idx:
        kept_vectors = vectors_all[kept_idx]
    kept_sig_vectors = None
    if signature_vectors_all is not None and kept_idx:
        kept_sig_vectors = signature_vectors_all[kept_idx]
    scan = WorkspaceScan(rows=kept_rows, vectors=kept_vectors, signature_vectors=kept_sig_vectors)
    if _SCAN_CACHE_ENABLED:
        _SCAN_CACHE[cache_key] = scan
    return scan


def find_symbols_by_roles(
    workspace_id: str,
    roles: list[str],
    *,
    query_text: str | None = None,
    limit: int = 25,
    lance_db_path: str = "./data/lancedb",
    embed_fn=None,
    include_tests: bool = False,
    prescanned: WorkspaceScan | None = None,
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
    rows = scan.rows
    has_query = bool(query_text and embed_fn is not None)
    # Vectorised distance: one numpy pass over the whole matrix, indexed
    # by each row's ``_idx`` — no per-row Python distance loop. Dual-facet:
    # min(body, signature) so a signature-shaped query reaches body-diluted
    # symbols.
    distances = _scan_distances(scan, query_text, embed_fn) if has_query else None

    def _distance(row: dict) -> float | None:
        if distances is None:
            return None
        idx = row.get("_idx")
        return None if idx is None else distances[idx]

    impact_mode = bool(_MODE_ROLES & set(roles))
    out: dict[str, list[RoleCandidate]] = {}
    for role in roles:
        evidence = ROLE_EVIDENCE_MAP.get(role)
        if evidence is None or (not evidence.contracts and not evidence.kinds):
            out[role] = []
            continue
        total_contracts = len(evidence.contracts)
        total_kinds = len(evidence.kinds)
        candidates: list[RoleCandidate] = []
        for idx in _row_indices_for_evidence(scan, evidence):
            row = rows[idx]
            matched_contracts = sorted(row["_contracts"] & evidence.contracts)
            matched_kinds = sorted(row["_kinds"] & evidence.kinds)
            if not (matched_contracts or matched_kinds):
                continue
            distance = _distance(row)
            structural = _structural_score(
                len(matched_contracts),
                len(matched_kinds),
                total_contracts,
                total_kinds,
            )
            semantic = _semantic_score(distance)
            tier_w = _tier_weight(row.get("file_tier"), impact_mode=impact_mode)
            candidates.append(
                RoleCandidate(
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
                )
            )
        # uid breaks score ties so the per-role cap is reproducible — without
        # it, equal-score candidates keep their Lance/dict input order, which is
        # PYTHONHASHSEED-randomized per process and flips which survive ``[:limit]``.
        candidates.sort(key=lambda c: (c.score, c.uid), reverse=True)
        out[role] = candidates[:limit]
    return out


def find_symbols_by_role(
    workspace_id: str,
    role: str,
    *,
    query_text: str | None = None,
    limit: int = 25,
    lance_db_path: str = "./data/lancedb",
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
    lance_db_path: str = "./data/lancedb",
    include_tests: bool = False,
    impact_mode: bool = False,
    prescanned: WorkspaceScan | None = None,
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
    distances = _scan_distances(scan, query_text, embed_fn)
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
            )
        )
    return out


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


def _scan_distances(scan: WorkspaceScan, query_text, embed_fn):
    """Dual-facet L2 distance: the element-wise MINIMUM of the body-vector
    distance and the signature-vector distance, embedding the query once.

    A symbol matches if EITHER its body or its signature is close to the
    query — so a signature/API-shaped question reaches a symbol whose large
    body diluted the body vector, without weakening behavioural matches.
    Falls back to the body distance when the signature facet is absent
    (pre-facet index)."""
    if scan.vectors is None or not query_text or embed_fn is None:
        return None
    try:
        import numpy as np

        qv = embed_fn(query_text)
        if hasattr(qv, "tolist"):
            qv = qv.tolist()
        qv = np.asarray(qv, dtype=scan.vectors.dtype)
        body = np.linalg.norm(scan.vectors - qv, axis=1)
        sig = scan.signature_vectors
        if sig is not None and getattr(sig, "shape", (0,))[0] == body.shape[0]:
            return np.minimum(body, np.linalg.norm(sig - qv, axis=1))
        return body
    except Exception:
        return None


__all__ = [
    "RoleCandidate",
    "WorkspaceScan",
    "find_seeds_by_vector",
    "find_symbols_by_role",
    "find_symbols_by_roles",
    "invalidate_workspace_scan_cache",
    "scan_workspace_rows",
]
