import logging
import os
from collections.abc import Generator
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from sidecar.ai.engine import AIEngine
from sidecar.api.sse import format_sse
from sidecar.auth import AuditLog, UserAuth
from sidecar.context.arbitrator import ContextArbitrator
from sidecar.context.overlay import InMemoryOverlay
from sidecar.context.types import RESOLVER_VERSION, PromptContext
from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.session import db_session
from sidecar.indexer.job_log import IndexJobLog
from sidecar.observability import (
    RequestTrace,
    default_metrics,
    estimate_cost_usd,
    estimate_text_tokens,
    new_trace_id,
)
from sidecar.search import UnifiedSearchResult, dedupe_and_rank
from sidecar.workspace import WorkspaceResolver

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_PREFERENCE = os.getenv("MODEL_PREFERENCE", "auto")  # "claude" | "ollama" | "auto"
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}

app = FastAPI(title="Surgical Context Sidecar")
overlay = InMemoryOverlay()
vector_db = LanceDBClient()
ai_engine = AIEngine(model_preference=MODEL_PREFERENCE)
user_auth = UserAuth()
audit_log = AuditLog()
workspace_resolver = WorkspaceResolver()


class IndexRequest(BaseModel):
    project_path: str


class IndexFileRequest(BaseModel):
    file_path: str


class IndexDocsRequest(BaseModel):
    docs_path: str


class AskRequest(BaseModel):
    symbol: str
    question: str = "What does this code do?"
    token_budget: int = 4000


class OverlayRequest(BaseModel):
    file_path: str
    content: str


class SearchRequest(BaseModel):
    query: str
    limit: int = 5


class UnifiedSearchRequest(SearchRequest):
    symbol: str | None = None
    include_graph: bool = True
    token_budget: int = 2000


class HealthResponse(BaseModel):
    status: str


class StatusPathResponse(BaseModel):
    status: str
    path: str


class IndexFileResponse(BaseModel):
    status: str
    file_path: str
    job_id: int
    workspace_id: str


class OverlayResponse(BaseModel):
    file_path: str
    symbols: list[str]


class ClearOverlayResponse(BaseModel):
    cleared: str


class SearchResponse(BaseModel):
    results: list[dict[str, Any]]


class UnifiedSearchResponse(BaseModel):
    trace_id: str
    workspace_id: str
    results: list[dict[str, Any]]
    total: int


class AskResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    symbol: str
    answer: str
    context: dict[str, Any]
    user: str
    cloud: bool
    workspace_id: str
    trace_id: str
    model_route: dict[str, Any]
    metrics: dict[str, Any]


class ImpactResponse(BaseModel):
    symbol: str
    symbol_uid: str
    file_path: str
    affected_symbols: list[dict[str, Any]]
    affected_files: list[str]
    affected_count: int
    affected_file_count: int
    max_depth: int


class AuthTokenResponse(BaseModel):
    token: str
    user_id: str
    expires_in_hours: int


class UsersResponse(BaseModel):
    users: list[dict[str, Any]]


class CloudStatusResponse(BaseModel):
    cloud_enabled: bool
    using_aura: bool
    using_fallback: bool
    health: dict[str, Any]


class AuditActionsResponse(BaseModel):
    actions: list[dict[str, Any]]
    total: int


def _header_value(value: Any) -> str | None:
    """Normalize FastAPI Header defaults when route functions are called directly in tests."""
    return value if isinstance(value, str) and value.strip() else None


def _resolve_request_user(
    x_user_id: Any = None,
    authorization: Any = None,
    *,
    require_auth: bool | None = None,
) -> str:
    """Resolve the request user and optionally require a valid bearer token."""
    require_auth = AUTH_REQUIRED if require_auth is None else require_auth
    authorization_value = _header_value(authorization)
    if authorization_value:
        scheme, _, token = authorization_value.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        if not user_auth.verify_token(token):
            raise HTTPException(status_code=401, detail="Invalid or expired bearer token")
        return user_auth.get_user_from_token(token)

    if require_auth:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    return user_auth.identify_user(_header_value(x_user_id))  # type: ignore


def _resolve_workspace(x_workspace: Any = None) -> str:
    try:
        return workspace_resolver.from_header(_header_value(x_workspace)).id
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _start_trace(endpoint: str, x_trace_id: Any = None, workspace_id: str = "") -> RequestTrace:
    return RequestTrace(
        trace_id=new_trace_id(_header_value(x_trace_id)),
        endpoint=endpoint,
        workspace_id=workspace_id,
    )


def _model_route(token_count: int, intent: str) -> dict[str, Any]:
    route = getattr(ai_engine, "route", None)
    if callable(route):
        return cast(dict[str, Any], route(token_count=token_count, intent=intent))
    return {
        "provider": getattr(ai_engine, "model_preference", MODEL_PREFERENCE),
        "model": getattr(ai_engine, "ollama_model", "unknown"),
        "preference": getattr(ai_engine, "model_preference", MODEL_PREFERENCE),
        "reason": "route_method_unavailable",
    }


def _last_model_route(default: dict[str, Any]) -> dict[str, Any]:
    last_route = getattr(ai_engine, "last_route", None)
    return cast(dict[str, Any], last_route) if isinstance(last_route, dict) else default


def _attach_trace_metadata(ctx: PromptContext, trace: RequestTrace) -> None:
    ctx.trace_id = trace.trace_id
    ctx.workspace_id = trace.workspace_id
    ctx.stage_timings_ms = dict(trace.stage_timings_ms)
    ctx.token_counts = dict(trace.token_counts)
    ctx.model_route = dict(trace.model_route)
    ctx.estimated_cost_usd = trace.estimated_cost_usd
    ctx.cost_basis = trace.cost_basis
    ctx.resolver_version = RESOLVER_VERSION


def _request_metrics(trace: RequestTrace) -> dict[str, Any]:
    return {
        "stage_timings_ms": dict(trace.stage_timings_ms),
        "token_counts": dict(trace.token_counts),
        "estimated_cost_usd": trace.estimated_cost_usd,
        "cost_basis": trace.cost_basis,
    }


@app.get("/health", response_model=HealthResponse)
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(default_metrics.render_prometheus(), media_type="text/plain")


@app.post("/index", response_model=StatusPathResponse)
def index(
    req: IndexRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace)
    if not os.path.isdir(req.project_path):
        raise HTTPException(status_code=400, detail=f"Path not found: {req.project_path}")

    from sidecar.indexer.code import run_indexing

    run_indexing(req.project_path, workspace_id=workspace_id)
    return {"status": "indexed", "path": req.project_path}


@app.post("/index/file", response_model=IndexFileResponse)
def index_file_endpoint(
    req: IndexFileRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace)
    if not os.path.isfile(req.file_path):
        raise HTTPException(status_code=400, detail=f"File not found: {req.file_path}")

    from sidecar.indexer.anchor import resolve_pending_anchors
    from sidecar.indexer.code import hash_file, index_file
    from sidecar.parser.extractor import SymbolExtractor

    job_log = IndexJobLog()
    job_id = 0
    file_hash = hash_file(req.file_path)
    try:
        with job_log.track_file_job(req.file_path, file_hash=file_hash) as job_id:
            with db_session() as db:
                db.delete_symbols_for_file(req.file_path, workspace_id=workspace_id)
                index_file(
                    req.file_path,
                    db,
                    vector_db,
                    SymbolExtractor(),
                    workspace_id=workspace_id,
                )
                resolve_pending_anchors(db, vector_db, workspace_id=workspace_id)
    except Exception as exc:
        job = job_log.get_job(job_id) if job_id else None
        detail = {
            "error": str(exc),
            "job_id": job_id,
            "job_status": job["status"] if job else "unknown",
        }
        raise HTTPException(status_code=500, detail=detail) from exc
    return {
        "status": "indexed",
        "file_path": req.file_path,
        "job_id": job_id,
        "workspace_id": workspace_id,
    }


@app.post("/index/docs", response_model=StatusPathResponse)
def index_docs_endpoint(
    req: IndexDocsRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace)
    if not os.path.isdir(req.docs_path):
        raise HTTPException(status_code=400, detail=f"Path not found: {req.docs_path}")

    from sidecar.indexer.docs import index_docs

    index_docs(req.docs_path, workspace_id=workspace_id)
    return {"status": "indexed", "path": req.docs_path}


@app.post("/search", response_model=SearchResponse)
def search(
    req: SearchRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    _resolve_request_user(x_user_id, authorization)
    _resolve_workspace(x_workspace)
    return {"results": vector_db.search(req.query, req.limit)}


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
    workspace_id = _resolve_workspace(x_workspace)
    trace = _start_trace("/search/unified", x_trace_id, workspace_id)
    status = "ok"
    results: list[UnifiedSearchResult] = []
    try:
        with trace.stage("vector_docs"):
            docs = vector_db.search(req.query, req.limit)
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

        search_symbols = getattr(vector_db, "search_symbols", None)
        if callable(search_symbols):
            with trace.stage("vector_symbols"):
                symbols = search_symbols(req.query, req.limit, threshold=1.0)
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

        if req.include_graph and req.symbol:
            with trace.stage("graph_neighbors"):
                with db_session(user_id=user_id) as db:
                    arb = ContextArbitrator(db, overlay, vector_db, workspace_id=workspace_id)
                    ctx = arb.get_context_for_symbol(
                        req.symbol,
                        question=req.query,
                        token_budget=req.token_budget,
                    )
            if not isinstance(ctx, str):
                for symbol, provenance in [
                    (ctx.primary_source, "graph:primary"),
                    *[(dep, "graph:neighbor") for dep in ctx.graph_context],
                ]:
                    results.append(
                        {
                            "type": "symbol",
                            "title": symbol.symbol,
                            "file_path": symbol.file_path,
                            "content": symbol.code,
                            "score": symbol.relevance_score,
                            "scores": {"relevance": symbol.relevance_score},
                            "provenance": [provenance],
                            "metadata": {
                                "relation": symbol.relation,
                                "direction": symbol.direction,
                                "depth": symbol.depth,
                                "is_dirty": symbol.is_dirty,
                            },
                        }
                    )

        ranked = dedupe_and_rank(results, req.limit)
        trace.token_counts["query"] = estimate_text_tokens(req.query)
        return {
            "trace_id": trace.trace_id,
            "workspace_id": workspace_id,
            "results": ranked,
            "total": len(ranked),
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
    workspace_id = _resolve_workspace(x_workspace)
    overlay.update(req.file_path, req.content, workspace_id=workspace_id, user_id=user_id)
    symbols = overlay.get_symbols(req.file_path, workspace_id=workspace_id, user_id=user_id)
    return {"file_path": req.file_path, "symbols": list(symbols.keys())}


@app.delete("/overlay", response_model=ClearOverlayResponse)
def clear_overlay(
    file_path: str,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace)
    overlay.clear(file_path, workspace_id=workspace_id, user_id=user_id)
    return {"cleared": file_path}


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
    workspace_id = _resolve_workspace(x_workspace)
    trace = _start_trace("/ask", x_trace_id, workspace_id)
    status = "ok"
    try:
        with db_session(user_id=user_id) as db:
            with trace.stage("context"):
                arb = ContextArbitrator(db, overlay, vector_db, workspace_id=workspace_id)
                ctx = arb.get_context_for_symbol(
                    req.symbol, question=req.question, token_budget=req.token_budget
                )
            if isinstance(ctx, str):
                status = "not_found"
                audit_log.log_error(user_id, "query", ctx)
                logger.info("trace_id=%s endpoint=/ask status=not_found", trace.trace_id)
                raise HTTPException(status_code=404, detail=ctx)

            with trace.stage("prompt"):
                system_prompt = (
                    "You are a Surgical Code Assistant. Use ONLY the provided context.\n\n"
                    f"{ctx.to_system_prompt()}"
                )
                context_tokens = ctx.token_count()
                trace.token_counts = {
                    "context": context_tokens,
                    "user": estimate_text_tokens(req.question),
                }
                trace.model_route = _model_route(context_tokens, ctx.intent)

            with trace.stage("llm"):
                answer = ai_engine.chat(
                    system_prompt=system_prompt,
                    user_message=req.question,
                    token_count=context_tokens,
                    intent=ctx.intent,
                )
            trace.model_route = _last_model_route(trace.model_route)

            output_tokens = estimate_text_tokens(answer)
            trace.token_counts["output_estimate"] = output_tokens
            trace.estimated_cost_usd, trace.cost_basis = estimate_cost_usd(
                trace.model_route,
                input_tokens=context_tokens + trace.token_counts["user"],
                output_tokens=output_tokens,
            )

            with trace.stage("audit"):
                audit_log.log_query(user_id, req.symbol, req.question, ctx.intent, ctx.mode)

            _attach_trace_metadata(ctx, trace)
            logger.info("trace_id=%s endpoint=/ask status=ok", trace.trace_id)
            return {
                "symbol": req.symbol,
                "answer": answer,
                "context": ctx.to_dict(),
                "user": user_id,
                "cloud": db.is_cloud(),
                "workspace_id": workspace_id,
                "trace_id": trace.trace_id,
                "model_route": trace.model_route,
                "metrics": _request_metrics(trace),
            }
    except HTTPException:
        raise
    except Exception:
        status = "error"
        logger.exception("trace_id=%s endpoint=/ask status=error", trace.trace_id)
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
    workspace_id = _resolve_workspace(x_workspace)
    trace = _start_trace("/ask/stream", x_trace_id, workspace_id)

    def response_generator() -> Generator[str, None, None]:
        status = "ok"
        answer_parts: list[str] = []
        yield format_sse("trace", {"type": "trace", "trace_id": trace.trace_id})
        try:
            with db_session(user_id=user_id) as db:
                with trace.stage("context"):
                    arb = ContextArbitrator(db, overlay, vector_db, workspace_id=workspace_id)
                    ctx = arb.get_context_for_symbol(
                        req.symbol, question=req.question, token_budget=req.token_budget
                    )
                if isinstance(ctx, str):
                    status = "not_found"
                    audit_log.log_error(user_id, "query", ctx)
                    yield format_sse(
                        "error",
                        {"type": "error", "error": ctx, "trace_id": trace.trace_id},
                    )
                    return

                with trace.stage("prompt"):
                    system_prompt = (
                        "You are a Surgical Code Assistant. Use ONLY the provided context.\n\n"
                        f"{ctx.to_system_prompt()}"
                    )
                    context_tokens = ctx.token_count()
                    trace.token_counts = {
                        "context": context_tokens,
                        "user": estimate_text_tokens(req.question),
                    }
                    trace.model_route = _model_route(context_tokens, ctx.intent)

                with trace.stage("llm"):
                    for chunk in ai_engine.stream_chat(
                        system_prompt=system_prompt,
                        user_message=req.question,
                        token_count=context_tokens,
                        intent=ctx.intent,
                    ):
                        answer_parts.append(chunk)
                        yield format_sse("chunk", {"type": "chunk", "content": chunk})
                trace.model_route = _last_model_route(trace.model_route)

                output_tokens = estimate_text_tokens("".join(answer_parts))
                trace.token_counts["output_estimate"] = output_tokens
                trace.estimated_cost_usd, trace.cost_basis = estimate_cost_usd(
                    trace.model_route,
                    input_tokens=context_tokens + trace.token_counts["user"],
                    output_tokens=output_tokens,
                )

                with trace.stage("audit"):
                    audit_log.log_query(user_id, req.symbol, req.question, ctx.intent, ctx.mode)

                _attach_trace_metadata(ctx, trace)
                yield format_sse(
                    "context",
                    {
                        "type": "context",
                        "trace_id": trace.trace_id,
                        "context": ctx.to_dict(),
                        "metrics": _request_metrics(trace),
                    },
                )
                yield format_sse("done", {"type": "done", "trace_id": trace.trace_id})
        except Exception as exc:
            status = "error"
            logger.exception("trace_id=%s endpoint=/ask/stream status=error", trace.trace_id)
            yield format_sse(
                "error",
                {"type": "error", "error": str(exc), "trace_id": trace.trace_id},
            )
        finally:
            default_metrics.record_trace(trace, status)

    return StreamingResponse(response_generator(), media_type="text/event-stream")


@app.get("/impact", response_model=ImpactResponse)
def impact(
    symbol: str,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    """Return downstream dependents affected by a change to the given symbol."""
    _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace)
    with db_session() as db:
        from sidecar.indexer.affects import AFFECTSIndexer

        # Look up symbol UID by name
        query = """
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {name: $name})
        RETURN s.uid AS uid LIMIT 1
        """
        with db.driver.session() as session:
            result = session.run(query, name=symbol, workspace_id=workspace_id).single()

        if not result:
            raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found")

        symbol_uid = result["uid"]

        # Get affected symbols
        indexer = AFFECTSIndexer(db)
        affected_symbols = indexer.get_affected_symbols(symbol_uid, workspace_id=workspace_id)

        # Get file containing the symbol
        query = """
        MATCH (s:Symbol {uid: $uid})
        OPTIONAL MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s)
        RETURN coalesce(f.path, '<unknown>') AS file_path
        """
        with db.driver.session() as session:
            result = session.run(query, uid=symbol_uid, workspace_id=workspace_id).single()

        symbol_file = result["file_path"] if result else "<unknown>"

        # Get affected files
        if symbol_file != "<unknown>":
            affected_files = indexer.get_affected_files(symbol_file, workspace_id=workspace_id)
        else:
            affected_files = []

        return {
            "symbol": symbol,
            "symbol_uid": symbol_uid,
            "file_path": symbol_file,
            "affected_symbols": affected_symbols,
            "affected_files": affected_files,
            "affected_count": len(affected_symbols),
            "affected_file_count": len(affected_files),
            "max_depth": AFFECTSIndexer.MAX_AFFECTS_DEPTH,
        }


@app.post("/auth/token", response_model=AuthTokenResponse)
def auth_token(user_id: str = None):  # type: ignore
    """Generate JWT token for multi-user mode."""
    user_id = user_auth.identify_user(user_id)
    token = user_auth.generate_token(user_id)
    logger.info(f"✅ Token issued for user: {user_id}")
    return {"token": token, "user_id": user_id, "expires_in_hours": 24}


@app.get("/auth/users", response_model=UsersResponse)
def list_users(x_user_id: str = Header(None), authorization: str = Header(None)):
    """List all active users."""
    _resolve_request_user(x_user_id, authorization)
    return {"users": user_auth.list_users()}


@app.get("/status/cloud", response_model=CloudStatusResponse)
def cloud_status(x_user_id: str = Header(None), authorization: str = Header(None)):
    """Get cloud (Aura) connection status."""
    _resolve_request_user(x_user_id, authorization)
    with db_session() as db:
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
    _resolve_request_user(x_user_id, authorization)
    actions = audit_log.get_recent_actions(user_id=user_id, limit=limit)
    return {"actions": actions, "total": len(actions)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
