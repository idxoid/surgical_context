"""Hook API bridge — seed/pool expansion to registration dispatch APIs.

``context_builder._hook_transparency_hits`` already walks the EVENT channel →
HOOK wrapper chain for *rendered context*, but the axis seed layer never crossed
it — so an event-topic seed could reach the topic module while missing the
registration API (the ``listen``/``subscribe`` dispatch surface) that wires
handlers to it.

This pass mirrors that two-hop archetype for retrieval candidates:

  seed (+ its API surface) → EVENT_SUB/EVENT_PUB sites → HOOK_CONFIG/HOOK_EXEC API

Gating and ranking are STRUCTURAL only (engineering_principles.md P2/P3). The
"is this an event topic" gate is the EVENT channel itself: a seed contributes
only if walking EVENT_SUB/EVENT_PUB from it (or its members) reaches a site — no
path/name/kind table. The registration API is ranked by file-tier (``core``) and
``reach`` (how many event sites converge on it — the dispatch hub), never by a
symbol name or library path.
"""

from __future__ import annotations

from collections.abc import Iterable

from context_engine.axis.graph_walk import Neighbour, walk_neighbours
from context_engine.axis.role_retrieval import RoleCandidate, WorkspaceScan

_EVENT_CHANNEL = ("EVENT_SUB", "EVENT_PUB")
_HOOK_API = ("HOOK_CONFIG", "HOOK_EXEC")
_TOPIC_SURFACE = ("HAS_API", "INHERITED_API")
_WALK_LIMIT = 64


def _rank_api_neighbours(
    neighbours: list[Neighbour],
    *,
    rows_by_uid: dict[str, dict],
) -> list[Neighbour]:
    """Structural ranking: keep ``core`` tier only, then order by ``reach`` (the
    dispatch hub many event sites converge on), then shallower depth, then uid.

    All neighbours already arrived via a HOOK_CONFIG/HOOK_EXEC edge, so edge
    membership is implicit; no symbol-name or path literals are consulted.
    """

    def _key(n: Neighbour) -> tuple[float, float, str]:
        row = rows_by_uid.get(n.uid) or {}
        if str(row.get("file_tier") or "core") != "core":
            return (-1.0, -1.0, n.uid or "")
        return (float(n.reach), -float(n.depth), n.uid or "")

    ranked = [n for n in neighbours if _key(n)[0] >= 0.0]
    ranked.sort(key=_key, reverse=True)
    return ranked


def expand_hook_api_bridge(
    seeds: Iterable[RoleCandidate],
    *,
    db,
    workspace_id: str,
    prescanned: WorkspaceScan | None = None,
    max_total: int = 16,
    include_tests: bool = False,
) -> list[RoleCandidate]:
    """Two-hop EVENT channel → HOOK wrapper expansion.

    The EVENT walk is the structural gate: seeds with no EVENT_SUB/EVENT_PUB
    reach (directly or through their HAS_API members) contribute nothing.
    """
    rows_by_uid = prescanned.rows_by_uid if prescanned is not None else {}
    seed_list = [c for c in seeds if c.uid]
    if not seed_list:
        return []
    seed_uids = list(dict.fromkeys(c.uid for c in seed_list))

    # Phase A: topics + their API surface (EVENT edges hang off handler members,
    # not always the topic class node itself).
    members = walk_neighbours(
        db,
        workspace_id,
        seed_uids,
        edges=_TOPIC_SURFACE,
        direction="forward",
        max_hops=1,
        exclude_tests=not include_tests,
        limit=_WALK_LIMIT,
    )
    topic_uids = list(dict.fromkeys(seed_uids + [n.uid for n in members if n.uid]))

    # Phase B: EVENT channel sites — structural gate, only event-participating
    # topics yield.
    sites = walk_neighbours(
        db,
        workspace_id,
        topic_uids,
        edges=_EVENT_CHANNEL,
        direction="undirected",
        max_hops=1,
        exclude_tests=not include_tests,
        limit=_WALK_LIMIT,
    )
    site_uids = list(dict.fromkeys(n.uid for n in sites if n.uid))
    if not site_uids:
        return []

    # Phase C: HOOK_CONFIG/HOOK_EXEC registration API.
    apis = walk_neighbours(
        db,
        workspace_id,
        site_uids,
        edges=_HOOK_API,
        direction="undirected",
        max_hops=1,
        exclude_tests=not include_tests,
        limit=_WALK_LIMIT,
    )
    if not apis:
        return []

    base_score = max(0.3, max((float(c.score) for c in seed_list), default=0.6) * 0.45)
    out: list[RoleCandidate] = []
    seen: set[str] = set()
    for neighbour in _rank_api_neighbours(apis, rows_by_uid=rows_by_uid):
        uid = neighbour.uid
        if not uid or uid in seen:
            continue
        owner_row = rows_by_uid.get(uid) or {}
        seen.add(uid)
        out.append(
            RoleCandidate(
                uid=uid,
                name=neighbour.name or str(owner_row.get("name") or ""),
                qualified_name=str(owner_row.get("qualified_name") or ""),
                file_path=neighbour.file_path or str(owner_row.get("file_path") or ""),
                role="hook_api_bridge",
                satisfying_contracts=(),
                satisfying_kinds=("hook_register_api",),
                contract_count=0,
                kind_count=1,
                vector_distance=None,
                score=base_score,
                depth=neighbour.depth,
                edge_type="HOOK_CONFIG",
            )
        )
        if len(out) >= max_total:
            break
    return out


__all__ = ["expand_hook_api_bridge"]
