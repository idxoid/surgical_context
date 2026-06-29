"""Vector and unified search routes."""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Request

from context_engine.api.routes.deps import (
    AuthHeader,
    TraceIdHeader,
    UserIdHeader,
    WorkspaceHeader,
    require_services,
)
from context_engine.api.schemas import (
    SearchRequest,
    SearchResponse,
    UnifiedSearchRequest,
    UnifiedSearchResponse,
)
from context_engine.search import UnifiedSearchResult, dedupe_and_rank

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])


def _vector_doc_score(doc: dict[str, Any], rank: int) -> float:
    score = doc.get("score")
    return float(score if score is not None else 1 / (rank + 1))


def _symbol_semantic_score(symbol: dict[str, Any]) -> float | None:
    score = symbol.get("score")
    if score is None and symbol.get("distance") is not None:
        return max(0.0, 1.0 - float(symbol["distance"]))
    return score


def _ranked_vector_score(semantic: float | None, rank: int) -> float:
    return float(semantic if semantic is not None else 1 / (rank + 1))


def _unified_doc_results(docs: list[dict[str, Any]]) -> list[UnifiedSearchResult]:
    results: list[UnifiedSearchResult] = []
    for rank, doc in enumerate(docs):
        score = doc.get("score")
        results.append(
            {
                "type": "doc",
                "title": doc.get("id") or doc["file_path"],
                "file_path": doc["file_path"],
                "content": doc["chunk"],
                "score": _vector_doc_score(doc, rank),
                "scores": {"semantic": score},
                "provenance": ["vector:docs"],
                "metadata": {"rank": rank + 1, "distance": doc.get("distance")},
            }
        )
    return results


def _unified_symbol_results(symbols: list[dict[str, Any]]) -> list[UnifiedSearchResult]:
    results: list[UnifiedSearchResult] = []
    for rank, symbol in enumerate(symbols):
        semantic = _symbol_semantic_score(symbol)
        results.append(
            {
                "type": "symbol",
                "title": symbol["name"],
                "file_path": symbol["file_path"],
                "content": "",
                "score": _ranked_vector_score(semantic, rank),
                "scores": {"semantic": semantic},
                "provenance": ["vector:symbols"],
                "metadata": {"uid": symbol.get("uid"), "rank": rank + 1},
            }
        )
    return results


def _axis_graph_neighbors(
    *, request: Request | None = None, symbol: str, workspace_id: str, user_id: str, limit: int
) -> list[dict[str, Any]]:
    """Axis replacement for the deleted arbitrator graph-neighbor enrichment in
    /search/unified: resolve ``symbol`` to its workspace uid(s), then return its
    structural neighbours (one-hop PROXIMITY walk) as ``symbol`` search results
    tagged ``graph:neighbor``. Best-effort — empty on any error (never fatal to
    the search)."""
    from context_engine.axis.graph_walk import EdgeProfile, walk_neighbours

    main = require_services(request)
    try:
        with main.db_session(user_id=user_id) as db:
            with db.driver.session() as session:
                rec = session.run(
                    """
                    MATCH (f:File {workspace_id: $ws})-[:CONTAINS]->(s:Symbol {name: $name})
                    RETURN collect(DISTINCT s.uid) AS uids
                    """,
                    ws=workspace_id,
                    name=symbol,
                ).single()
            seed_uids = (rec and rec.get("uids")) or []
            if not seed_uids:
                return []
            neighbours = walk_neighbours(
                db,
                workspace_id,
                seed_uids,
                edges=EdgeProfile.PROXIMITY,
                direction="undirected",
                max_hops=1,
                limit=limit,
            )
    except Exception:
        logger.exception("/search axis graph-neighbor adapter failed; skipping graph results")
        return []

    return [
        {
            "type": "symbol",
            "title": n.name,
            "file_path": n.file_path,
            "content": "",
            "score": float(1.0 / (n.depth + 1)),
            "scores": {"graph": float(1.0 / (n.depth + 1))},
            "provenance": ["graph:neighbor"],
            "metadata": {"uid": n.uid, "depth": n.depth, "reach": n.reach},
        }
        for n in neighbours
    ]


@router.post("/search", response_model=SearchResponse)
def search(
    req: SearchRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    main = require_services(request)
    main._resolve_request_user(x_user_id, authorization)
    index_workspace_id = main._resolve_index_workspace(x_workspace, authorization)
    return {
        "results": main._vector_search_docs(req.query, req.limit, workspace_id=index_workspace_id)
    }


@router.post("/search/unified", response_model=UnifiedSearchResponse)
def unified_search(
    req: UnifiedSearchRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    x_trace_id: TraceIdHeader = None,
    request: Request = None,
):
    """Blend doc vectors, symbol vectors, and optional graph neighbors into one ranked list."""
    main = require_services(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    base_workspace_id = main._resolve_workspace(x_workspace, authorization)
    index_workspace_id = main.effective_index_workspace_id(base_workspace_id)
    trace = main._start_trace("/search/unified", x_trace_id, base_workspace_id)
    status = "ok"
    results: list[UnifiedSearchResult] = []
    try:
        with trace.stage("vector_docs"):
            docs = main._vector_search_docs(req.query, req.limit, workspace_id=index_workspace_id)
        results = _unified_doc_results(docs)

        with trace.stage("vector_symbols"):
            symbols = main._vector_search_symbols(
                req.query, req.limit, workspace_id=index_workspace_id
            )
        results.extend(_unified_symbol_results(symbols))

        if req.include_graph and req.symbol:
            with trace.stage("graph_neighbors"):
                results.extend(
                    cast(
                        Any,
                        main._axis_graph_neighbors(
                            request=request,
                            symbol=req.symbol,
                            workspace_id=index_workspace_id,
                            user_id=user_id,
                            limit=req.limit,
                        ),
                    )
                )

        ranked = dedupe_and_rank(results, req.limit)
        trace.token_counts["query"] = main.estimate_text_tokens(req.query)
        with main.db_session(user_id=user_id) as db:
            mid, sv = main._index_manifest_fields(db, index_workspace_id)
        return {
            "trace_id": trace.trace_id,
            "workspace_id": base_workspace_id,
            "results": ranked,
            "total": len(ranked),
            "index_manifest_id": mid,
            "index_manifest_schema_version": sv,
        }
    except Exception:
        status = "error"
        raise
    finally:
        main.default_metrics.record_trace(trace, status)
