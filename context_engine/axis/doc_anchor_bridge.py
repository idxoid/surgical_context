"""Reverse-USES_TYPE bridge from doc-anchor seeds.

Doc-anchor seeds often land on framework interfaces (``Module``, guards,
interceptors). The gold implementation sits in a file that *implements* the
interface via a ``USES_TYPE`` edge pointing at the seeded type. Walking
``USES_TYPE`` *forward* from the interface would fan out to hundreds of
referrers; walking *reverse* from a specific seeded interface is bounded
and tier-filtered to ``core`` files only.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from context_engine.axis.graph_walk import Neighbour, walk_neighbours
from context_engine.axis.role_retrieval import RoleCandidate, WorkspaceScan

_USES_TYPE = ("USES_TYPE",)


def _seed_idf_weights(seed_uids: list[str], *, db, workspace_id: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for uid in seed_uids:
        if not uid:
            continue
        try:
            with db.driver.session() as session:
                rec = session.run(
                    """
                    MATCH (n:Symbol {uid: $uid})<-[r:USES_TYPE {workspace_id: $workspace_id}]-()
                    RETURN count(r) AS indeg
                    """,
                    uid=uid,
                    workspace_id=workspace_id,
                ).single()
            indeg = max(int(rec["indeg"]) if rec else 1, 1)
        except Exception:
            indeg = 1
        weights[uid] = 1.0 / math.log(indeg + 1.0)
    return weights


def _core_library_boost(file_path: str) -> float:
    """Prefer ``packages/core`` implementations over sample/integration noise."""
    path = (file_path or "").replace("\\", "/")
    if "/packages/core/" in path or path.startswith("packages/core/"):
        return 1.0
    if "/packages/common/" in path or path.startswith("packages/common/"):
        return 0.85
    if "/integration/" in path or "/sample/" in path:
        return 0.2
    return 0.5


def _rank_bridge_neighbours(
    neighbours: list[Neighbour],
    *,
    rows_by_uid: dict[str, dict],
) -> list[Neighbour]:
    """Rank reverse-USES_TYPE neighbours before capping.

    Walk order is depth/reach-biased but not domain-aware — interceptors from
    a ``NestInterceptor`` doc seed can crowd out ``GuardsConsumer`` from a
    ``CanActivate`` seed when the global cap fires early. Sort by structural
    centrality (``reach``), then library-path boost, then uid for stability.
    """

    def _key(n: Neighbour) -> tuple[float, float, float, float, str]:
        row = rows_by_uid.get(n.uid) or {}
        tier = str(row.get("file_tier") or "core")
        if tier != "core":
            return (-1.0, -1.0, -1.0, -1.0, n.uid or "")
        path = n.file_path or str(row.get("file_path") or "")
        name = n.name or str(row.get("name") or "")
        consumerish = 1.0 if name.endswith(("Consumer", "ContextCreator")) else 0.0
        return (float(n.reach), _core_library_boost(path), consumerish, -float(n.depth), n.uid or "")

    ranked = [n for n in neighbours if _key(n)[0] >= 0.0]
    ranked.sort(key=_key, reverse=True)
    return ranked


def expand_doc_anchor_bridge(
    doc_anchor_candidates: Iterable[RoleCandidate],
    *,
    db,
    workspace_id: str,
    prescanned: WorkspaceScan | None = None,
    max_per_seed: int = 3,
    max_total: int = 20,
    include_tests: bool = False,
) -> list[RoleCandidate]:
    """One-hop reverse ``USES_TYPE`` from each doc-anchor seed, ``core`` tier only."""
    seeds = [c for c in doc_anchor_candidates if c.uid]
    if not seeds:
        return []

    rows_by_uid = prescanned.rows_by_uid if prescanned is not None else {}
    idf_by_seed = _seed_idf_weights([c.uid for c in seeds], db=db, workspace_id=workspace_id)

    out: list[RoleCandidate] = []
    seen: set[str] = set()
    for seed in seeds:
        neighbours = walk_neighbours(
            db,
            workspace_id,
            [seed.uid],
            edges=_USES_TYPE,
            direction="reverse",
            max_hops=1,
            exclude_tests=not include_tests,
        )
        if not neighbours:
            continue
        seed_score = 0.35 * idf_by_seed.get(seed.uid, 1.0)
        taken = 0
        for neighbour in _rank_bridge_neighbours(neighbours, rows_by_uid=rows_by_uid):
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
                    role="doc_anchor_bridge",
                    satisfying_contracts=(),
                    satisfying_kinds=("reverse_uses_type",),
                    contract_count=0,
                    kind_count=1,
                    vector_distance=None,
                    score=seed_score,
                    depth=neighbour.depth,
                    edge_type="USES_TYPE",
                )
            )
            taken += 1
            if taken >= max_per_seed or len(out) >= max_total:
                break
        if len(out) >= max_total:
            break
    return out


__all__ = ["expand_doc_anchor_bridge"]
