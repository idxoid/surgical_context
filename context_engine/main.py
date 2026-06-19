"""FastAPI sidecar — install stderr filtering before LanceDB / SentenceTransformer import."""

from context_engine.silence import install as _install_stderr_filter

_install_stderr_filter()

import hashlib
import logging
import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

from fastapi import Header, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse

from context_engine.api.app import create_app
from context_engine.api.config import load_sidecar_config
from context_engine.api.deps import (
    canonical_user_id as _canonical_user_id,
)
from context_engine.api.deps import (
    header_value as _header_value,
)
from context_engine.api.deps import (
    resolve_request_user,
    resolve_workspace,
    resolve_workspace_context,
)
from context_engine.api.errors import (
    LLM_UNREACHABLE_REASON,
    PUBLIC_INTERNAL_ERROR,
    degraded_llm_answer,
)
from context_engine.api.routes.indexing import (
    IndexingRouteDeps,
    register_indexing_routes,
)
from context_engine.api.schemas import (
    IMPACT_DEPTH_MAX,
    IMPACT_DEPTH_MIN,
    AskAxisRequest,
    AskAxisResponse,
    AskRequest,
    AskResponse,
    AuditActionsResponse,
    AuthTokenResponse,
    AxisCandidateResponse,
    AxisContextBundleResponse,
    AxisContextSymbolResponse,
    AxisIntentMatchResponse,
    ClearOverlayResponse,
    CloudStatusResponse,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    HistoryAskRecordRequest,
    HistoryAskRecordResponse,
    HistoryConversationResponse,
    HistoryConversationsResponse,
    HistoryRequestBundleResponse,
    ImpactResponse,
    IndexFileRequest,  # noqa: F401 — re-exported for endpoint tests
    IndexFilesRequest,  # noqa: F401 — re-exported for endpoint tests
    IndexRequest,  # noqa: F401 — re-exported for endpoint tests
    OverlayRequest,
    OverlayResponse,
    SearchRequest,
    SearchResponse,
    UnifiedSearchRequest,
    UnifiedSearchResponse,
    UsersResponse,
)
from context_engine.api.sse import format_sse
from context_engine.api.state import SidecarState, build_sidecar_state
from context_engine.api.workspace_security import (
    authorize_workspace_project_root,
    require_workspace_root_dir,
    sandbox_path,
)
from context_engine.cache.layered import default_cache
from context_engine.context_types import (
    CONTEXT_PIPELINE_VERSION,
    DocChunk,
    PromptContext,
    SymbolContext,
)
from context_engine.database.session import db_session
from context_engine.doc_resolver import DocResolver
from context_engine.feedback import FeedbackEvent, RetrievalSnapshot
from context_engine.history import hash_history_text
from context_engine.index_profile import effective_index_workspace_id
from context_engine.indexer.git_delta_poller import GitDeltaTarget
from context_engine.indexer.job_log import IndexJobLog
from context_engine.indexer.queue import EnqueueResult, IndexWorkItem
from context_engine.observability import (
    RequestTrace,
    default_metrics,
    estimate_cost_usd,
    estimate_text_tokens,
    new_trace_id,
)
from context_engine.search import UnifiedSearchResult, dedupe_and_rank
from context_engine.workspace import DEFAULT_WORKSPACE_ID, Workspace

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

state: SidecarState


def _resolve_index_workspace(x_workspace: Any = None, authorization: Any = None) -> str:
    """Physical index namespace for the active profile (Neo4j/LanceDB reads/writes)."""
    return effective_index_workspace_id(_resolve_workspace(x_workspace, authorization))


def _start_trace(endpoint: str, x_trace_id: Any = None, workspace_id: str = "") -> RequestTrace:
    return RequestTrace(
        trace_id=new_trace_id(_header_value(x_trace_id)),
        endpoint=endpoint,
        workspace_id=workspace_id,
    )


state = build_sidecar_state(load_sidecar_config())

config = state.config
MODEL_PREFERENCE = config.model_preference
ALLOW_CLOUD_LLM = config.allow_cloud_llm
AUTH_REQUIRED = config.auth_required
TRUST_CLIENT_USER_HEADER = config.trust_client_user_header
TRUST_CLIENT_WORKSPACE_HEADER = config.trust_client_workspace_header

overlay = state.overlay
vector_db = state.vector_db
ai_engine = state.ai_engine
user_auth = state.user_auth
audit_log = state.audit_log
workspace_resolver = state.workspace_resolver
feedback_store = state.feedback_store
history_provider = state.history_provider
index_queue = state.index_queue
git_delta_registry = state.git_delta_registry
git_delta_poller = state.git_delta_poller
indexing_service = state.indexing_service

ask_context_builder = state.ask_context_builder
ask_service = state.ask_service


def _context_from_file(**kwargs):
    ask_context_builder.vector_db = vector_db
    ask_context_builder.overlay = overlay
    return ask_context_builder.context_from_file(**kwargs)


def _context_from_workspace(*args, **kwargs):
    ask_context_builder.vector_db = vector_db
    return ask_context_builder.context_from_workspace(*args, **kwargs)


def _context_from_direct(*args, **kwargs):
    return ask_context_builder.context_from_direct(*args, **kwargs)


def _context_from_axis(*args, **kwargs):
    ask_context_builder.overlay = overlay
    return ask_context_builder.context_from_axis(*args, **kwargs)


def _ask_axis_first_enabled() -> bool:
    return ask_context_builder.ask_axis_first_enabled()


def _try_axis_context(**kwargs):
    return ask_context_builder.try_axis_context(
        **kwargs,
        context_from_axis=_context_from_axis,
    )


def _context_budget(ctx: Any) -> dict[str, Any]:
    return ask_context_builder.context_budget(ctx)


def _resolve_ask_context(**kwargs):
    return ask_context_builder.resolve_ask_context(
        **kwargs,
        sandbox_path=_sandbox_path,
        context_from_axis=_context_from_axis,
        context_from_file=_context_from_file,
        context_from_workspace=_context_from_workspace,
        context_from_direct=_context_from_direct,
    )


def _context_file_paths(ctx: PromptContext) -> list[str]:
    return ask_context_builder.context_file_paths(ctx)


def _index_manifest_fields(db: Any, workspace_id: str) -> tuple[str | None, int | None]:
    return ask_service.index_manifest_fields(db, workspace_id)


def _attach_index_manifest(ctx: PromptContext, db: Any, workspace_id: str) -> None:
    ask_service.attach_index_manifest(ctx, db, workspace_id)


def _attach_trace_metadata(ctx: PromptContext, trace: RequestTrace) -> None:
    ask_service.attach_trace_metadata(ctx, trace)


def _request_metrics(trace: RequestTrace) -> dict[str, Any]:
    return ask_service.request_metrics(trace)


def _stream_trace_payload(
    trace: RequestTrace,
    *,
    stage: str | None = None,
    ctx: PromptContext | None = None,
) -> dict[str, Any]:
    return ask_service.stream_trace_payload(trace, stage=stage, ctx=ctx)


def _system_prompt_for_context(ctx: PromptContext) -> str:
    return ask_service.system_prompt_for_context(ctx)


def _vector_search_docs(query: str, limit: int, *, workspace_id: str) -> list[dict[str, Any]]:
    try:
        return vector_db.search(query, limit, workspace_id=workspace_id)
    except TypeError:
        return vector_db.search(query, limit)


def _vector_search_symbols(query: str, limit: int, *, workspace_id: str) -> list[dict[str, Any]]:
    search_symbols = getattr(vector_db, "search_symbols", None)
    if not callable(search_symbols):
        return []
    try:
        return cast(
            list[dict[str, Any]],
            search_symbols(query, limit, threshold=1.0, workspace_id=workspace_id),
        )
    except TypeError:
        return cast(list[dict[str, Any]], search_symbols(query, limit, threshold=1.0))


def _history_conversation_for_scope(
    conversation_id: str,
    *,
    workspace_id: str,
    user_id: str,
) -> dict[str, Any]:
    conversation = history_provider.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown history conversation")
    if conversation["workspace_id"] != workspace_id:
        raise HTTPException(
            status_code=403, detail="History conversation belongs to another workspace"
        )
    if conversation["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="History conversation belongs to another user")
    return conversation


def _history_enabled() -> bool:
    return bool(getattr(history_provider, "enabled", True))


def _history_snapshot(
    req: HistoryAskRecordRequest,
    *,
    workspace_id: str,
    user_id: str,
    answer_summary: str,
) -> dict[str, Any]:
    return {
        **req.ask_snapshot,
        "request_id": req.request_id,
        "trace_id": req.trace_id,
        "feedback_token": req.feedback_token,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "symbol": req.symbol,
        "answer_summary": answer_summary,
    }




def _index_file_now(file_path: str, base_workspace_id: str, user_id: str) -> int:
    import context_engine.indexer.service as index_service_mod

    index_service_mod.db_session = db_session
    index_service_mod.IndexJobLog = IndexJobLog
    indexing_service.vector_db = vector_db
    indexing_service.overlay = overlay
    return indexing_service.index_file_now(file_path, base_workspace_id, user_id)


def _enqueue_index_file(file_path: str, workspace_id: str, user_id: str) -> EnqueueResult:
    indexing_service.attach_queue(index_queue)
    return indexing_service.enqueue_index_file(file_path, workspace_id, user_id)


def _enqueue_index_files(
    file_paths: list[str],
    workspace_id: str,
    user_id: str,
) -> list[EnqueueResult]:
    return indexing_service.enqueue_index_files(file_paths, workspace_id, user_id)


def _summarize_enqueue_results(results: list[EnqueueResult]) -> dict[str, int]:
    return indexing_service.summarize_enqueue_results(results)


def _process_index_batch(items: list[IndexWorkItem]) -> None:
    import context_engine.indexer.service as index_service_mod

    index_service_mod.db_session = db_session
    indexing_service.vector_db = vector_db
    indexing_service.overlay = overlay
    indexing_service.metrics = default_metrics
    indexing_service.process_index_batch(items)


def _track_git_delta_target(workspace_id: str, project_path: str, user_id: str) -> None:
    indexing_service.track_git_delta_target(workspace_id, project_path, user_id)


def _apply_git_head_delta_for_workspace(
    *,
    workspace_id: str,
    user_id: str,
    project_root: Path,
    db: Any,
    queue: bool,
) -> dict[str, Any]:
    return indexing_service.apply_git_head_delta_for_workspace(
        workspace_id=workspace_id,
        user_id=user_id,
        project_root=project_root,
        db=db,
        queue=queue,
    )


def _poll_git_delta_target(target: GitDeltaTarget) -> dict[str, Any] | None:
    return indexing_service.poll_git_delta_target(target)


def _resolve_request_user(
    x_user_id: Any = None,
    authorization: Any = None,
    *,
    require_auth: bool | None = None,
) -> str:
    return resolve_request_user(
        state,
        x_user_id,
        authorization,
        require_auth=AUTH_REQUIRED if require_auth is None else require_auth,
        trust_client_user_header=TRUST_CLIENT_USER_HEADER,
    )


def _resolve_workspace_context(
    x_workspace: Any = None,
    authorization: Any = None,
) -> Workspace:
    return resolve_workspace_context(
        state,
        x_workspace,
        authorization,
        trust_client_workspace_header=TRUST_CLIENT_WORKSPACE_HEADER,
    )


def _resolve_workspace(x_workspace: Any = None, authorization: Any = None) -> str:
    return resolve_workspace(state, x_workspace, authorization)


def _require_workspace_root_dir(raw_project_path: str) -> Path:
    return require_workspace_root_dir(raw_project_path)


def _authorize_workspace_project_root(
    project_root: Path,
    *,
    workspace: Workspace,
    db: Any,
) -> None:
    authorize_workspace_project_root(project_root, workspace=workspace, db=db)


def _sandbox_path(
    raw_path: str,
    *,
    workspace_id: str,
    db: Any,
    workspace_root=None,
) -> str:
    return sandbox_path(
        raw_path,
        workspace_id=workspace_id,
        db=db,
        workspace_root=workspace_root,
    )


app = create_app(state)

register_indexing_routes(
    app,
    IndexingRouteDeps(
        main=sys.modules[__name__],
        state=state,
        indexing=indexing_service,
    ),
)

from context_engine.api.routes import indexing as _indexing_routes

index = _indexing_routes.index  # noqa: F401 — re-exported for endpoint tests
index_docs_endpoint = _indexing_routes.index_docs_endpoint  # noqa: F401
index_file_endpoint = _indexing_routes.index_file_endpoint  # noqa: F401
index_files_endpoint = _indexing_routes.index_files_endpoint  # noqa: F401
index_git_delta_endpoint = _indexing_routes.index_git_delta_endpoint  # noqa: F401
index_git_delta_status = _indexing_routes.index_git_delta_status  # noqa: F401
index_manifest_endpoint = _indexing_routes.index_manifest_endpoint  # noqa: F401
index_queue_status = _indexing_routes.index_queue_status  # noqa: F401


@app.get("/health", response_model=HealthResponse)
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(default_metrics.render_prometheus(), media_type="text/plain")


@app.post("/search", response_model=SearchResponse)
def search(
    req: SearchRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    _resolve_request_user(x_user_id, authorization)
    index_workspace_id = _resolve_index_workspace(x_workspace, authorization)
    return {"results": _vector_search_docs(req.query, req.limit, workspace_id=index_workspace_id)}


def _axis_graph_neighbors(
    *, symbol: str, workspace_id: str, user_id: str, limit: int
) -> list[dict[str, Any]]:
    """Axis replacement for the deleted arbitrator graph-neighbor enrichment in
    /search/unified: resolve ``symbol`` to its workspace uid(s), then return its
    structural neighbours (one-hop PROXIMITY walk) as ``symbol`` search results
    tagged ``graph:neighbor``. Best-effort — empty on any error (never fatal to
    the search)."""
    from context_engine.axis.graph_walk import EdgeProfile, walk_neighbours

    try:
        with db_session(user_id=user_id) as db:
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


@app.post("/search/unified", response_model=UnifiedSearchResponse)
def unified_search(
    req: UnifiedSearchRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
    x_trace_id: str = Header(None),
):
    """Blend doc vectors, symbol vectors, and optional graph neighbors into one ranked list."""
    user_id = _resolve_request_user(x_user_id, authorization)
    base_workspace_id = _resolve_workspace(x_workspace, authorization)
    index_workspace_id = effective_index_workspace_id(base_workspace_id)
    trace = _start_trace("/search/unified", x_trace_id, base_workspace_id)
    status = "ok"
    results: list[UnifiedSearchResult] = []
    try:
        with trace.stage("vector_docs"):
            docs = _vector_search_docs(req.query, req.limit, workspace_id=index_workspace_id)
        for rank, doc in enumerate(docs):
            score = doc.get("score")
            results.append(
                {
                    "type": "doc",
                    "title": doc.get("id") or doc["file_path"],
                    "file_path": doc["file_path"],
                    "content": doc["chunk"],
                    "score": float(score if score is not None else 1 / (rank + 1)),
                    "scores": {"semantic": score},
                    "provenance": ["vector:docs"],
                    "metadata": {"rank": rank + 1, "distance": doc.get("distance")},
                }
            )

        with trace.stage("vector_symbols"):
            symbols = _vector_search_symbols(req.query, req.limit, workspace_id=index_workspace_id)
        if symbols:
            for rank, symbol in enumerate(symbols):
                score = symbol.get("score")
                if score is None and symbol.get("distance") is not None:
                    score = max(0.0, 1.0 - float(symbol["distance"]))
                results.append(
                    {
                        "type": "symbol",
                        "title": symbol["name"],
                        "file_path": symbol["file_path"],
                        "content": "",
                        "score": float(score if score is not None else 1 / (rank + 1)),
                        "scores": {"semantic": score},
                        "provenance": ["vector:symbols"],
                        "metadata": {"uid": symbol.get("uid"), "rank": rank + 1},
                    }
                )

        # Graph-neighbor enrichment via the axis graph walk (replaces the deleted
        # arbitrator path): one-hop PROXIMITY neighbours of req.symbol, best-effort.
        if req.include_graph and req.symbol:
            with trace.stage("graph_neighbors"):
                results.extend(
                    cast(
                        Any,
                        _axis_graph_neighbors(
                            symbol=req.symbol,
                            workspace_id=index_workspace_id,
                            user_id=user_id,
                            limit=req.limit,
                        ),
                    )
                )

        ranked = dedupe_and_rank(results, req.limit)
        trace.token_counts["query"] = estimate_text_tokens(req.query)
        with db_session(user_id=user_id) as db:
            mid, sv = _index_manifest_fields(db, index_workspace_id)
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
        default_metrics.record_trace(trace, status)


@app.post("/overlay", response_model=OverlayResponse)
def update_overlay(
    req: OverlayRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    with db_session(user_id=user_id) as db:
        safe_path = _sandbox_path(req.file_path, workspace_id=workspace_id, db=db)
    overlay.update(
        safe_path,
        req.content,
        workspace_id=workspace_id,
        user_id=user_id,
        dirty=req.dirty,
    )
    symbols = overlay.get_symbols(safe_path, workspace_id=workspace_id, user_id=user_id)
    return {"file_path": safe_path, "symbols": list(symbols.keys())}


@app.delete("/overlay", response_model=ClearOverlayResponse)
def clear_overlay(
    file_path: str,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    with db_session(user_id=user_id) as db:
        safe_path = _sandbox_path(file_path, workspace_id=workspace_id, db=db)
    overlay.clear(safe_path, workspace_id=workspace_id, user_id=user_id)
    return {"cleared": safe_path}


@app.post("/ask", response_model=AskResponse)
def ask(
    req: AskRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
    x_trace_id: str = Header(None),
):
    """Ask about a symbol (with multi-user audit logging)."""
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    trace = _start_trace("/ask", x_trace_id, workspace_id)
    status = "ok"
    try:
        ask_service.ai_engine = ai_engine
        ask_service.audit_log = audit_log
        ask_service.feedback_store = feedback_store
        ask_service.default_cache = default_cache
        with db_session(user_id=user_id) as db:
            return ask_service.ask(
                req,
                user_id=user_id,
                workspace_id=workspace_id,
                trace=trace,
                db=db,
                resolve_ask_context=_resolve_ask_context,
            )
    except HTTPException:
        raise
    except Exception:
        status = "error"
        logger.exception("trace_id=%s endpoint=/ask status=error", trace.trace_id)
        raise
    finally:
        default_metrics.record_trace(trace, status)

@app.post("/ask/axis", response_model=AskAxisResponse)
def ask_axis(
    req: AskAxisRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
    x_trace_id: str = Header(None),
):
    """Axis-pipeline answer: intent → roles → ranked candidates → context."""
    user_id = _resolve_request_user(x_user_id, authorization)
    base_workspace_id = _resolve_workspace(x_workspace, authorization)
    trace = _start_trace("/ask/axis", x_trace_id, base_workspace_id)
    status = "ok"
    try:
        ask_service.overlay = overlay
        with db_session(user_id=user_id) as db:
            return ask_service.ask_axis(
                req,
                user_id=user_id,
                base_workspace_id=base_workspace_id,
                trace=trace,
                db=db,
            )
    except HTTPException:
        raise
    except Exception:
        status = "error"
        logger.exception(
            "trace_id=%s endpoint=/ask/axis status=error",
            trace.trace_id,
        )
        raise
    finally:
        default_metrics.record_trace(trace, status)

@app.post("/ask/stream")
def ask_stream(
    req: AskRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
    x_trace_id: str = Header(None),
):
    """Streaming version of /ask endpoint (SSE)."""
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    trace = _start_trace("/ask/stream", x_trace_id, workspace_id)

    def response_generator() -> Generator[str, None, None]:
        ask_service.ai_engine = ai_engine
        ask_service.audit_log = audit_log
        ask_service.feedback_store = feedback_store
        ask_service.default_cache = default_cache
        with db_session(user_id=user_id) as db:
            yield from ask_service.ask_stream(
                req,
                user_id=user_id,
                workspace_id=workspace_id,
                trace=trace,
                db=db,
                resolve_ask_context=_resolve_ask_context,
            )

    return StreamingResponse(response_generator(), media_type="text/event-stream")

@app.post("/feedback", response_model=FeedbackResponse)
def record_feedback(
    req: FeedbackRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    """Record retrieval feedback against an issued feedback token."""
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    snapshot = feedback_store.get_snapshot(req.feedback_token)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Unknown feedback token")
    if snapshot.workspace_id != workspace_id:
        raise HTTPException(status_code=403, detail="Feedback token belongs to another workspace")
    if snapshot.user_id != user_id:
        raise HTTPException(status_code=403, detail="Feedback token belongs to another user")

    event = FeedbackEvent(
        feedback_token=req.feedback_token,
        kind=req.kind,
        workspace_id=workspace_id,
        user_id=user_id,
        trace_id=snapshot.trace_id,
        details=req.details,
        client_timestamp=req.timestamp,
    )
    try:
        feedback_store.record_feedback(event)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    default_metrics.increment(
        "sidecar_feedback_events_total",
        labels={"kind": event.kind, "outcome": event.outcome, "workspace": workspace_id},
    )
    return {
        "status": "recorded",
        "feedback_token": req.feedback_token,
        "kind": event.kind,
        "outcome": event.outcome,
        "workspace_id": workspace_id,
        "trace_id": snapshot.trace_id,
    }


@app.post("/history/ask", response_model=HistoryAskRecordResponse)
def record_history_ask(
    req: HistoryAskRecordRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    """Persist a sanitized ask/request snapshot for local dialog history."""
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    if not _history_enabled():
        return {
            "status": "disabled",
            "conversation_id": req.conversation_id or "",
            "user_message_id": "",
            "assistant_message_id": "",
            "selected_request_id": req.request_id,
        }
    if not req.request_id.strip():
        raise HTTPException(status_code=400, detail="request_id is required")

    conversation_id = req.conversation_id
    if conversation_id:
        conversation = history_provider.get_conversation(conversation_id)
        if conversation:
            _history_conversation_for_scope(
                conversation_id,
                workspace_id=workspace_id,
                user_id=user_id,
            )
        else:
            title = req.prompt_summary or (
                f"Ask about {req.symbol}" if req.symbol else "Workspace ask"
            )
            conversation_id = history_provider.create_conversation(
                workspace_id=workspace_id,
                user_id=user_id,
                conversation_id=conversation_id,
                title=title,
                selected_request_id=req.request_id,
                metadata={
                    "source": "extension",
                    "symbol": req.symbol,
                    **req.metadata,
                },
            )
    else:
        title = req.prompt_summary or (f"Ask about {req.symbol}" if req.symbol else "Workspace ask")
        conversation_id = history_provider.create_conversation(
            workspace_id=workspace_id,
            user_id=user_id,
            title=title,
            selected_request_id=req.request_id,
            metadata={
                "source": "extension",
                "symbol": req.symbol,
                **req.metadata,
            },
        )

    prompt_hash = req.prompt_hash or hash_history_text(req.prompt_summary)
    answer_hash = req.answer_hash or hash_history_text(req.answer_summary)
    user_message_id = history_provider.append_message(
        conversation_id=conversation_id,
        role="user",
        request_id=req.request_id,
        content_summary=req.prompt_summary,
        content_hash=prompt_hash,
        symbol=req.symbol,
        trace_id=req.trace_id,
        metadata={
            "source": "extension",
            "kind": "prompt",
        },
    )
    assistant_message_id = history_provider.append_message(
        conversation_id=conversation_id,
        role="assistant",
        request_id=req.request_id,
        content_summary=req.answer_summary,
        content_hash=answer_hash,
        symbol=req.symbol,
        trace_id=req.trace_id,
        feedback_token=req.feedback_token,
        metadata={
            "source": "extension",
            "kind": "answer",
            "has_feedback_token": bool(req.feedback_token),
        },
    )

    history_provider.save_ask_snapshot(
        assistant_message_id,
        _history_snapshot(
            req,
            workspace_id=workspace_id,
            user_id=user_id,
            answer_summary=req.answer_summary,
        ),
    )
    if req.inspector_snapshot:
        history_provider.save_inspector_snapshot(
            assistant_message_id,
            {
                **req.inspector_snapshot,
                "request_id": req.request_id,
                "trace_id": req.trace_id,
                "feedback_token": req.feedback_token,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "symbol": req.symbol,
            },
        )
    if req.impact_snapshot:
        history_provider.save_impact_snapshot(
            assistant_message_id,
            {
                **req.impact_snapshot,
                "request_id": req.request_id,
                "trace_id": req.trace_id,
                "feedback_token": req.feedback_token,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "symbol": req.symbol,
            },
        )
    history_provider.set_selected_request(conversation_id, req.request_id)

    return {
        "status": "recorded",
        "conversation_id": conversation_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "selected_request_id": req.request_id,
    }


@app.get("/history/conversations", response_model=HistoryConversationsResponse)
def history_conversations(
    limit: int = 30,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    """List local history conversations for the current workspace and user."""
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    if not _history_enabled():
        return {"conversations": []}
    return {
        "conversations": history_provider.list_conversations(
            workspace_id=workspace_id,
            user_id=user_id,
            limit=limit,
        )
    }


@app.get("/history/conversations/{conversation_id}", response_model=HistoryConversationResponse)
def history_conversation(
    conversation_id: str,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    """Return a sanitized conversation bundle with messages and snapshots."""
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    _history_conversation_for_scope(conversation_id, workspace_id=workspace_id, user_id=user_id)
    bundle = history_provider.get_conversation_bundle(conversation_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Unknown history conversation")
    return bundle


@app.get(
    "/history/conversations/{conversation_id}/requests/{request_id}",
    response_model=HistoryRequestBundleResponse,
)
def history_request_bundle(
    conversation_id: str,
    request_id: str,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    """Return the snapshots for a selected request in a conversation."""
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    _history_conversation_for_scope(conversation_id, workspace_id=workspace_id, user_id=user_id)
    bundle = history_provider.get_request_bundle(conversation_id, request_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Unknown history request")
    return bundle


@app.get("/impact", response_model=ImpactResponse)
def impact(
    symbol: str,
    max_depth: int = Query(default=3, ge=IMPACT_DEPTH_MIN, le=IMPACT_DEPTH_MAX),
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    """Return downstream dependents affected by a change to the given symbol."""
    user_id = _resolve_request_user(x_user_id, authorization)
    base_workspace_id = _resolve_workspace(x_workspace, authorization)
    index_workspace_id = effective_index_workspace_id(base_workspace_id)
    with db_session(user_id=user_id) as db:
        from context_engine.axis.impact_surface import build_impact_surface

        symbol_uid = db.get_symbol_uid_by_name(symbol, workspace_id=index_workspace_id)
        if not symbol_uid:
            raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found")

        symbol_file = db.get_file_path_for_symbol(symbol_uid, workspace_id=index_workspace_id)
        surface = build_impact_surface(
            db=db,
            symbol_uid=symbol_uid,
            symbol_name=symbol,
            file_path=symbol_file,
            workspace_id=index_workspace_id,
            max_depth=max_depth,
        )
        affected_symbols = surface["affected_symbols"]
        affected_files = surface["affected_files"]

        return {
            "symbol": symbol,
            "symbol_uid": symbol_uid,
            "file_path": symbol_file,
            "affected_symbols": affected_symbols,
            "affected_files": affected_files,
            "affected_count": len(affected_symbols),
            "affected_file_count": len(affected_files),
            "max_depth": surface["max_depth"],
        }


@app.post("/auth/token", response_model=AuthTokenResponse)
def auth_token(
    user_id: str = None,  # type: ignore
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    """Generate a signed token scoped to a workspace.

    Bootstrap endpoint for the VS Code extension. When AUTH_REQUIRED=true an
    existing bearer token is required, and callers may only mint a replacement
    token for themselves. X-User-Id is never trusted for identity; workspace
    scope is taken from X-Workspace (or DEFAULT_WORKSPACE_ID).
    """
    workspace_id = _header_value(x_workspace) or DEFAULT_WORKSPACE_ID
    try:
        workspace_resolver.from_header(workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if AUTH_REQUIRED:
        requester = _resolve_request_user(x_user_id, authorization, require_auth=True)
        requested_user = _canonical_user_id(user_id) or requester
        if requested_user != requester:
            raise HTTPException(status_code=403, detail="Cannot issue token for another user")
        token_user = requester
    else:
        requested = _canonical_user_id(user_id)
        if TRUST_CLIENT_USER_HEADER:
            token_user = user_auth.identify_user(requested or _header_value(x_user_id))
        else:
            token_user = user_auth.identify_user(requested or None)

    token = user_auth.generate_token(token_user, workspace_id=workspace_id)
    logger.info("Token issued for user=%s workspace=%s", token_user, workspace_id)
    return {"token": token, "user_id": token_user, "expires_in_hours": 24}


@app.get("/auth/users", response_model=UsersResponse)
def list_users(x_user_id: str = Header(None), authorization: str = Header(None)):
    """List all active users (requires a valid bearer token)."""
    _resolve_request_user(x_user_id, authorization, require_auth=True)
    return {"users": user_auth.list_users()}


@app.get("/status/cloud", response_model=CloudStatusResponse)
def cloud_status(x_user_id: str = Header(None), authorization: str = Header(None)):
    """Get cloud (Aura) connection status."""
    user_id = _resolve_request_user(x_user_id, authorization)
    with db_session(user_id=user_id) as db:
        health = db.health_check()
        return {
            "cloud_enabled": True,
            "using_aura": db.is_cloud(),
            "using_fallback": db.is_fallback(),
            "health": health,
        }


@app.get("/audit/actions", response_model=AuditActionsResponse)
def audit_actions(
    user_id: str = None,  # type: ignore
    limit: int = 100,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
):
    """Get recent audit log entries."""
    requester = _resolve_request_user(x_user_id, authorization)
    requested_user = _canonical_user_id(user_id)
    if requested_user and requested_user != requester:
        raise HTTPException(status_code=403, detail="Cannot read audit actions for another user")
    actions = audit_log.get_recent_actions(user_id=requester, limit=limit)
    return {"actions": actions, "total": len(actions)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
