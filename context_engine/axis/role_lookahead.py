"""Cross-role lookahead — graph-evidenced candidate expansion.

The intent classifier reads the question's *phrasing*; the L4
retrieval reads per-role *vector* matches. Both miss the same case:
when one role's candidates sit *inside* the structural region of
another role, the second role is structurally evidenced even when its
vector retrieval is shallow or its intent similarity sat below the
top-k cutoff.

Worked example. The Flask question "How does ``current_app`` find the
right application?" makes the intent classifier shout *proxy* — and
``proxy_object`` candidates (``current_app``, ``request``, ``g``,
``session``) live in ``globals.py``. The actual mechanism that resolves
those proxies is in ``app.py``'s ``Flask.wsgi_app`` /
``Flask.dispatch_request``: methods that the structural classifier
already tagged ``keyed_dispatch_callable`` (backing
``dispatch_surface``). A K-hop walk from each ``proxy_object`` seed,
gathering neighbours whose container_kinds back any intent role,
exposes those dispatchers as structurally-evidenced candidates for the
``dispatch_surface`` slot — even when that slot's vector retrieval
returned nothing.

The pass is *injection-only*: it never drops or re-scores
vector-derived candidates. Graph-derived candidates carry a moderate
``base_score`` and the ``satisfying_kinds`` that earned them their
slot, so consumers can tell them apart and rank accordingly. The
``max_injected_per_role`` cap keeps the pool from blowing up when a
dense graph touches many neighbours.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from context_engine.axis.graph_walk import EdgeProfile, walk_neighbours
from context_engine.axis.kind_rows import flat_kinds
from context_engine.axis.role_resolver import ROLE_EVIDENCE_MAP
from context_engine.axis.role_retrieval import (
    RoleCandidate,
    _combined_score,
    _scan_distances,
    _semantic_score,
)


def _build_kind_to_roles(intent_roles: Iterable[str]) -> dict[str, set[str]]:
    """Reverse-index ``ROLE_EVIDENCE_MAP`` for *only* the roles in this
    intent ranking. The narrower index avoids attributing a neighbour to
    a role the user's question never gestured at."""
    out: dict[str, set[str]] = {}
    for role in intent_roles:
        ev = ROLE_EVIDENCE_MAP.get(role)
        if not ev:
            continue
        for kind in ev.kinds:
            out.setdefault(kind, set()).add(role)
    return out


def _quote_lance_sql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _kinds_from_prescanned(
    neighbour_uids: set[str],
    prescanned,
) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    out: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    rows_by_uid = getattr(prescanned, "rows_by_uid", {}) or {}
    for uid in neighbour_uids:
        row = rows_by_uid.get(uid)
        if not row:
            continue
        kinds = set(row.get("_kinds") or set())
        if not kinds:
            continue
        out[uid] = (
            str(row.get("name") or ""),
            str(row.get("file_path") or ""),
            tuple(sorted(kinds)),
        )
    return out


def _symbol_kinds_filter_sql(
    workspace_id: str,
    neighbour_uids: set[str],
    *,
    sym_table_fn,
) -> str:
    uid_filter = ", ".join(_quote_lance_sql(uid) for uid in sorted(neighbour_uids))
    from context_engine.database.lance_workspace_tables import workspace_partitioned_enabled

    if workspace_partitioned_enabled() and callable(sym_table_fn):
        return f"uid IN ({uid_filter})"
    return f"workspace_id = {_quote_lance_sql(workspace_id)} AND uid IN ({uid_filter})"


def _kinds_row_tuple(
    name: object,
    file_path: object,
    kinds_json: object,
) -> tuple[str, str, tuple[str, ...]] | None:
    kinds = flat_kinds(kinds_json)
    if not kinds:
        return None
    return (str(name or ""), str(file_path or ""), tuple(sorted(kinds)))


def _kinds_from_pylist_rows(
    table,
    *,
    workspace_id: str,
    neighbour_uids: set[str],
) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    out: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for row in table.to_pylist():
        if row.get("workspace_id") != workspace_id:
            continue
        uid = str(row.get("uid") or "")
        if uid not in neighbour_uids:
            continue
        parsed = _kinds_row_tuple(
            row.get("name"), row.get("file_path"), row.get("axis_container_kinds_json")
        )
        if parsed is not None:
            out[uid] = parsed
    return out


def _kinds_from_arrow_columns(
    table,
    *,
    workspace_id: str,
    neighbour_uids: set[str],
) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    out: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    uids = table["uid"].to_pylist()
    names = table["name"].to_pylist()
    file_paths = table["file_path"].to_pylist()
    kinds_jsons = table["axis_container_kinds_json"].to_pylist()
    workspace_ids = table["workspace_id"].to_pylist()
    for uid_raw, name, file_path, kinds_json, row_workspace_id in zip(
        uids, names, file_paths, kinds_jsons, workspace_ids, strict=False
    ):
        if row_workspace_id != workspace_id:
            continue
        uid = str(uid_raw or "")
        if uid not in neighbour_uids:
            continue
        parsed = _kinds_row_tuple(name, file_path, kinds_json)
        if parsed is not None:
            out[uid] = parsed
    return out


def _load_neighbour_kinds_table(
    lance_table,
    columns: list[str],
    filter_sql: str,
    *,
    workspace_id: str,
    neighbour_uids: set[str],
):
    try:
        return lance_table.to_table(columns=columns, filter=filter_sql)
    except TypeError:
        table = lance_table.to_table(columns=columns)
        try:
            import pyarrow as pa
            import pyarrow.compute as pc

            uid_set = pa.array(list(neighbour_uids), type=table["uid"].type)
            mask = pc.and_(
                pc.equal(
                    table["workspace_id"],
                    pa.scalar(workspace_id, type=table["workspace_id"].type),
                ),
                pc.is_in(table["uid"], value_set=uid_set),
            )
            return table.filter(mask)
        except Exception:
            return table


def _fetch_neighbour_kinds_from_lance(
    lance,
    workspace_id: str,
    neighbour_uids: set[str],
) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    sym_table_fn = getattr(lance, "symbols_table", None)
    sym_table = sym_table_fn(workspace_id) if callable(sym_table_fn) else lance._sym_table  # noqa: SLF001
    columns = [
        "uid",
        "name",
        "file_path",
        "axis_container_kinds_json",
        "workspace_id",
    ]
    filter_sql = _symbol_kinds_filter_sql(
        workspace_id,
        neighbour_uids,
        sym_table_fn=sym_table_fn,
    )
    table = _load_neighbour_kinds_table(
        sym_table.to_lance(),
        columns,
        filter_sql,
        workspace_id=workspace_id,
        neighbour_uids=neighbour_uids,
    )
    try:
        return _kinds_from_arrow_columns(
            table,
            workspace_id=workspace_id,
            neighbour_uids=neighbour_uids,
        )
    except Exception:
        return _kinds_from_pylist_rows(
            table,
            workspace_id=workspace_id,
            neighbour_uids=neighbour_uids,
        )


def _fetch_neighbour_kinds(
    lance,
    workspace_id: str,
    neighbour_uids: set[str],
    prescanned=None,
) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    """Read ``(name, file_path, container_kinds)`` for each requested
    neighbour. Prefer the shared workspace scan (``_kinds`` already
    parsed) over a fresh Lance materialisation.
    """
    if not neighbour_uids:
        return {}
    if prescanned is not None:
        return _kinds_from_prescanned(neighbour_uids, prescanned)
    return _fetch_neighbour_kinds_from_lance(lance, workspace_id, neighbour_uids)


def _lookahead_injection_candidate(
    *,
    uid: str,
    name: str,
    file_path: str,
    target_role: str,
    evidence_kinds: set[str],
    base_score: float,
    distance: float | None = None,
) -> RoleCandidate:
    # ``base_score`` is the structural-affinity constant; blending the real
    # query distance (free — the scan matrix is already in memory) keeps the
    # injections on the same combined scale as scan candidates instead of a
    # flat constant that lands mid-way through the semantically-scored range
    # and outranks genuinely close symbols by uid tiebreak.
    return RoleCandidate(
        uid=uid,
        name=name,
        file_path=file_path,
        role=target_role,
        satisfying_contracts=(),
        satisfying_kinds=tuple(sorted(evidence_kinds)),
        contract_count=0,
        kind_count=len(evidence_kinds),
        vector_distance=distance,
        score=_combined_score(base_score, _semantic_score(distance), distance is not None),
    )


def _matched_target_roles_for_neighbour(
    kinds: tuple[str, ...],
    *,
    source_role: str,
    kind_to_roles: dict[str, set[str]],
    intent_set: set[str],
    existing_uids_by_role: dict[str, set[str]],
    uid: str,
) -> dict[str, set[str]]:
    matched_targets: dict[str, set[str]] = {}
    for kind in kinds:
        for target_role in kind_to_roles.get(kind, ()):
            if target_role == source_role:
                continue
            if target_role in intent_set and uid in existing_uids_by_role.get(target_role, set()):
                continue
            matched_targets.setdefault(target_role, set()).add(kind)
    return matched_targets


def _record_neighbour_reach(
    neighbours,
    aggregated_neighbour_reach: dict[str, int],
) -> tuple[dict[str, int], set[str]]:
    neighbour_reach: dict[str, int] = {}
    for nb in neighbours:
        neighbour_reach[nb.uid] = nb.reach
        aggregated_neighbour_reach[nb.uid] = aggregated_neighbour_reach.get(nb.uid, 0) + nb.reach
    return neighbour_reach, set(neighbour_reach.keys())


def _inject_lookahead_candidates(
    out: dict[str, list[RoleCandidate]],
    per_target: dict[str, list[RoleCandidate]],
    *,
    neighbour_reach: dict[str, int],
    max_injected_per_role: int,
) -> None:
    for target_role, items in per_target.items():
        items_sorted = sorted(
            items,
            key=lambda c: (neighbour_reach.get(c.uid, 0), c.uid),
            reverse=True,
        )
        out[target_role].extend(items_sorted[:max_injected_per_role])


def _auto_promote_lookahead_roles(
    out: dict[str, list[RoleCandidate]],
    promotion_evidence: dict[str, dict[str, tuple[str, tuple[str, ...]]]],
    *,
    aggregated_neighbour_reach: dict[str, int],
    auto_promote_min_hits: int,
    max_injected_per_role: int,
    base_score: float,
    distance_for: Callable[[str], float | None] = lambda _uid: None,
) -> None:
    for target_role, uid_evidence in promotion_evidence.items():
        if len(uid_evidence) < auto_promote_min_hits:
            continue
        ranked_uids = sorted(
            uid_evidence.keys(),
            key=lambda uid: (aggregated_neighbour_reach.get(uid, 0), uid),
            reverse=True,
        )
        injected: list[RoleCandidate] = []
        for uid in ranked_uids:
            name_path, kinds = uid_evidence[uid]
            name, _, file_path = name_path.partition("|")
            distance = distance_for(uid)
            injected.append(
                RoleCandidate(
                    uid=uid,
                    name=name,
                    file_path=file_path,
                    role=target_role,
                    satisfying_contracts=(),
                    satisfying_kinds=kinds,
                    contract_count=0,
                    kind_count=len(kinds),
                    vector_distance=distance,
                    score=_combined_score(
                        base_score, _semantic_score(distance), distance is not None
                    ),
                )
            )
        out[target_role] = injected[:max_injected_per_role]


@dataclass
class _LookaheadExpansionState:
    out: dict[str, list[RoleCandidate]]
    promotion_evidence: dict[str, dict[str, tuple[str, tuple[str, ...]]]]
    aggregated_neighbour_reach: dict[str, int]
    existing_uids_by_role: dict[str, set[str]]
    all_seed_uids: set[str]


def _expand_lookahead_for_source_role(
    source_role: str,
    *,
    candidates_by_role: Mapping[str, list[RoleCandidate]],
    db,
    lance,
    workspace_id: str,
    max_hops: int,
    include_tests: bool,
    prescanned,
    kind_to_roles: dict[str, set[str]],
    intent_set: set[str],
    base_score: float,
    max_injected_per_role: int,
    state: _LookaheadExpansionState,
    distance_for: Callable[[str], float | None] = lambda _uid: None,
) -> None:
    seed_uids = [c.uid for c in (candidates_by_role.get(source_role) or [])]
    if not seed_uids:
        return

    neighbours = walk_neighbours(
        db,
        workspace_id,
        seed_uids,
        edges=EdgeProfile.PROXIMITY,
        direction="undirected",
        max_hops=max_hops,
        exclude_tests=not include_tests,
    )
    neighbour_reach, flat_neighbours = _record_neighbour_reach(
        neighbours,
        state.aggregated_neighbour_reach,
    )
    flat_neighbours -= state.all_seed_uids
    if not flat_neighbours:
        return

    kinds_by_uid = _fetch_neighbour_kinds(
        lance,
        workspace_id,
        flat_neighbours,
        prescanned=prescanned,
    )
    per_target: dict[str, list[RoleCandidate]] = {}
    for uid, (name, file_path, kinds) in kinds_by_uid.items():
        matched_targets = _matched_target_roles_for_neighbour(
            kinds,
            source_role=source_role,
            kind_to_roles=kind_to_roles,
            intent_set=intent_set,
            existing_uids_by_role=state.existing_uids_by_role,
            uid=uid,
        )
        for target_role, evidence_kinds in matched_targets.items():
            if target_role in intent_set:
                per_target.setdefault(target_role, []).append(
                    _lookahead_injection_candidate(
                        uid=uid,
                        name=name,
                        file_path=file_path,
                        target_role=target_role,
                        evidence_kinds=evidence_kinds,
                        base_score=base_score,
                        distance=distance_for(uid),
                    )
                )
                state.existing_uids_by_role[target_role].add(uid)
            else:
                bucket = state.promotion_evidence.setdefault(target_role, {})
                bucket[uid] = (f"{name}|{file_path}", tuple(sorted(evidence_kinds)))

    _inject_lookahead_candidates(
        state.out,
        per_target,
        neighbour_reach=neighbour_reach,
        max_injected_per_role=max_injected_per_role,
    )


def expand_candidates_via_neighbourhood(
    intent_roles: list[str],
    candidates_by_role: Mapping[str, list[RoleCandidate]],
    *,
    db,
    lance,
    workspace_id: str,
    max_hops: int = 2,
    base_score: float = 0.4,
    max_injected_per_role: int = 8,
    auto_promote_min_hits: int = 3,
    auto_promote_role_pool: Iterable[str] | None = None,
    include_tests: bool = False,
    prescanned=None,
    query_text: str | None = None,
    embed_fn=None,
) -> dict[str, list[RoleCandidate]]:
    """Walk K hops from every role's candidates and use the
    container_kinds of the reached neighbours two ways:

    1. **Injection** — a neighbour whose kinds back a *different*
       intent role is appended to that role's candidate pool. The
       existing pool order and scores are preserved.
    2. **Auto-promotion** — a role that is *not* in ``intent_roles``
       but accumulates at least ``auto_promote_min_hits`` distinct
       neighbours through its evidence kinds is added to the output as
       a new role with those neighbours as its candidates. This is the
       structural answer to "the question gestures at proxies, but the
       structural mechanism lives in dispatcher methods" — the graph
       proves that role's relevance even when the intent classifier
       could not.

    A neighbour that is itself a seed (in any intent role) is never
    injected: the role pools already contain it. ``max_injected_per_role``
    caps the graph-derived pool so a dense graph cannot drown the
    vector signal.

    ``auto_promote_role_pool`` defaults to every role in
    ``ROLE_EVIDENCE_MAP``; pass a narrower set when the consumer wants
    promotion only inside a known sub-space (e.g. avoiding
    ``binding_surface`` umbrella inflation).
    """
    promote_pool: set[str] = set(
        auto_promote_role_pool if auto_promote_role_pool is not None else ROLE_EVIDENCE_MAP.keys()
    )
    relevant_roles = set(intent_roles) | promote_pool
    kind_to_roles = _build_kind_to_roles(relevant_roles)
    intent_set = set(intent_roles)

    # Query distances for injected candidates, looked up off the prescanned
    # matrix (one vectorised pass; no per-row loops). Neighbours outside the
    # scan (e.g. test-fenced rows) fall back to the flat ``base_score``.
    distances = (
        _scan_distances(prescanned, query_text, embed_fn)
        if prescanned is not None and query_text and embed_fn is not None
        else None
    )

    def _distance_for(uid: str) -> float | None:
        if distances is None:
            return None
        row = prescanned.rows_by_uid.get(uid)
        if row is None:
            return None
        idx = row.get("_idx")
        return float(distances[idx]) if idx is not None else None

    out: dict[str, list[RoleCandidate]] = {
        role: list(candidates_by_role.get(role) or []) for role in intent_roles
    }
    if not kind_to_roles:
        return out

    existing_uids_by_role: dict[str, set[str]] = {
        role: {c.uid for c in out[role]} for role in intent_roles
    }
    all_seed_uids: set[str] = set()
    for cands in out.values():
        all_seed_uids.update(c.uid for c in cands)

    # ``promotion_evidence[role][uid] = sorted-tuple of kinds`` — used
    # to decide which non-intent roles cross the auto-promote bar
    # *after* every source role has contributed evidence.
    promotion_evidence: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {}
    # Cumulative reach count across every source-role walk. Drives
    # the cap selection inside auto-promotion — a neighbour reachable
    # from many distinct seeds across multiple source roles is more
    # structurally central than one reached from a single use site.
    aggregated_neighbour_reach: dict[str, int] = {}

    expansion_state = _LookaheadExpansionState(
        out=out,
        promotion_evidence=promotion_evidence,
        aggregated_neighbour_reach=aggregated_neighbour_reach,
        existing_uids_by_role=existing_uids_by_role,
        all_seed_uids=all_seed_uids,
    )

    for source_role in intent_roles:
        _expand_lookahead_for_source_role(
            source_role,
            candidates_by_role=candidates_by_role,
            db=db,
            lance=lance,
            workspace_id=workspace_id,
            max_hops=max_hops,
            include_tests=include_tests,
            prescanned=prescanned,
            kind_to_roles=kind_to_roles,
            intent_set=intent_set,
            base_score=base_score,
            max_injected_per_role=max_injected_per_role,
            state=expansion_state,
            distance_for=_distance_for,
        )

    _auto_promote_lookahead_roles(
        out,
        promotion_evidence,
        aggregated_neighbour_reach=aggregated_neighbour_reach,
        auto_promote_min_hits=auto_promote_min_hits,
        max_injected_per_role=max_injected_per_role,
        base_score=base_score,
        distance_for=_distance_for,
    )

    return out


__all__ = ["expand_candidates_via_neighbourhood"]
