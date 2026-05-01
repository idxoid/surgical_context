import hashlib
import logging
import os
from collections.abc import Generator
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from sidecar.ai.engine import AIEngine
from sidecar.api.sse import format_sse
from sidecar.auth import AuditLog, UserAuth
from sidecar.cache.layered import default_cache
from sidecar.context.arbitrator import ContextArbitrator
from sidecar.context.doc_resolver import DocResolver
from sidecar.context.intent_classifier import IntentClassifier
from sidecar.context.overlay import InMemoryOverlay
from sidecar.context.types import RESOLVER_VERSION, DocChunk, PromptContext, SymbolContext
from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.session import db_session
from sidecar.feedback import FeedbackEvent, FeedbackStore, RetrievalSnapshot
from sidecar.history import build_history_provider, hash_history_text, parse_retention_days
from sidecar.indexer.job_log import IndexJobLog
from sidecar.indexer.queue import EnqueueResult, IndexBatchQueue, IndexWorkItem
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
INDEX_QUEUE_MAX_PENDING = int(os.getenv("INDEX_QUEUE_MAX_PENDING", "500"))
INDEX_QUEUE_DEBOUNCE_MS = int(os.getenv("INDEX_QUEUE_DEBOUNCE_MS", "500"))
INDEX_QUEUE_BATCH_SIZE = int(os.getenv("INDEX_QUEUE_BATCH_SIZE", "50"))

app = FastAPI(title="Surgical Context Sidecar")
overlay = InMemoryOverlay()
vector_db = LanceDBClient()
ai_engine = AIEngine(model_preference=MODEL_PREFERENCE)
user_auth = UserAuth()
audit_log = AuditLog()
workspace_resolver = WorkspaceResolver()
feedback_store = FeedbackStore()
history_provider = build_history_provider(
    mode=os.getenv("HISTORY_MODE", "local"),
    db_path=os.getenv("HISTORY_DB_PATH", "./data/history/surgical_context.sqlite3"),
    retention_days=parse_retention_days(os.getenv("HISTORY_RETENTION_DAYS", "")),
)


class IndexRequest(BaseModel):
    project_path: str
    queue: bool = True


class IndexFileRequest(BaseModel):
    file_path: str
    queue: bool = True


class IndexFilesRequest(BaseModel):
    file_paths: list[str]
    queue: bool = True


class IndexDocsRequest(BaseModel):
    docs_path: str


class AskRequest(BaseModel):
    symbol: str | None = None
    question: str = "What does this code do?"
    token_budget: int = 4000
    file_path: str | None = None


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
    queued: int = 0
    coalesced: int = 0
    rejected: int = 0
    queue_depth: int = 0


class IndexFileResponse(BaseModel):
    status: str
    file_path: str
    job_id: int = 0
    workspace_id: str
    queue_depth: int = 0
    reason: str = ""


class IndexFilesResponse(BaseModel):
    status: str
    workspace_id: str
    results: list[dict[str, Any]]
    queued: int
    coalesced: int
    rejected: int
    queue_depth: int


class IndexQueueStatusResponse(BaseModel):
    status: str
    queue: dict[str, Any]


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
    feedback_token: str
    model_route: dict[str, Any]
    metrics: dict[str, Any]


class FeedbackRequest(BaseModel):
    feedback_token: str
    kind: str
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = ""


class FeedbackResponse(BaseModel):
    status: str
    feedback_token: str
    kind: str
    outcome: str
    workspace_id: str
    trace_id: str


class HistoryAskRecordRequest(BaseModel):
    conversation_id: str | None = None
    request_id: str
    prompt_summary: str = ""
    prompt_hash: str = ""
    answer_summary: str = ""
    answer_hash: str = ""
    symbol: str = ""
    trace_id: str = ""
    feedback_token: str = ""
    ask_snapshot: dict[str, Any] = Field(default_factory=dict)
    inspector_snapshot: dict[str, Any] = Field(default_factory=dict)
    impact_snapshot: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HistoryAskRecordResponse(BaseModel):
    status: str
    conversation_id: str
    user_message_id: str
    assistant_message_id: str
    selected_request_id: str


class HistoryConversationsResponse(BaseModel):
    conversations: list[dict[str, Any]]


class HistoryConversationResponse(BaseModel):
    conversation: dict[str, Any]
    messages: list[dict[str, Any]]


class HistoryRequestBundleResponse(BaseModel):
    message: dict[str, Any]
    ask_snapshot: dict[str, Any] | None
    inspector_snapshot: dict[str, Any] | None
    impact_snapshot: dict[str, Any] | None


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
        "latency_slo": trace.latency_slo(),
        "token_counts": dict(trace.token_counts),
        "estimated_cost_usd": trace.estimated_cost_usd,
        "cost_basis": trace.cost_basis,
    }


def _degraded_llm_answer(exc: Exception) -> str:
    return (
        "The language model is currently unreachable, so this is a degraded "
        "context-only response. The assembled context is still included below "
        f"for inspection. Error: {exc}"
    )


def _mark_degraded_route(route: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        **route,
        "degraded": True,
        "reason": "llm_unreachable_context_only",
        "error": str(exc),
    }


def _candidate_record(symbol: Any) -> dict[str, Any]:
    return {
        "symbol": getattr(symbol, "symbol", ""),
        "file_path": getattr(symbol, "file_path", ""),
        "relation": getattr(symbol, "relation", ""),
        "direction": getattr(symbol, "direction", ""),
        "depth": getattr(symbol, "depth", 0),
        "relevance_score": getattr(symbol, "relevance_score", 0.0),
        "is_dirty": getattr(symbol, "is_dirty", False),
    }


def _doc_record(doc: Any) -> dict[str, Any]:
    return {
        "chunk_id": getattr(doc, "chunk_id", ""),
        "source_file": getattr(doc, "source_file", ""),
        "score": getattr(doc, "score", None),
        "provenance": getattr(doc, "provenance", []),
        "anchor_type": getattr(doc, "anchor_type", ""),
        "anchor_confidence": getattr(doc, "anchor_confidence", 0.0),
        "primary_bias": getattr(doc, "primary_bias", 0.0),
    }


def _record_retrieval_snapshot(
    *,
    feedback_token: str,
    user_id: str,
    workspace_id: str,
    symbol: str,
    question: str,
    ctx: Any,
    trace: RequestTrace,
) -> None:
    selected = [_candidate_record(ctx.primary_source)]
    selected.extend(_candidate_record(candidate) for candidate in getattr(ctx, "graph_context", []))
    documentation = [_doc_record(doc) for doc in getattr(ctx, "documentation", [])]
    snapshot = RetrievalSnapshot(
        feedback_token=feedback_token,
        workspace_id=workspace_id,
        user_id=user_id,
        trace_id=trace.trace_id,
        symbol=symbol,
        intent=str(getattr(ctx, "intent", "")),
        mode=str(getattr(ctx, "mode", "")),
        question_hash=hashlib.sha256(question.encode()).hexdigest(),
        question_tokens=trace.token_counts.get("user", estimate_text_tokens(question)),
        resolver_version=getattr(ctx, "resolver_version", RESOLVER_VERSION),
        selected_candidates=selected,
        documentation=documentation,
        context_metadata={
            "budget": getattr(ctx, "budget", {}),
            "tier_tokens": getattr(ctx, "tier_tokens", {}),
            "token_counts": dict(trace.token_counts),
            "model_route": dict(trace.model_route),
            "estimated_cost_usd": trace.estimated_cost_usd,
            "cost_basis": trace.cost_basis,
        },
    )
    feedback_store.record_snapshot(snapshot)
    default_metrics.increment(
        "sidecar_feedback_snapshots_total",
        labels={"workspace": workspace_id},
    )


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
        raise HTTPException(status_code=403, detail="History conversation belongs to another workspace")
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


def _read_file_context(
    file_path: str,
    *,
    workspace_id: str,
    user_id: str,
    token_budget: int,
) -> tuple[str, bool]:
    if overlay.has(file_path, workspace_id=workspace_id, user_id=user_id):
        symbols = overlay.get_symbols(file_path, workspace_id=workspace_id, user_id=user_id)
        if symbols:
            start = min(line_range[0] for line_range in symbols.values())
            end = max(line_range[1] for line_range in symbols.values())
            code = overlay.read_lines(
                file_path,
                start,
                end,
                workspace_id=workspace_id,
                user_id=user_id,
            )
        else:
            code = overlay.read_lines(
                file_path,
                1,
                500,
                workspace_id=workspace_id,
                user_id=user_id,
            )
        return _trim_text_to_budget(code, token_budget), True

    try:
        with open(file_path, encoding="utf-8") as file:
            code = file.read()
    except (OSError, FileNotFoundError):
        return "", False
    return _trim_text_to_budget(code, token_budget), False


def _trim_text_to_budget(text: str, token_budget: int) -> str:
    if not text:
        return ""
    max_tokens = max(400, int(token_budget * 0.75))
    if estimate_text_tokens(text) <= max_tokens:
        return text

    # Cheap deterministic trimming. Keep the top of the file because definitions/imports
    # usually explain module shape better than a middle slice.
    lines = text.splitlines()
    kept: list[str] = []
    for line in lines:
        candidate = "\n".join([*kept, line])
        if estimate_text_tokens(candidate) > max_tokens:
            break
        kept.append(line)
    return "\n".join(kept)


def _context_from_file(
    *,
    file_path: str,
    question: str,
    token_budget: int,
    workspace_id: str,
    user_id: str,
) -> PromptContext | None:
    code, is_dirty = _read_file_context(
        file_path,
        workspace_id=workspace_id,
        user_id=user_id,
        token_budget=token_budget,
    )
    if not code:
        return None

    intent = IntentClassifier.classify_intent(question)
    ctx = PromptContext(
        primary_source=SymbolContext(
            symbol=os.path.basename(file_path) or file_path,
            file_path=file_path,
            relation="file",
            relevance_score=1.0,
            is_dirty=is_dirty,
            code=code,
            provenance=["file"],
        ),
        graph_context=[],
        documentation=_search_docs(f"{file_path} {question}", limit=3),
        mode="file",
        intent=intent.value,
        tier_tokens={"code": estimate_text_tokens(code)},
    )
    ctx.tier_tokens.update(_doc_tier_tokens(ctx.documentation))
    return ctx


def _context_from_workspace(question: str, token_budget: int) -> PromptContext | None:
    docs = _search_docs(question, limit=5)
    symbols = _search_symbols(question, limit=5)
    if not docs and not symbols:
        return None

    intent = IntentClassifier.classify_intent(question)
    ctx = PromptContext(
        primary_source=SymbolContext(
            symbol="workspace",
            file_path="<workspace>",
            relation="workspace",
            relevance_score=1.0,
            provenance=["workspace_search"],
        ),
        graph_context=symbols,
        documentation=docs,
        mode="workspace",
        intent=intent.value,
        tier_tokens={"cross_refs": sum(estimate_text_tokens(sym.symbol) for sym in symbols)},
    )
    ctx.tier_tokens.update(_doc_tier_tokens(docs))
    ctx.budget["token_budget"] = token_budget
    return ctx


def _context_from_direct(question: str, token_budget: int) -> PromptContext:
    intent = IntentClassifier.classify_intent(question)
    return PromptContext(
        primary_source=SymbolContext(
            symbol="direct",
            file_path="<none>",
            relation="direct",
            relevance_score=0.0,
            provenance=["direct_llm"],
        ),
        graph_context=[],
        documentation=[],
        mode="direct",
        intent=intent.value,
        tier_tokens={},
        budget={"token_budget": token_budget},
    )


def _search_docs(query: str, limit: int) -> list[DocChunk]:
    try:
        return DocResolver(vector_db).search(query, limit=limit)
    except Exception:
        return []


def _search_symbols(query: str, limit: int) -> list[SymbolContext]:
    search_symbols = getattr(vector_db, "search_symbols", None)
    if not callable(search_symbols):
        return []
    try:
        raw_symbols = search_symbols(query, limit=limit, threshold=1.0)
    except Exception:
        return []
    return [
        SymbolContext(
            symbol=str(symbol.get("name", "")),
            file_path=str(symbol.get("file_path", "")),
            relation="workspace_match",
            relevance_score=float(symbol.get("score") or 0.0),
            provenance=["vector:symbols"],
        )
        for symbol in raw_symbols
    ]


def _doc_tier_tokens(docs: list[DocChunk]) -> dict[str, int]:
    if not docs:
        return {}
    return {"docs": sum(estimate_text_tokens(doc.content) for doc in docs)}


def _resolve_ask_context(
    *,
    req: AskRequest,
    user_id: str,
    workspace_id: str,
    db: Any,
) -> PromptContext:
    symbol_error = ""
    if req.symbol:
        arb = ContextArbitrator(db, overlay, vector_db, workspace_id=workspace_id)
        ctx = arb.get_context_for_symbol(
            req.symbol,
            question=req.question,
            token_budget=req.token_budget,
        )
        if not isinstance(ctx, str):
            _context_budget(ctx)["ask_level"] = "symbol"
            return ctx
        symbol_error = ctx

    if req.file_path:
        file_ctx = _context_from_file(
            file_path=req.file_path,
            question=req.question,
            token_budget=req.token_budget,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        if file_ctx:
            _mark_ask_fallback(file_ctx, req, "file", symbol_error)
            return file_ctx

    workspace_ctx = _context_from_workspace(req.question, req.token_budget)
    if workspace_ctx:
        _mark_ask_fallback(workspace_ctx, req, "workspace", symbol_error)
        return workspace_ctx

    direct_ctx = _context_from_direct(req.question, req.token_budget)
    _mark_ask_fallback(direct_ctx, req, "direct_llm", symbol_error)
    return direct_ctx


def _context_budget(ctx: Any) -> dict[str, Any]:
    budget = getattr(ctx, "budget", None)
    if not isinstance(budget, dict):
        budget = {}
        ctx.budget = budget
    return budget


def _mark_ask_fallback(
    ctx: PromptContext,
    req: AskRequest,
    ask_level: str,
    symbol_error: str = "",
) -> None:
    budget = _context_budget(ctx)
    budget["ask_level"] = ask_level
    budget["fallback_ladder"] = ["symbol", "file", "workspace", "direct_llm"]
    if req.symbol:
        display_level = "direct LLM" if ask_level == "direct_llm" else ask_level
        budget["missing_symbol"] = req.symbol
        budget["fallback_from"] = "symbol"
        budget["fallback_reason"] = _fallback_reason(symbol_error)
        budget["warnings"] = _append_context_warning(
            budget.get("warnings"),
            {
                "code": budget["fallback_reason"],
                "severity": "warning",
                "message": (
                    f"Symbol '{req.symbol}' was not found; "
                    f"using {display_level} context."
                ),
            },
        )


def _fallback_reason(symbol_error: str) -> str:
    if "not found" in symbol_error.lower():
        return "symbol_not_found"
    if symbol_error:
        return "symbol_context_unavailable"
    return "symbol_not_provided"


def _append_context_warning(current: Any, warning: dict[str, str]) -> list[dict[str, str]]:
    warnings = [item for item in current if isinstance(item, dict)] if isinstance(current, list) else []
    if not any(item.get("code") == warning["code"] for item in warnings):
        warnings.append(warning)
    return warnings


def _system_prompt_for_context(ctx: PromptContext) -> str:
    if ctx.mode == "direct":
        return (
            "You are a Surgical Code Assistant. No codebase context was retrieved for this "
            "question. Answer from general engineering knowledge, and clearly state when a "
            "claim would need codebase verification."
        )
    if ctx.mode in {"file", "workspace"}:
        return (
            "You are a Surgical Code Assistant. Use the retrieved context when it is relevant. "
            "If the context is incomplete, keep the answer practical and mark assumptions.\n\n"
            f"{ctx.to_system_prompt()}"
        )
    return (
        "You are a Surgical Code Assistant. Use ONLY the provided context.\n\n"
        f"{ctx.to_system_prompt()}"
    )


def _index_file_now(file_path: str, workspace_id: str, user_id: str) -> int:
    from sidecar.indexer.anchor import resolve_pending_anchors
    from sidecar.indexer.code import hash_file, index_file
    from sidecar.parser.extractor import SymbolExtractor

    job_log = IndexJobLog()
    file_hash = hash_file(file_path)
    with job_log.track_file_job(file_path, file_hash=file_hash) as tracked_job_id:
        with db_session(user_id=user_id) as db:
            index_file(
                file_path,
                db,
                vector_db,
                SymbolExtractor(),
                workspace_id=workspace_id,
            )
            resolve_pending_anchors(db, vector_db, workspace_id=workspace_id)
    return tracked_job_id


def _enqueue_index_file(file_path: str, workspace_id: str, user_id: str) -> EnqueueResult:
    result = index_queue.enqueue_file(file_path, workspace_id=workspace_id, user_id=user_id)
    default_metrics.increment(
        "sidecar_index_queue_events_total",
        labels={"status": result.status, "workspace": workspace_id},
    )
    return result


def _enqueue_index_files(
    file_paths: list[str],
    workspace_id: str,
    user_id: str,
) -> list[EnqueueResult]:
    return [_enqueue_index_file(path, workspace_id, user_id) for path in file_paths]


def _summarize_enqueue_results(results: list[EnqueueResult]) -> dict[str, int]:
    queued = sum(1 for result in results if result.status == "queued")
    coalesced = sum(1 for result in results if result.status == "coalesced")
    rejected = sum(1 for result in results if not result.accepted)
    queue_depth = max(
        (result.queue_depth for result in results), default=index_queue.snapshot()["pending"]
    )
    return {
        "queued": queued,
        "coalesced": coalesced,
        "rejected": rejected,
        "queue_depth": queue_depth,
    }


def _process_index_batch(items: list[IndexWorkItem]) -> None:
    """Process a coalesced file batch and resolve doc anchors once per workspace."""
    if not items:
        return

    from collections import defaultdict

    from sidecar.indexer.anchor import resolve_pending_anchors
    from sidecar.indexer.code import hash_file, index_file, is_indexable_file
    from sidecar.parser.extractor import SymbolExtractor

    grouped: dict[tuple[str, str], list[IndexWorkItem]] = defaultdict(list)
    for item in items:
        grouped[(item.user_id, item.workspace_id)].append(item)

    job_log = IndexJobLog()
    extractor = SymbolExtractor()
    for (user_id, workspace_id), group in grouped.items():
        existing_paths = [item.file_path for item in group if os.path.isfile(item.file_path)]
        missing_paths = [item.file_path for item in group if not os.path.isfile(item.file_path)]
        unsupported_paths = [path for path in existing_paths if not is_indexable_file(path)]
        indexable_paths = [path for path in existing_paths if is_indexable_file(path)]
        for path in missing_paths:
            logger.warning("Skipping queued index for missing file: %s", path)
            default_metrics.increment(
                "sidecar_index_queue_skipped_total",
                labels={"reason": "missing_file", "workspace": workspace_id},
            )
        for path in unsupported_paths:
            logger.info("Skipping queued index for unsupported file type: %s", path)
            default_metrics.increment(
                "sidecar_index_queue_skipped_total",
                labels={"reason": "unsupported_extension", "workspace": workspace_id},
            )
        if not indexable_paths:
            continue

        current_hashes = {path: hash_file(path) for path in indexable_paths}
        completed = 0
        with db_session(user_id=user_id) as db:
            get_file_hashes = getattr(db, "get_file_hashes", None)
            stored_hashes = (
                get_file_hashes(indexable_paths, workspace_id=workspace_id)
                if callable(get_file_hashes)
                else {}
            )
            for path in indexable_paths:
                file_hash = current_hashes[path]
                if stored_hashes.get(path) == file_hash:
                    default_metrics.increment(
                        "sidecar_index_queue_skipped_total",
                        labels={"reason": "unchanged_hash", "workspace": workspace_id},
                    )
                    continue
                try:
                    with job_log.track_file_job(path, file_hash=file_hash):
                        index_file(path, db, vector_db, extractor, workspace_id=workspace_id)
                        completed += 1
                except Exception:
                    logger.exception("Queued indexing failed for %s", path)
                    default_metrics.increment(
                        "sidecar_index_queue_failures_total",
                        labels={"workspace": workspace_id},
                    )
            if completed:
                resolve_pending_anchors(db, vector_db, workspace_id=workspace_id)
                default_metrics.increment(
                    "sidecar_index_queue_completed_files_total",
                    value=completed,
                    labels={"workspace": workspace_id},
                )


index_queue = IndexBatchQueue(
    _process_index_batch,
    max_pending=INDEX_QUEUE_MAX_PENDING,
    debounce_ms=INDEX_QUEUE_DEBOUNCE_MS,
    batch_size=INDEX_QUEUE_BATCH_SIZE,
)


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
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace)
    if not os.path.isdir(req.project_path):
        raise HTTPException(status_code=400, detail=f"Path not found: {req.project_path}")

    if req.queue:
        from sidecar.indexer.code import _collect_files

        files = _collect_files(req.project_path)
        results = _enqueue_index_files(files, workspace_id, user_id)
        summary = _summarize_enqueue_results(results)
        status = "queued"
        if not files:
            status = "no_files"
        elif summary["rejected"]:
            status = "partial_queued"
        return {"status": status, "path": req.project_path, **summary}

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
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace)
    if not os.path.isfile(req.file_path):
        raise HTTPException(status_code=400, detail=f"File not found: {req.file_path}")

    if req.queue:
        result = _enqueue_index_file(req.file_path, workspace_id, user_id)
        if not result.accepted:
            raise HTTPException(status_code=429, detail=result.to_dict())
        return {
            "status": result.status,
            "file_path": req.file_path,
            "job_id": 0,
            "workspace_id": workspace_id,
            "queue_depth": result.queue_depth,
            "reason": result.reason,
        }

    job_id = 0
    try:
        job_id = _index_file_now(req.file_path, workspace_id, user_id)
    except Exception as exc:
        job_log = IndexJobLog()
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


@app.post("/index/files", response_model=IndexFilesResponse)
def index_files_endpoint(
    req: IndexFilesRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace)
    missing = [
        EnqueueResult(
            accepted=False,
            status="skipped",
            file_path=file_path,
            workspace_id=workspace_id,
            queue_depth=index_queue.snapshot()["pending"],
            reason="file_not_found",
        )
        for file_path in req.file_paths
        if not os.path.isfile(file_path)
    ]
    valid_paths = [file_path for file_path in req.file_paths if os.path.isfile(file_path)]

    if req.queue:
        results = [*missing, *_enqueue_index_files(valid_paths, workspace_id, user_id)]
        summary = _summarize_enqueue_results(results)
        status = "queued" if not summary["rejected"] else "partial_queued"
        return {
            "status": status,
            "workspace_id": workspace_id,
            "results": [result.to_dict() for result in results],
            **summary,
        }

    sync_results = missing
    for file_path in valid_paths:
        try:
            job_id = _index_file_now(file_path, workspace_id, user_id)
            sync_results.append(
                EnqueueResult(
                    accepted=True,
                    status="indexed",
                    file_path=file_path,
                    workspace_id=workspace_id,
                    queue_depth=index_queue.snapshot()["pending"],
                    generation=job_id,
                )
            )
        except Exception as exc:
            sync_results.append(
                EnqueueResult(
                    accepted=False,
                    status="failed",
                    file_path=file_path,
                    workspace_id=workspace_id,
                    queue_depth=index_queue.snapshot()["pending"],
                    reason=str(exc),
                )
            )
    summary = _summarize_enqueue_results(sync_results)
    return {
        "status": "indexed" if not summary["rejected"] else "partial_indexed",
        "workspace_id": workspace_id,
        "results": [result.to_dict() for result in sync_results],
        **summary,
    }


@app.get("/index/queue", response_model=IndexQueueStatusResponse)
def index_queue_status(
    x_user_id: str = Header(None),
    authorization: str = Header(None),
):
    _resolve_request_user(x_user_id, authorization)
    return {"status": "ok", "queue": index_queue.snapshot()}


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
                ctx = _resolve_ask_context(
                    req=req,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    db=db,
                )
                ask_anchor = ctx.primary_source.symbol

            with trace.stage("prompt"):
                system_prompt = _system_prompt_for_context(ctx)
                context_tokens = ctx.token_count()
                trace.token_counts = {
                    "context": context_tokens,
                    "user": estimate_text_tokens(req.question),
                }
                trace.model_route = _model_route(context_tokens, ctx.intent)

            with trace.stage("llm"):
                response_cache_hit = False
                degraded_response = False
                prompt_hash = hashlib.sha256(
                    f"{system_prompt}\n{req.question}".encode()
                ).hexdigest()
                cached_response = default_cache.get_response(prompt_hash, workspace_id)
                if cached_response:
                    response_cache_hit = True
                    answer = cached_response.answer
                    if hasattr(ctx, "budget"):
                        ctx.budget["cache_hits"] = sorted(
                            {*ctx.budget.get("cache_hits", []), "l3_response"}
                        )
                    trace.model_route = {
                        **trace.model_route,
                        "cached": True,
                        "cache_layer": "l3_response",
                    }
                else:
                    try:
                        answer = ai_engine.chat(
                            system_prompt=system_prompt,
                            user_message=req.question,
                            token_count=context_tokens,
                            intent=ctx.intent,
                        )
                    except RuntimeError as exc:
                        degraded_response = True
                        answer = _degraded_llm_answer(exc)
                        trace.model_route = _mark_degraded_route(trace.model_route, exc)
                        default_metrics.increment(
                            "sidecar_llm_degraded_total",
                            labels={"endpoint": "/ask", "workspace": workspace_id},
                        )
                    else:
                        default_cache.put_response(
                            prompt_hash,
                            workspace_id,
                            answer,
                            {"intent": ctx.intent, "mode": ctx.mode},
                        )
            if not response_cache_hit and not degraded_response:
                trace.model_route = _last_model_route(trace.model_route)

            output_tokens = estimate_text_tokens(answer)
            trace.token_counts["output_estimate"] = output_tokens
            trace.estimated_cost_usd, trace.cost_basis = estimate_cost_usd(
                trace.model_route,
                input_tokens=context_tokens + trace.token_counts["user"],
                output_tokens=output_tokens,
            )

            with trace.stage("audit"):
                audit_log.log_query(user_id, ask_anchor, req.question, ctx.intent, ctx.mode)

            _attach_trace_metadata(ctx, trace)
            feedback_token = feedback_store.issue_token()
            ctx.feedback_token = feedback_token
            with trace.stage("feedback_snapshot"):
                _record_retrieval_snapshot(
                    feedback_token=feedback_token,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    symbol=ask_anchor,
                    question=req.question,
                    ctx=ctx,
                    trace=trace,
                )
            logger.info("trace_id=%s endpoint=/ask status=ok", trace.trace_id)
            return {
                "symbol": ask_anchor,
                "answer": answer,
                "context": ctx.to_dict(),
                "user": user_id,
                "cloud": db.is_cloud(),
                "workspace_id": workspace_id,
                "trace_id": trace.trace_id,
                "feedback_token": feedback_token,
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
                    ctx = _resolve_ask_context(
                        req=req,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        db=db,
                    )
                    ask_anchor = ctx.primary_source.symbol

                with trace.stage("prompt"):
                    system_prompt = _system_prompt_for_context(ctx)
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
                    audit_log.log_query(user_id, ask_anchor, req.question, ctx.intent, ctx.mode)

                _attach_trace_metadata(ctx, trace)
                feedback_token = feedback_store.issue_token()
                ctx.feedback_token = feedback_token
                with trace.stage("feedback_snapshot"):
                    _record_retrieval_snapshot(
                        feedback_token=feedback_token,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        symbol=ask_anchor,
                        question=req.question,
                        ctx=ctx,
                        trace=trace,
                    )
                yield format_sse(
                    "context",
                    {
                        "type": "context",
                        "trace_id": trace.trace_id,
                        "feedback_token": feedback_token,
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


@app.post("/feedback", response_model=FeedbackResponse)
def record_feedback(
    req: FeedbackRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    """Record retrieval feedback against an issued feedback token."""
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace)
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
    workspace_id = _resolve_workspace(x_workspace)
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
            title = req.prompt_summary or (f"Ask about {req.symbol}" if req.symbol else "Workspace ask")
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
    workspace_id = _resolve_workspace(x_workspace)
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
    workspace_id = _resolve_workspace(x_workspace)
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
    workspace_id = _resolve_workspace(x_workspace)
    _history_conversation_for_scope(conversation_id, workspace_id=workspace_id, user_id=user_id)
    bundle = history_provider.get_request_bundle(conversation_id, request_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Unknown history request")
    return bundle


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
