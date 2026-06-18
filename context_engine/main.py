"""FastAPI sidecar — install stderr filtering before LanceDB / SentenceTransformer import."""

from context_engine.silence import install as _install_stderr_filter

_install_stderr_filter()

import hashlib
import logging
import os
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from context_engine.ai.engine import AIEngine
from context_engine.api.errors import (
    INDEX_FAILED_REASON,
    LLM_UNREACHABLE_REASON,
    PUBLIC_INTERNAL_ERROR,
    degraded_llm_answer,
)
from context_engine.api.sse import format_sse
from context_engine.auth import AuditLog, UserAuth
from context_engine.cache.layered import default_cache
from context_engine.context_types import CONTEXT_PIPELINE_VERSION, DocChunk, PromptContext, SymbolContext
from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.session import db_session
from context_engine.doc_resolver import DocResolver
from context_engine.feedback import FeedbackEvent, FeedbackStore, RetrievalSnapshot
from context_engine.history import build_history_provider, hash_history_text, parse_retention_days
from context_engine.indexer.job_log import IndexJobLog
from context_engine.indexer.queue import EnqueueResult, IndexBatchQueue, IndexWorkItem
from context_engine.observability import (
    RequestTrace,
    default_metrics,
    estimate_cost_usd,
    estimate_text_tokens,
    new_trace_id,
)
from context_engine.overlay import InMemoryOverlay
from context_engine.search import UnifiedSearchResult, dedupe_and_rank
from context_engine.workspace import DEFAULT_WORKSPACE_ID, Workspace, WorkspaceResolver

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_PREFERENCE = os.getenv("MODEL_PREFERENCE", "ollama")  # "ollama" | "auto" | "claude"
ALLOW_CLOUD_LLM = os.getenv("ALLOW_CLOUD_LLM", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}
TRUST_CLIENT_USER_HEADER = os.getenv("TRUST_CLIENT_USER_HEADER", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TRUST_CLIENT_WORKSPACE_HEADER = os.getenv("TRUST_CLIENT_WORKSPACE_HEADER", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
INDEX_QUEUE_MAX_PENDING = int(os.getenv("INDEX_QUEUE_MAX_PENDING", "500"))
INDEX_QUEUE_DEBOUNCE_MS = int(os.getenv("INDEX_QUEUE_DEBOUNCE_MS", "500"))
INDEX_QUEUE_BATCH_SIZE = int(os.getenv("INDEX_QUEUE_BATCH_SIZE", "50"))

SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_MAX = 50
TOKEN_BUDGET_MIN = 400
TOKEN_BUDGET_MAX = 32_000
IMPACT_DEPTH_MIN = 1
IMPACT_DEPTH_MAX = 4

overlay = InMemoryOverlay()
vector_db = LanceDBClient()

# Intent label stamped on the non-axis fallback PromptContexts (file/workspace/
# direct). The legacy keyword IntentClassifier died with the cascade (Phase 5);
# these are deep fallbacks (axis is the default provider and classifies real
# intent), so a fixed label matching the legacy default is sufficient metadata.
_FALLBACK_INTENT = "exploration"
ai_engine = AIEngine(model_preference=MODEL_PREFERENCE, allow_cloud_llm=ALLOW_CLOUD_LLM)
if MODEL_PREFERENCE in {"auto", "claude"} and not ALLOW_CLOUD_LLM:
    logger.info(
        "Local-first LLM routing: MODEL_PREFERENCE=%s with ALLOW_CLOUD_LLM=false "
        "— assembled context stays on Ollama even when ANTHROPIC_API_KEY is set.",
        MODEL_PREFERENCE,
    )
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


class IndexGitDeltaRequest(BaseModel):
    project_path: str | None = None
    queue: bool = True


class IndexDocsRequest(BaseModel):
    docs_path: str


class AskRequest(BaseModel):
    symbol: str | None = None
    question: str = "What does this code do?"
    token_budget: int = Field(default=6000, ge=TOKEN_BUDGET_MIN, le=TOKEN_BUDGET_MAX)
    file_path: str | None = None


class OverlayRequest(BaseModel):
    file_path: str
    content: str
    dirty: bool = True


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=5, ge=SEARCH_LIMIT_MIN, le=SEARCH_LIMIT_MAX)


class UnifiedSearchRequest(SearchRequest):
    symbol: str | None = None
    include_graph: bool = True
    token_budget: int = Field(default=2000, ge=TOKEN_BUDGET_MIN, le=TOKEN_BUDGET_MAX)


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
    index_manifest_id: str | None = None
    index_manifest_schema_version: int | None = None


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
    index_manifest_id: str | None = None
    index_manifest_schema_version: int | None = None


class AskAxisRequest(BaseModel):
    """``/ask/axis`` payload — axis-pipeline-only retrieval shape.

    No symbol anchor, no token budget: the axis pipeline picks
    candidates by role intent. ``with_context`` toggles whether
    expanded code bundles come back; without it the response only
    carries intent matches + ranked candidates (cheap).
    """

    question: str
    top_roles: int = Field(default=3, ge=1, le=10)
    intent_threshold: float = Field(default=0.20, ge=0.0, le=1.0)
    per_role_limit: int = Field(default=7, ge=1, le=50)
    with_context: bool = True
    context_seeds_per_role: int = Field(default=2, ge=1, le=10)
    context_per_seed: int = Field(default=4, ge=1, le=20)


class AxisIntentMatchResponse(BaseModel):
    role: str
    similarity: float
    description: str


class AxisCandidateResponse(BaseModel):
    uid: str
    name: str
    file_path: str
    role: str
    satisfying_contracts: list[str]
    satisfying_kinds: list[str] = Field(default_factory=list)
    contract_count: int
    kind_count: int = 0
    vector_distance: float | None
    score: float


class AxisContextSymbolResponse(BaseModel):
    uid: str
    name: str
    file_path: str
    role: str
    distance_from_seed: int
    expansion_step: str | None
    code: str | None


class AxisContextBundleResponse(BaseModel):
    role: str
    seed: AxisContextSymbolResponse
    related: list[AxisContextSymbolResponse]


class AskAxisResponse(BaseModel):
    """Axis-pipeline response. The endpoint does not call an LLM —
    callers can plug ``context_bundles`` into their own prompt.
    """

    question: str
    workspace_id: str
    user: str
    intent_matches: list[AxisIntentMatchResponse]
    candidates_by_role: dict[str, list[AxisCandidateResponse]]
    context_bundles: list[AxisContextBundleResponse]


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


def _canonical_user_id(value: str | None) -> str:
    """Canonicalize an explicit user id without creating/updating a user record."""
    return str(value or "").lower().strip()


def _extract_bearer_token(authorization: Any = None) -> str | None:
    authorization_value = _header_value(authorization)
    if not authorization_value:
        return None
    scheme, _, token = authorization_value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    return token


def _resolve_request_user(
    x_user_id: Any = None,
    authorization: Any = None,
    *,
    require_auth: bool | None = None,
) -> str:
    """Resolve the request user and optionally require a valid bearer token."""
    require_auth = AUTH_REQUIRED if require_auth is None else require_auth
    token = _extract_bearer_token(authorization)
    if token is not None:
        if not user_auth.verify_token(token):
            raise HTTPException(status_code=401, detail="Invalid or expired bearer token")
        return user_auth.get_user_from_token(token)

    if require_auth:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    if TRUST_CLIENT_USER_HEADER:
        return user_auth.identify_user(_header_value(x_user_id))  # type: ignore[union-attr]

    return user_auth.identify_user(None)


def _resolve_workspace_context(
    x_workspace: Any = None,
    authorization: Any = None,
) -> Workspace:
    token = _extract_bearer_token(authorization)
    token_workspace: str | None = None
    if token is not None:
        if not user_auth.verify_token(token):
            raise HTTPException(status_code=401, detail="Invalid or expired bearer token")
        token_workspace = user_auth.get_workspace_from_token(token)

    header_workspace = _header_value(x_workspace)
    if token_workspace:
        if header_workspace and header_workspace != token_workspace:
            raise HTTPException(
                status_code=403,
                detail="X-Workspace does not match bearer token workspace",
            )
        try:
            return workspace_resolver.from_header(token_workspace)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if TRUST_CLIENT_WORKSPACE_HEADER:
        try:
            return workspace_resolver.from_header(header_workspace)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        return workspace_resolver.from_header(DEFAULT_WORKSPACE_ID)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_workspace(x_workspace: Any = None, authorization: Any = None) -> str:
    return _resolve_workspace_context(x_workspace, authorization).id


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
    ctx.context_pipeline_version = CONTEXT_PIPELINE_VERSION


def _index_manifest_fields(db: Any, workspace_id: str) -> tuple[str | None, int | None]:
    """Read manifest id + schema version from Neo4j Workspace (if indexed)."""
    get_m = getattr(db, "get_index_manifest", None)
    if not callable(get_m):
        return None, None
    try:
        raw = get_m(workspace_id=workspace_id)
    except TypeError:
        raw = get_m(workspace_id)
    if not isinstance(raw, dict):
        return None, None
    mid = raw.get("manifest_id")
    manifest_id = str(mid) if mid else None
    schema_v: int | None = None
    sv = raw.get("manifest_schema_version")
    if sv is not None:
        try:
            schema_v = int(sv)
        except (TypeError, ValueError):
            pass
    return manifest_id, schema_v


def _attach_index_manifest(ctx: PromptContext, db: Any, workspace_id: str) -> None:
    mid, sv = _index_manifest_fields(db, workspace_id)
    if mid:
        ctx.index_manifest_id = mid
    if sv is not None:
        ctx.index_manifest_schema_version = sv


def _request_metrics(trace: RequestTrace) -> dict[str, Any]:
    return {
        "stage_timings_ms": dict(trace.stage_timings_ms),
        "latency_slo": trace.latency_slo(),
        "token_counts": dict(trace.token_counts),
        "estimated_cost_usd": trace.estimated_cost_usd,
        "cost_basis": trace.cost_basis,
    }


def _stream_trace_payload(
    trace: RequestTrace,
    *,
    stage: str | None = None,
    ctx: PromptContext | None = None,
) -> dict[str, Any]:
    """Build an SSE trace payload for /ask/stream stage and cache visibility."""
    payload: dict[str, Any] = {
        "type": "trace",
        "trace_id": trace.trace_id,
    }
    if stage:
        payload["stage"] = stage
        elapsed = trace.stage_timings_ms.get(stage)
        if elapsed is not None:
            payload["elapsed_ms"] = elapsed
    if ctx is not None:
        cache_hits = getattr(ctx, "budget", {}).get("cache_hits")
        if cache_hits:
            payload["cache_hits"] = list(cache_hits)
    if trace.model_route:
        payload["model_route"] = dict(trace.model_route)
    return payload


def _degraded_llm_answer(exc: Exception) -> str:
    logger.warning("LLM unreachable, returning degraded context-only response: %s", exc)
    return degraded_llm_answer()


def _mark_degraded_route(route: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        **route,
        "degraded": True,
        "reason": LLM_UNREACHABLE_REASON,
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
        context_pipeline_version=getattr(
            ctx, "context_pipeline_version", CONTEXT_PIPELINE_VERSION
        ),
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


def _require_workspace_root_dir(raw_project_path: str):
    from context_engine.workspace_paths import resolve_project_root

    try:
        return resolve_project_root(raw_project_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _authorize_workspace_project_root(
    project_root: Path,
    *,
    workspace: Workspace,
    db: Any,
) -> None:
    from context_engine.workspace_paths import (
        WorkspaceRootMismatchError,
        WorkspaceRootNotAllowedError,
        registered_workspace_root,
        validate_workspace_project_root,
    )

    existing = registered_workspace_root(db, workspace.id)
    try:
        validate_workspace_project_root(
            project_root,
            workspace_repo=workspace.repo,
            existing_root=existing,
        )
    except WorkspaceRootMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WorkspaceRootNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _sandbox_path(
    raw_path: str,
    *,
    workspace_id: str,
    db: Any,
    workspace_root=None,
) -> str:
    from context_engine.workspace_paths import (
        PathOutsideWorkspaceError,
        WorkspaceRootNotRegisteredError,
        resolve_path_under_workspace_root,
    )

    try:
        return str(
            resolve_path_under_workspace_root(
                raw_path,
                workspace_id=workspace_id,
                db=db,
                workspace_root=workspace_root,
            )
        )
    except WorkspaceRootNotRegisteredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PathOutsideWorkspaceError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _read_file_context(
    file_path: str,
    *,
    workspace_id: str,
    user_id: str,
    token_budget: int,
    anchor_line: int | None = None,
) -> tuple[str, bool]:
    # file_path must already be resolved under the workspace root (see _sandbox_path).
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
        return _trim_text_to_budget(code, token_budget, anchor_line), True

    try:
        with open(file_path, encoding="utf-8") as file:
            code = file.read()
    except (OSError, FileNotFoundError):
        return "", False
    return _trim_text_to_budget(code, token_budget, anchor_line), False


def _trim_text_to_budget(text: str, token_budget: int, anchor_line: int | None = None) -> str:
    if not text:
        return ""
    max_tokens = max(400, int(token_budget * 0.75))
    if estimate_text_tokens(text) <= max_tokens:
        return text

    lines = text.splitlines()
    total = len(lines)
    max_lines = max(50, max_tokens // 4)

    if anchor_line is not None:
        # Center window around the anchor (1-based), biased slightly upward so
        # the definition header lands near the top of the window.
        center = max(0, min(anchor_line - 1, total - 1))
        half = max_lines // 2
        start = max(0, center - half // 2)
        end = min(total, start + max_lines)
        # Re-anchor start if we hit the bottom boundary.
        start = max(0, end - max_lines)
        return "\n".join(lines[start:end])

    # No anchor: keep from the top (imports / module-level definitions).
    kept: list[str] = []
    for line in lines:
        candidate = "\n".join([*kept, line])
        if estimate_text_tokens(candidate) > max_tokens:
            break
        kept.append(line)
    return "\n".join(kept)


def _find_symbol_line(file_path: str, symbol: str | None) -> int | None:
    """Return the 1-based line number of the first definition matching `symbol`, or None."""
    if not symbol or not file_path:
        return None
    try:
        with open(file_path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                stripped = line.lstrip()
                if stripped.startswith(
                    ("def ", "class ", "async def ", "function ", "const ", "let ", "var ")
                ):
                    if symbol in line:
                        return lineno
    except (OSError, FileNotFoundError):
        pass
    return None


def _context_from_file(
    *,
    file_path: str,
    question: str,
    token_budget: int,
    workspace_id: str,
    user_id: str,
    symbol: str | None = None,
) -> PromptContext | None:
    anchor_line = _find_symbol_line(file_path, symbol)
    code, is_dirty = _read_file_context(
        file_path,
        workspace_id=workspace_id,
        user_id=user_id,
        token_budget=token_budget,
        anchor_line=anchor_line,
    )
    if not code:
        return None

    intent = _FALLBACK_INTENT
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
        documentation=_search_docs(f"{file_path} {question}", limit=3, workspace_id=workspace_id),
        mode="file",
        intent=intent,
        tier_tokens={"code": estimate_text_tokens(code)},
    )
    ctx.tier_tokens.update(_doc_tier_tokens(ctx.documentation))
    return ctx


def _context_from_workspace(
    question: str, token_budget: int, *, workspace_id: str
) -> PromptContext | None:
    docs = _search_docs(question, limit=5, workspace_id=workspace_id)
    symbols = _search_symbols(question, limit=5, workspace_id=workspace_id)
    if not docs and not symbols:
        return None

    intent = _FALLBACK_INTENT
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
        intent=intent,
        tier_tokens={"cross_refs": sum(estimate_text_tokens(sym.symbol) for sym in symbols)},
    )
    ctx.tier_tokens.update(_doc_tier_tokens(docs))
    ctx.budget["token_budget"] = token_budget
    return ctx


def _context_from_direct(question: str, token_budget: int) -> PromptContext:
    intent = _FALLBACK_INTENT
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
        intent=intent,
        tier_tokens={},
        budget={"token_budget": token_budget},
    )


def _context_from_axis(
    question: str,
    *,
    workspace_id: str,
    db: Any,
    token_budget: int = 6000,
    anchor_path: str | None = None,
    trace_id: str = "",
    user_id: str = "anonymous",
) -> PromptContext | None:
    """Axis-pipeline provider: canonical retrieval -> renderable PromptContext.

    Runs ``run_axis_retrieval`` with intent-driven budgeting on (echelon-1
    seed cap + echelon-2 token/render budget, sized off ``token_budget``)
    and adapts its ranked bundles through ``axis_bundles_to_prompt_context``.
    Returns ``None`` when the pipeline yields nothing renderable, so
    ``_resolve_ask_context`` can fall through to the next provider.
    """
    from context_engine.axis.pipeline import run_axis_retrieval
    from context_engine.axis.prompt_provider import axis_bundles_to_prompt_context
    from context_engine.database.lancedb_client import LanceDBClient
    from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE

    lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)
    result = run_axis_retrieval(
        question,
        workspace_id=workspace_id,
        db=db,
        lance=lance,
        intent_budget=True,
        base_token_budget=token_budget,
        anchor_path=anchor_path,
        # Hook transparency: open hook-DECLARATION seeds through their
        # registration lifecycle (the hook->registration archetype chain).
        # Inert for non-hook seeds; closes the named-hook gap (sqlalchemy q03
        # 0.5 -> 1.0) at the cost of two cheap walks when hook seeds are present.
        hook_transparency=True,
        overlay=overlay,
        user_id=user_id,
    )
    intent = result.intent[0].role if result.intent else ""
    return axis_bundles_to_prompt_context(
        result.bundles,
        question=question,
        workspace_id=workspace_id,
        intent=intent,
        trace_id=trace_id,
        render_mode=result.render_mode,
    )


def _search_docs(query: str, limit: int, *, workspace_id: str) -> list[DocChunk]:
    try:
        return DocResolver(vector_db).search(query, limit=limit, workspace_id=workspace_id)
    except Exception:
        return []


def _search_symbols(query: str, limit: int, *, workspace_id: str) -> list[SymbolContext]:
    search_symbols = getattr(vector_db, "search_symbols", None)
    if not callable(search_symbols):
        return []
    try:
        raw_symbols = search_symbols(query, limit=limit, threshold=1.0, workspace_id=workspace_id)
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


def _doc_tier_tokens(docs: list[DocChunk]) -> dict[str, int]:
    if not docs:
        return {}
    return {"docs": sum(estimate_text_tokens(doc.content) for doc in docs)}


def _context_file_paths(ctx: PromptContext) -> list[str]:
    """Collect unique real file paths from a resolved PromptContext for cache tagging."""
    paths: list[str] = []
    seen: set[str] = set()
    for sym in [ctx.primary_source, *ctx.graph_context]:
        fp = getattr(sym, "file_path", "") or ""
        if fp and fp not in ("<none>", "<unknown>", "<workspace>") and fp not in seen:
            seen.add(fp)
            paths.append(fp)
    return paths


def _ask_axis_first_enabled() -> bool:
    """The axis pipeline is the DEFAULT /ask provider (Phase 3 cutover).

    Unset / truthy ``ASK_AXIS_FIRST`` means axis leads. False values disable
    the symbol-tier axis provider and leave only the file/workspace/direct
    fallback ladder; the old ranking cascade is gone."""
    return os.environ.get("ASK_AXIS_FIRST", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _try_axis_context(
    *, req: AskRequest, workspace_id: str, db: Any, anchor_path: str | None = None, user_id: str
) -> PromptContext | None:
    """Best-effort axis context. Any failure (missing axis index, db/Lance
    error) degrades to ``None`` so the remaining fallback ladder can answer.

    ``anchor_path`` is the already-sandboxed IDE open file (the ask anchor) —
    callers must sandbox it before passing it in."""
    try:
        return _context_from_axis(
            req.question,
            workspace_id=workspace_id,
            db=db,
            token_budget=req.token_budget,
            anchor_path=anchor_path,
            user_id=user_id,
        )
    except Exception:
        logger.exception("ask_axis_first provider failed; falling through")
        return None


def _resolve_ask_context(
    *,
    req: AskRequest,
    user_id: str,
    workspace_id: str,
    db: Any,
) -> PromptContext:
    # Sandbox the IDE anchor file UP FRONT (Phase 5): it feeds BOTH the axis
    # anchor and the file-tier fallback, so an out-of-workspace path must be
    # rejected before either provider uses it (_sandbox_path raises 403).
    safe_file_path = ""
    if req.file_path:
        safe_file_path = _sandbox_path(req.file_path, workspace_id=workspace_id, db=db)

    # Axis is the default symbol-tier provider. On nothing-renderable / failure
    # we fall straight through to the file -> workspace -> direct providers below.
    if _ask_axis_first_enabled():
        axis_ctx = _try_axis_context(
            req=req,
            workspace_id=workspace_id,
            db=db,
            anchor_path=safe_file_path or None,
            user_id=user_id,
        )
        if axis_ctx is not None:
            _context_budget(axis_ctx)["ask_level"] = "axis"
            return axis_ctx

    # Axis owns symbol retrieval. When a symbol was requested but axis rendered
    # nothing, mark it not-found so the fallback ladder preserves the /ask
    # not-found contract.
    symbol_error = f"Error: Symbol '{req.symbol}' not found in graph." if req.symbol else ""
    if req.file_path:
        file_ctx = _context_from_file(
            file_path=safe_file_path,
            question=req.question,
            token_budget=req.token_budget,
            workspace_id=workspace_id,
            user_id=user_id,
            symbol=req.symbol,
        )
        if file_ctx:
            _mark_ask_fallback(file_ctx, req, "file", symbol_error)
            return file_ctx

    workspace_ctx = _context_from_workspace(
        req.question, req.token_budget, workspace_id=workspace_id
    )
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
                "message": (f"Symbol '{req.symbol}' was not found; using {display_level} context."),
            },
        )


def _fallback_reason(symbol_error: str) -> str:
    if "not found" in symbol_error.lower():
        return "symbol_not_found"
    if symbol_error:
        return "symbol_context_unavailable"
    return "symbol_not_provided"


def _append_context_warning(current: Any, warning: dict[str, str]) -> list[dict[str, str]]:
    warnings = (
        [item for item in current if isinstance(item, dict)] if isinstance(current, list) else []
    )
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
    from context_engine.indexer.anchor import resolve_pending_anchors
    from context_engine.indexer.code import hash_file, index_file
    from context_engine.indexer.git_committed import should_index_file
    from context_engine.parser.extractor import SymbolExtractor

    if not should_index_file(file_path):
        return 0

    job_log = IndexJobLog()
    file_hash = hash_file(file_path)
    with job_log.track_file_job(file_path, file_hash=file_hash) as tracked_job_id:
        with db_session(user_id=user_id) as db:
            extractor = SymbolExtractor()
            if hasattr(extractor, "project_root"):
                extractor.project_root = os.path.dirname(file_path)
            index_file(
                file_path,
                db,
                vector_db,
                extractor,
                workspace_id=workspace_id,
            )
            resolve_pending_anchors(db, vector_db, workspace_id=workspace_id)
    default_cache.invalidate_files([file_path], workspace_id)
    overlay.clear(file_path, workspace_id=workspace_id, user_id=user_id)
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

    from context_engine.indexer.anchor import resolve_pending_anchors
    from context_engine.indexer.code import hash_file, index_file, is_indexable_file
    from context_engine.indexer.git_committed import should_index_file
    from context_engine.parser.extractor import SymbolExtractor

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
        indexable_paths = [path for path in indexable_paths if should_index_file(path)]
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
        extractor.project_root = os.path.commonpath(indexable_paths) if indexable_paths else None

        current_hashes = {path: hash_file(path) for path in indexable_paths}
        completed = 0
        all_changed_uids: list[str] = []
        indexed_paths: list[str] = []
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
                        changed = index_file(
                            path,
                            db,
                            vector_db,
                            extractor,
                            workspace_id=workspace_id,
                            skip_affects=True,
                        )
                        all_changed_uids.extend(changed)
                        indexed_paths.append(path)
                        completed += 1
                        overlay.clear(path, workspace_id=workspace_id, user_id=user_id)
                except Exception:
                    logger.exception("Queued indexing failed for %s", path)
                    default_metrics.increment(
                        "sidecar_index_queue_failures_total",
                        labels={"workspace": workspace_id},
                    )
            if completed:
                if all_changed_uids:
                    from context_engine.indexer.affects import AFFECTSIndexer

                    AFFECTSIndexer(db).rebuild_affects(
                        list(dict.fromkeys(all_changed_uids)),
                        workspace_id=workspace_id,
                    )
                resolve_pending_anchors(db, vector_db, workspace_id=workspace_id)
                default_cache.invalidate_files(indexed_paths, workspace_id)
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

from context_engine.indexer.git_delta_poller import (
    GitDeltaPoller,
    GitDeltaRegistry,
    GitDeltaTarget,
    poll_interval_seconds,
)

git_delta_registry = GitDeltaRegistry()


def _track_git_delta_target(workspace_id: str, project_path: str, user_id: str) -> None:
    git_delta_registry.register(workspace_id, project_path, user_id=user_id)


def _apply_git_head_delta_for_workspace(
    *,
    workspace_id: str,
    user_id: str,
    project_root: Path,
    db: Any,
    queue: bool,
) -> dict[str, Any]:
    from context_engine.indexer.code import index_file
    from context_engine.indexer.git_delta import apply_git_head_delta
    from context_engine.parser.extractor import SymbolExtractor

    extractor = SymbolExtractor()
    if hasattr(extractor, "project_root"):
        extractor.project_root = str(project_root)

    def _index_one(path: str, db: Any, lance: Any, *, workspace_id: str) -> list[str]:
        return index_file(
            path,
            db,
            lance,
            extractor,
            workspace_id=workspace_id,
        )

    return apply_git_head_delta(
        str(project_root),
        db=db,
        lance=vector_db,
        workspace_id=workspace_id,
        user_id=user_id,
        index_file_fn=_index_one,
        enqueue_file_fn=_enqueue_index_file,
        queue=queue,
    )


def _poll_git_delta_target(target: GitDeltaTarget) -> dict[str, Any] | None:
    with db_session(user_id=target.user_id) as db:
        return _apply_git_head_delta_for_workspace(
            workspace_id=target.workspace_id,
            user_id=target.user_id,
            project_root=Path(target.project_path),
            db=db,
            queue=True,
        )


git_delta_poller = GitDeltaPoller(
    git_delta_registry,
    _poll_git_delta_target,
    interval_seconds=poll_interval_seconds(),
    auto_start=False,
)


@asynccontextmanager
async def _app_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    git_delta_poller.start()
    try:
        yield
    finally:
        git_delta_poller.close()
        index_queue.close()


app = FastAPI(title="Surgical Context Sidecar", lifespan=_app_lifespan)


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
    workspace = _resolve_workspace_context(x_workspace, authorization)
    workspace_id = workspace.id
    project_root = _require_workspace_root_dir(req.project_path)
    _track_git_delta_target(workspace_id, str(project_root), user_id)

    with db_session(user_id=user_id) as db:
        _authorize_workspace_project_root(project_root, workspace=workspace, db=db)
        if req.queue:
            from context_engine.indexer.code import _collect_files

            files = _collect_files(str(project_root))
            safe_files = [
                _sandbox_path(
                    file_path,
                    workspace_id=workspace_id,
                    db=db,
                    workspace_root=project_root,
                )
                for file_path in files
            ]
            from context_engine.retrieval.manifest import register_workspace_project_root

            register_workspace_project_root(
                db=db,
                workspace_id=workspace_id,
                project_path=str(project_root),
                file_count=len(safe_files),
            )
            results = _enqueue_index_files(safe_files, workspace_id, user_id)
            summary = _summarize_enqueue_results(results)
            status = "queued"
            if not safe_files:
                status = "no_files"
            elif summary["rejected"]:
                status = "partial_queued"
            return {"status": status, "path": str(project_root), **summary}

        from context_engine.indexer.code import _collect_files, run_indexing
        from context_engine.retrieval.manifest import register_workspace_project_root

        files = _collect_files(str(project_root))
        register_workspace_project_root(
            db=db,
            workspace_id=workspace_id,
            project_path=str(project_root),
            file_count=len(files),
        )
        run_indexing(str(project_root), workspace_id=workspace_id)
    return {"status": "indexed", "path": str(project_root)}


@app.post("/index/file", response_model=IndexFileResponse)
def index_file_endpoint(
    req: IndexFileRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    with db_session(user_id=user_id) as db:
        safe_path = _sandbox_path(req.file_path, workspace_id=workspace_id, db=db)
    if not os.path.isfile(safe_path):
        raise HTTPException(status_code=400, detail=f"File not found: {req.file_path}")

    from context_engine.indexer.git_committed import should_index_file

    if not should_index_file(safe_path):
        return {
            "status": "skipped",
            "file_path": safe_path,
            "job_id": 0,
            "workspace_id": workspace_id,
            "reason": "uncommitted_or_untracked",
        }

    if req.queue:
        result = _enqueue_index_file(safe_path, workspace_id, user_id)
        if not result.accepted:
            raise HTTPException(status_code=429, detail=result.to_dict())
        return {
            "status": result.status,
            "file_path": safe_path,
            "job_id": 0,
            "workspace_id": workspace_id,
            "queue_depth": result.queue_depth,
            "reason": result.reason,
        }

    job_id = 0
    try:
        job_id = _index_file_now(safe_path, workspace_id, user_id)
    except Exception as exc:
        logger.exception("index_file failed for %s", safe_path)
        job_log = IndexJobLog()
        job = job_log.get_job(job_id) if job_id else None
        detail = {
            "error": PUBLIC_INTERNAL_ERROR,
            "job_id": job_id,
            "job_status": job["status"] if job else "unknown",
        }
        raise HTTPException(status_code=500, detail=detail) from exc
    return {
        "status": "indexed",
        "file_path": safe_path,
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
    workspace_id = _resolve_workspace(x_workspace, authorization)
    with db_session(user_id=user_id) as db:
        safe_paths = [
            _sandbox_path(file_path, workspace_id=workspace_id, db=db)
            for file_path in req.file_paths
        ]
    missing = [
        EnqueueResult(
            accepted=False,
            status="skipped",
            file_path=file_path,
            workspace_id=workspace_id,
            queue_depth=index_queue.snapshot()["pending"],
            reason="file_not_found",
        )
        for file_path in safe_paths
        if not os.path.isfile(file_path)
    ]
    valid_paths = [file_path for file_path in safe_paths if os.path.isfile(file_path)]

    from context_engine.indexer.git_committed import should_index_file

    uncommitted = [
        EnqueueResult(
            accepted=False,
            status="skipped",
            file_path=file_path,
            workspace_id=workspace_id,
            queue_depth=index_queue.snapshot()["pending"],
            reason="uncommitted_or_untracked",
        )
        for file_path in valid_paths
        if not should_index_file(file_path)
    ]
    indexable_paths = [file_path for file_path in valid_paths if should_index_file(file_path)]

    if req.queue:
        results = [
            *missing,
            *uncommitted,
            *_enqueue_index_files(indexable_paths, workspace_id, user_id),
        ]
        summary = _summarize_enqueue_results(results)
        status = "queued" if not summary["rejected"] else "partial_queued"
        return {
            "status": status,
            "workspace_id": workspace_id,
            "results": [result.to_dict() for result in results],
            **summary,
        }

    sync_results = [*missing, *uncommitted]
    for file_path in indexable_paths:
        try:
            job_id = _index_file_now(file_path, workspace_id, user_id)
            status = "indexed" if job_id > 0 else "skipped"
            reason = "" if job_id > 0 else "uncommitted_or_untracked"
            sync_results.append(
                EnqueueResult(
                    accepted=job_id > 0,
                    status=status,
                    file_path=file_path,
                    workspace_id=workspace_id,
                    queue_depth=index_queue.snapshot()["pending"],
                    generation=job_id,
                    reason=reason,
                )
            )
        except Exception:
            logger.exception("index_files sync failed for %s", file_path)
            sync_results.append(
                EnqueueResult(
                    accepted=False,
                    status="failed",
                    file_path=file_path,
                    workspace_id=workspace_id,
                    queue_depth=index_queue.snapshot()["pending"],
                    reason=INDEX_FAILED_REASON,
                )
            )
    summary = _summarize_enqueue_results(sync_results)
    return {
        "status": "indexed" if not summary["rejected"] else "partial_indexed",
        "workspace_id": workspace_id,
        "results": [result.to_dict() for result in sync_results],
        **summary,
    }


@app.post("/index/git-delta")
def index_git_delta_endpoint(
    req: IndexGitDeltaRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    """Incremental post-commit sync: index only files in ``prev..HEAD`` git diff."""
    from context_engine.workspace_paths import registered_workspace_root

    user_id = _resolve_request_user(x_user_id, authorization)
    workspace = _resolve_workspace_context(x_workspace, authorization)
    workspace_id = workspace.id
    with db_session(user_id=user_id) as db:
        if req.project_path:
            project_root = _require_workspace_root_dir(req.project_path)
            _authorize_workspace_project_root(project_root, workspace=workspace, db=db)
        else:
            manifest_root = registered_workspace_root(db, workspace_id)
            if manifest_root is None:
                raise HTTPException(
                    status_code=400,
                    detail="project_path required when workspace has no registered project root",
                )
            project_root = manifest_root
        _track_git_delta_target(workspace_id, str(project_root), user_id)

        stats = _apply_git_head_delta_for_workspace(
            workspace_id=workspace_id,
            user_id=user_id,
            project_root=project_root,
            db=db,
            queue=req.queue,
        )
    return {"status": "ok", "workspace_id": workspace_id, **stats}


@app.get("/index/git-delta/status")
def index_git_delta_status(
    x_user_id: str = Header(None),
    authorization: str = Header(None),
):
    _resolve_request_user(x_user_id, authorization)
    return {"status": "ok", "poller": git_delta_poller.snapshot()}


@app.get("/index/queue", response_model=IndexQueueStatusResponse)
def index_queue_status(
    x_user_id: str = Header(None),
    authorization: str = Header(None),
):
    _resolve_request_user(x_user_id, authorization)
    return {"status": "ok", "queue": index_queue.snapshot()}


@app.get("/index/manifest")
def index_manifest_endpoint(
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    """Return the latest index manifest stored on the Workspace node (Neo4j)."""
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    with db_session(user_id=user_id) as db:
        get_m = getattr(db, "get_index_manifest", None)
        manifest = get_m(workspace_id=workspace_id) if callable(get_m) else None
    if not manifest:
        raise HTTPException(
            status_code=404,
            detail="Index manifest not found for this workspace (run indexing first)",
        )
    return manifest


@app.post("/index/docs", response_model=StatusPathResponse)
def index_docs_endpoint(
    req: IndexDocsRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    with db_session(user_id=user_id) as db:
        safe_docs_path = _sandbox_path(req.docs_path, workspace_id=workspace_id, db=db)
    if not os.path.isdir(safe_docs_path):
        raise HTTPException(status_code=400, detail=f"Path not found: {req.docs_path}")

    from context_engine.indexer.docs import index_docs

    index_docs(safe_docs_path, workspace_id=workspace_id)
    return {"status": "indexed", "path": safe_docs_path}


@app.post("/search", response_model=SearchResponse)
def search(
    req: SearchRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    return {"results": _vector_search_docs(req.query, req.limit, workspace_id=workspace_id)}


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
    workspace_id = _resolve_workspace(x_workspace, authorization)
    trace = _start_trace("/search/unified", x_trace_id, workspace_id)
    status = "ok"
    results: list[UnifiedSearchResult] = []
    try:
        with trace.stage("vector_docs"):
            docs = _vector_search_docs(req.query, req.limit, workspace_id=workspace_id)
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
            symbols = _vector_search_symbols(req.query, req.limit, workspace_id=workspace_id)
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
                            workspace_id=workspace_id,
                            user_id=user_id,
                            limit=req.limit,
                        ),
                    )
                )

        ranked = dedupe_and_rank(results, req.limit)
        trace.token_counts["query"] = estimate_text_tokens(req.query)
        with db_session(user_id=user_id) as db:
            mid, sv = _index_manifest_fields(db, workspace_id)
        return {
            "trace_id": trace.trace_id,
            "workspace_id": workspace_id,
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
                            file_paths=_context_file_paths(ctx),
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
            _attach_index_manifest(ctx, db, workspace_id)
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
                "index_manifest_id": ctx.index_manifest_id or None,
                "index_manifest_schema_version": ctx.index_manifest_schema_version,
            }
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
    """Axis-pipeline answer: intent → roles → ranked candidates → context.

    Returns structured retrieval evidence WITHOUT calling an LLM. Caller
    plugs ``context_bundles`` into its own prompt. Useful for headless
    retrieval consumers (CI gates, indexers, tests) and UI surfaces that
    need retrieval evidence without answer generation.
    """

    from context_engine.axis.pipeline import run_axis_retrieval
    from context_engine.database.lancedb_client import LanceDBClient
    from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE

    user_id = _resolve_request_user(x_user_id, authorization)
    workspace_id = _resolve_workspace(x_workspace, authorization)
    trace = _start_trace("/ask/axis", x_trace_id, workspace_id)
    status = "ok"

    try:
        lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)

        # The whole read-side pipeline lives in one canonical function
        # (``run_axis_retrieval``) that the QA benchmark validates and the
        # axis->PromptContext provider also consumes — so the endpoint can
        # never drift from the measured pipeline. One ``db_session`` spans
        # every stage; ``trace`` keeps the per-stage spans.
        with db_session(user_id=user_id) as db:
            result = run_axis_retrieval(
                req.question,
                workspace_id=workspace_id,
                db=db,
                lance=lance,
                top_roles=req.top_roles,
                per_role_limit=req.per_role_limit,
                intent_threshold=req.intent_threshold,
                with_context=req.with_context,
                context_per_seed=req.context_per_seed,
                context_seeds_per_role=req.context_seeds_per_role,
                trace=trace,
                overlay=overlay,
                user_id=user_id,
            )

        intent_payload = [
            AxisIntentMatchResponse(
                role=m.role,
                similarity=m.similarity,
                description=m.description,
            )
            for m in result.intent
        ]

        # ``raw_by_role`` may carry roles the intent classifier never
        # produced — see ``expand_candidates_via_neighbourhood`` auto-promote.
        candidates_by_role: dict[str, list[AxisCandidateResponse]] = {}
        intent_role_order = [m.role for m in result.intent]
        promoted_roles = [r for r in result.raw_by_role if r not in set(intent_role_order)]
        for role in intent_role_order + promoted_roles:
            candidates = result.raw_by_role.get(role) or []
            if not candidates:
                continue
            candidates_by_role[role] = [
                AxisCandidateResponse(
                    uid=c.uid,
                    name=c.name,
                    file_path=c.file_path,
                    role=c.role,
                    satisfying_contracts=list(c.satisfying_contracts),
                    satisfying_kinds=list(c.satisfying_kinds),
                    contract_count=c.contract_count,
                    kind_count=c.kind_count,
                    vector_distance=c.vector_distance,
                    score=c.score,
                )
                for c in candidates
            ]

        bundles_payload: list[AxisContextBundleResponse] = [
            AxisContextBundleResponse(
                role=bundle.role,
                seed=AxisContextSymbolResponse(**bundle.seed.to_dict()),
                related=[AxisContextSymbolResponse(**s.to_dict()) for s in bundle.related],
            )
            for bundle in result.bundles
        ]

        logger.info(
            "trace_id=%s endpoint=/ask/axis status=ok roles=%d candidates=%d bundles=%d",
            trace.trace_id,
            len(intent_payload),
            sum(len(v) for v in candidates_by_role.values()),
            len(bundles_payload),
        )
        return AskAxisResponse(
            question=req.question,
            workspace_id=workspace_id,
            user=user_id,
            intent_matches=intent_payload,
            candidates_by_role=candidates_by_role,
            context_bundles=bundles_payload,
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
                    response_cache_hit = False
                    degraded_response = False
                    prompt_hash = hashlib.sha256(
                        f"{system_prompt}\n{req.question}".encode()
                    ).hexdigest()
                    cached_response = default_cache.get_response(prompt_hash, workspace_id)
                    if cached_response:
                        response_cache_hit = True
                        answer_parts.append(cached_response.answer)
                        if hasattr(ctx, "budget"):
                            ctx.budget["cache_hits"] = sorted(
                                {*ctx.budget.get("cache_hits", []), "l3_response"}
                            )
                        trace.model_route = {
                            **trace.model_route,
                            "cached": True,
                            "cache_layer": "l3_response",
                        }
                        yield format_sse(
                            "trace",
                            _stream_trace_payload(trace, stage="llm", ctx=ctx),
                        )
                        yield format_sse(
                            "chunk", {"type": "chunk", "content": cached_response.answer}
                        )
                    else:
                        try:
                            for chunk in ai_engine.stream_chat(
                                system_prompt=system_prompt,
                                user_message=req.question,
                                token_count=context_tokens,
                                intent=ctx.intent,
                            ):
                                answer_parts.append(chunk)
                                yield format_sse("chunk", {"type": "chunk", "content": chunk})
                        except RuntimeError as exc:
                            degraded_response = True
                            degraded_text = _degraded_llm_answer(exc)
                            answer_parts.append(degraded_text)
                            trace.model_route = _mark_degraded_route(trace.model_route, exc)
                            default_metrics.increment(
                                "sidecar_llm_degraded_total",
                                labels={
                                    "endpoint": "/ask/stream",
                                    "workspace": workspace_id,
                                },
                            )
                            yield format_sse("chunk", {"type": "chunk", "content": degraded_text})
                        else:
                            default_cache.put_response(
                                prompt_hash,
                                workspace_id,
                                "".join(answer_parts),
                                {"intent": ctx.intent, "mode": ctx.mode},
                                file_paths=_context_file_paths(ctx),
                            )
                if not response_cache_hit and not degraded_response:
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
                _attach_index_manifest(ctx, db, workspace_id)
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
                        "index_manifest_id": ctx.index_manifest_id or None,
                        "index_manifest_schema_version": ctx.index_manifest_schema_version,
                    },
                )
                yield format_sse("done", {"type": "done", "trace_id": trace.trace_id})
        except Exception:
            status = "error"
            logger.exception("trace_id=%s endpoint=/ask/stream status=error", trace.trace_id)
            yield format_sse(
                "error",
                {
                    "type": "error",
                    "error": PUBLIC_INTERNAL_ERROR,
                    "trace_id": trace.trace_id,
                },
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
    workspace_id = _resolve_workspace(x_workspace, authorization)
    with db_session(user_id=user_id) as db:
        from context_engine.axis.impact_surface import build_impact_surface

        symbol_uid = db.get_symbol_uid_by_name(symbol, workspace_id=workspace_id)
        if not symbol_uid:
            raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found")

        symbol_file = db.get_file_path_for_symbol(symbol_uid, workspace_id=workspace_id)
        surface = build_impact_surface(
            db=db,
            symbol_uid=symbol_uid,
            symbol_name=symbol,
            file_path=symbol_file,
            workspace_id=workspace_id,
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
