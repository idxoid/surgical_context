"""HTTP endpoint bridge for cross-language client↔handler seed expansion.

When routing/trace seeds land on a backend handler (``ask`` under
``@app.post("/ask")``), seed recall also needs the extension symbols that
call the HTTP client surface (``SidecarClient`` → ``SurgicalContextViewProvider``).
Index-time ``CALLS_ENDPOINT`` / ``IMPLEMENTS_ENDPOINT`` edges link handler
and client through a shared ``ApiEndpoint`` node; this pass walks:

  handler seed → ApiEndpoint ← HTTP client → reverse ``CALLS_*`` callers
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from context_engine.axis.graph_walk import EdgeProfile, Neighbour, walk_neighbours
from context_engine.axis.role_retrieval import RoleCandidate, WorkspaceScan

_CALLS = EdgeProfile.CALLS


def _seed_idf_weights(seed_uids: list[str], *, db, workspace_id: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for uid in seed_uids:
        if not uid:
            continue
        try:
            with db.driver.session() as session:
                rec = session.run(
                    """
                    MATCH (n:Symbol {uid: $uid})-[:CALLS_ENDPOINT {workspace_id: $workspace_id}]->()
                    RETURN count(*) AS outdeg
                    """,
                    uid=uid,
                    workspace_id=workspace_id,
                ).single()
            outdeg = max(int(rec["outdeg"]) if rec else 1, 1)
        except Exception:
            outdeg = 1
        weights[uid] = 1.0 / math.log(outdeg + 1.0)
    return weights


def _http_client_uids_for_seed(session, seed_uid: str, workspace_id: str) -> list[str]:
    clients: list[str] = []
    for row in session.run(
        """
        MATCH (s:Symbol {uid: $uid})-[:IMPLEMENTS_ENDPOINT {workspace_id: $ws}]->(:ApiEndpoint)
              <-[:CALLS_ENDPOINT {workspace_id: $ws}]-(client:Symbol)
        RETURN DISTINCT client.uid AS uid
        """,
        uid=seed_uid,
        ws=workspace_id,
    ):
        uid = row.get("uid")
        if uid:
            clients.append(str(uid))
    if (
        session.run(
            """
        MATCH (s:Symbol {uid: $uid})-[:CALLS_ENDPOINT {workspace_id: $ws}]->(:ApiEndpoint)
        RETURN s.uid AS uid LIMIT 1
        """,
            uid=seed_uid,
            ws=workspace_id,
        ).single()
        and seed_uid not in clients
    ):
        clients.append(seed_uid)
    return clients


def _collect_http_clients(db, workspace_id: str, seed_uids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not seed_uids:
        return out
    try:
        with db.driver.session() as session:
            for seed_uid in seed_uids:
                clients = _http_client_uids_for_seed(session, seed_uid, workspace_id)
                if clients:
                    out[seed_uid] = clients
    except Exception:
        return {}
    return out


def _rank_http_callers(
    neighbours: list[Neighbour],
    *,
    rows_by_uid: dict[str, dict],
) -> list[Neighbour]:
    """Structural ranking: keep ``core`` tier only, then order by ``reach`` (how
    many client seeds reach the caller — the entry-point surface), then shallower
    depth, then uid. No symbol-name or path literals.
    """

    def _key(n: Neighbour) -> tuple[float, float, str]:
        row = rows_by_uid.get(n.uid) or {}
        if str(row.get("file_tier") or "core") != "core":
            return (-1.0, -1.0, n.uid or "")
        return (float(n.reach), -float(n.depth), n.uid or "")

    ranked = [n for n in neighbours if _key(n)[0] >= 0.0]
    ranked.sort(key=_key, reverse=True)
    return ranked


def _http_endpoint_bridge_candidate(
    neighbour: Neighbour,
    *,
    rows_by_uid: dict[str, dict],
    seed_score: float,
) -> RoleCandidate:
    owner_row = rows_by_uid.get(neighbour.uid) or {}
    return RoleCandidate(
        uid=neighbour.uid,
        name=neighbour.name or str(owner_row.get("name") or ""),
        qualified_name=str(owner_row.get("qualified_name") or ""),
        file_path=neighbour.file_path or str(owner_row.get("file_path") or ""),
        role="http_endpoint_bridge",
        satisfying_contracts=(),
        satisfying_kinds=("http_client_caller",),
        contract_count=0,
        kind_count=1,
        vector_distance=None,
        score=seed_score,
        depth=neighbour.depth,
        edge_type="CALLS",
    )


def _expand_http_endpoint_for_seed(
    seed: RoleCandidate,
    *,
    db,
    workspace_id: str,
    clients_by_seed: dict[str, list[str]],
    rows_by_uid: dict[str, dict],
    idf_by_seed: dict[str, float],
    include_tests: bool,
    max_per_seed: int,
    max_total: int,
    seen: set[str],
    out: list[RoleCandidate],
) -> bool:
    """Expand one handler seed; return True when ``max_total`` is reached."""
    client_uids = clients_by_seed.get(seed.uid)
    if not client_uids:
        return False
    callers = walk_neighbours(
        db,
        workspace_id,
        client_uids,
        edges=_CALLS,
        direction="reverse",
        max_hops=1,
        exclude_tests=not include_tests,
    )
    if not callers:
        return False
    seed_score = max(0.32 * idf_by_seed.get(seed.uid, 1.0), float(seed.score) * 0.45)
    taken = 0
    for neighbour in _rank_http_callers(callers, rows_by_uid=rows_by_uid):
        uid = neighbour.uid
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append(
            _http_endpoint_bridge_candidate(
                neighbour,
                rows_by_uid=rows_by_uid,
                seed_score=seed_score,
            )
        )
        taken += 1
        if taken >= max_per_seed or len(out) >= max_total:
            return len(out) >= max_total
    return len(out) >= max_total


def expand_http_endpoint_bridge(
    seeds: Iterable[RoleCandidate],
    *,
    db,
    workspace_id: str,
    prescanned: WorkspaceScan | None = None,
    max_per_seed: int = 4,
    max_total: int = 20,
    include_tests: bool = False,
) -> list[RoleCandidate]:
    """Expand handler/client seeds to HTTP client callers (extension/webview layer)."""
    seed_list = [c for c in seeds if c.uid]
    if not seed_list:
        return []

    rows_by_uid = prescanned.rows_by_uid if prescanned is not None else {}
    idf_by_seed = _seed_idf_weights([c.uid for c in seed_list], db=db, workspace_id=workspace_id)
    clients_by_seed = _collect_http_clients(db, workspace_id, [c.uid for c in seed_list])
    if not clients_by_seed:
        return []

    out: list[RoleCandidate] = []
    seen: set[str] = set()
    for seed in seed_list:
        if _expand_http_endpoint_for_seed(
            seed,
            db=db,
            workspace_id=workspace_id,
            clients_by_seed=clients_by_seed,
            rows_by_uid=rows_by_uid,
            idf_by_seed=idf_by_seed,
            include_tests=include_tests,
            max_per_seed=max_per_seed,
            max_total=max_total,
            seen=seen,
            out=out,
        ):
            break
    return out


__all__ = ["expand_http_endpoint_bridge"]
