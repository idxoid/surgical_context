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

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from sidecar.axis.role_resolver import ROLE_EVIDENCE_MAP
from sidecar.axis.role_retrieval import RoleCandidate


# Same proximity-edge whitelist as
# ``sidecar.axis.cross_role_boost`` — when we widen it there we should
# widen it here too so intersection and expansion see the same graph.
# ``READS_ATTR`` / ``WRITES_ATTR`` / ``RESOLVES_ATTR`` are how proxy
# objects (Flask ``current_app``, Werkzeug ``LocalProxy``) connect to
# their consumers — the structural backbone the user pointed at for
# "this proxy is used inside that dispatcher".
_PROXIMITY_RELS: tuple[str, ...] = (
    "CALLS",
    "CALLS_DIRECT",
    "CALLS_SCOPED",
    "CALLS_IMPORTED",
    "CALLS_DYNAMIC",
    "CALLS_INFERRED",
    "CALLS_GUESS",
    "CALLS_EXTERNAL",
    "HAS_API",
    "INHERITED_API",
    "HANDLES",
    "INSTANTIATES",
    "DEPENDS_ON",
    "DECORATED_BY",
    "USES_TYPE",
    "REFERENCES",
    "READS_ATTR",
    "WRITES_ATTR",
    "RESOLVES_ATTR",
)


def _safe_rel_pattern(edge_types: Iterable[str]) -> str:
    """Concatenate edge types into a Cypher ``|``-pattern; reject anything
    that isn't an uppercase identifier so a malformed name can never
    smuggle a fragment into the query."""
    safe: list[str] = []
    pattern = re.compile(r"^[A-Z][A-Z0-9_]*$")
    for et in edge_types:
        if not pattern.match(et):
            raise ValueError(f"unsafe edge type: {et!r}")
        safe.append(et)
    return "|".join(safe)


def _flat_kinds(raw: Any) -> set[str]:
    """``axis_container_kinds_json`` is a JSON list of either kind names
    (older indexer) or dicts carrying ``{kind, payload, evidence_bits}``
    (current indexer). Flatten to a set of kind names."""
    if not raw:
        return set()
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return set()
    out: set[str] = set()
    for item in parsed:
        if isinstance(item, dict):
            name = item.get("kind") or item.get("name")
            if name:
                out.add(str(name))
        elif item is not None:
            out.add(str(item))
    return out


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


def _query_neighbour_uids(
    db,
    workspace_id: str,
    seed_uids: list[str],
    *,
    max_hops: int,
) -> dict[str, set[str]]:
    """For each seed uid, walk up to ``max_hops`` via the proximity-rel
    whitelist and return the set of reached Symbol uids. Workspace
    scoping comes from the ``File-CONTAINS-Symbol`` edge — Symbol nodes
    themselves do not carry ``workspace_id``."""
    if not seed_uids:
        return {}
    rel_pattern = _safe_rel_pattern(_PROXIMITY_RELS)
    cypher = f"""
    UNWIND $seed_uids AS su
    MATCH (f:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(s:Symbol {{uid: su}})
    MATCH (s)-[r:{rel_pattern}*1..{max_hops}]-(n:Symbol)
    RETURN su AS seed_uid, collect(DISTINCT n.uid) AS neighbours
    """
    out: dict[str, set[str]] = {}
    try:
        with db.driver.session() as session:
            for record in session.run(
                cypher,
                seed_uids=list(seed_uids),
                workspace_id=workspace_id,
            ):
                seed = str(record.get("seed_uid") or "")
                if not seed:
                    continue
                out[seed] = {
                    str(u) for u in record.get("neighbours") or [] if u
                }
    except Exception:
        return {}
    return out


def _fetch_neighbour_kinds(
    lance,
    workspace_id: str,
    neighbour_uids: set[str],
) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    """Read ``(name, file_path, container_kinds)`` for each requested
    neighbour. One full-table scan per call — acceptable at L4 cardinalities
    (≤ a few hundred neighbours per question). Workspace filtering is
    enforced row-by-row so this is safe to call against the shared sym
    table.
    """
    if not neighbour_uids:
        return {}
    sym_table = lance._sym_table  # noqa: SLF001 — the field is the public hook
    table = sym_table.to_lance().to_table(
        columns=[
            "uid",
            "name",
            "file_path",
            "axis_container_kinds_json",
            "workspace_id",
        ]
    )
    out: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for row in table.to_pylist():
        if row.get("workspace_id") != workspace_id:
            continue
        uid = str(row.get("uid") or "")
        if uid not in neighbour_uids:
            continue
        kinds = _flat_kinds(row.get("axis_container_kinds_json"))
        if not kinds:
            continue
        out[uid] = (
            str(row.get("name") or ""),
            str(row.get("file_path") or ""),
            tuple(sorted(kinds)),
        )
    return out


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
        auto_promote_role_pool
        if auto_promote_role_pool is not None
        else ROLE_EVIDENCE_MAP.keys()
    )
    relevant_roles = set(intent_roles) | promote_pool
    kind_to_roles = _build_kind_to_roles(relevant_roles)
    intent_set = set(intent_roles)

    out: dict[str, list[RoleCandidate]] = {
        role: list(candidates_by_role.get(role) or [])
        for role in intent_roles
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

    for source_role in intent_roles:
        seeds = out.get(source_role) or []
        if not seeds:
            continue
        # Seed by *original* vector candidates only — keeps the
        # lookahead one hop of indirection deep, not a recursive
        # expansion.
        seed_uids = [
            c.uid for c in (candidates_by_role.get(source_role) or [])
        ]
        if not seed_uids:
            continue

        neighbours_by_seed = _query_neighbour_uids(
            db, workspace_id, seed_uids, max_hops=max_hops,
        )
        flat_neighbours: set[str] = set()
        for ns in neighbours_by_seed.values():
            flat_neighbours |= ns
        flat_neighbours -= all_seed_uids
        if not flat_neighbours:
            continue

        kinds_by_uid = _fetch_neighbour_kinds(
            lance, workspace_id, flat_neighbours,
        )
        per_target: dict[str, list[RoleCandidate]] = {}
        for uid, (name, file_path, kinds) in kinds_by_uid.items():
            matched_targets: dict[str, set[str]] = {}
            for kind in kinds:
                for target_role in kind_to_roles.get(kind, ()):
                    if target_role == source_role:
                        continue
                    if (
                        target_role in intent_set
                        and uid in existing_uids_by_role.get(target_role, set())
                    ):
                        continue
                    matched_targets.setdefault(target_role, set()).add(kind)
            for target_role, evidence_kinds in matched_targets.items():
                if target_role in intent_set:
                    per_target.setdefault(target_role, []).append(
                        RoleCandidate(
                            uid=uid,
                            name=name,
                            file_path=file_path,
                            role=target_role,
                            satisfying_contracts=(),
                            satisfying_kinds=tuple(sorted(evidence_kinds)),
                            contract_count=0,
                            kind_count=len(evidence_kinds),
                            vector_distance=None,
                            score=base_score,
                        )
                    )
                    existing_uids_by_role[target_role].add(uid)
                else:
                    # Non-intent role — bank evidence for promotion
                    # decision. ``(file_path, kinds)`` are kept so the
                    # synthesised RoleCandidate can be built later.
                    bucket = promotion_evidence.setdefault(target_role, {})
                    bucket[uid] = (
                        f"{name}|{file_path}",
                        tuple(sorted(evidence_kinds)),
                    )

        for target_role, items in per_target.items():
            out[target_role].extend(items[:max_injected_per_role])

    # Auto-promote non-intent roles that accumulated enough evidence.
    for target_role, uid_evidence in promotion_evidence.items():
        if len(uid_evidence) < auto_promote_min_hits:
            continue
        injected: list[RoleCandidate] = []
        for uid, (name_path, kinds) in uid_evidence.items():
            name, _, file_path = name_path.partition("|")
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
                    vector_distance=None,
                    score=base_score,
                )
            )
        out[target_role] = injected[:max_injected_per_role]

    return out


__all__ = ["expand_candidates_via_neighbourhood"]
