"""FastAPI context_engine — install stderr filtering before LanceDB / SentenceTransformer import."""

import sys
from pathlib import Path

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context_engine.env_loader import load_repo_dotenv

load_repo_dotenv()

from context_engine.silence import install as _install_stderr_filter

_install_stderr_filter()

import logging
from typing import Any, cast

from fastapi import HTTPException

from context_engine.api.app import create_app
from context_engine.api.config import load_context_engine_config
from context_engine.api.deps import (
    canonical_user_id as _canonical_user_id,  # noqa: F401 — route bridge
)
from context_engine.api.deps import (
    header_value as _header_value,
)
from context_engine.api.deps import (
    resolve_request_user,
    resolve_workspace,
    resolve_workspace_context,
)
from context_engine.api.routes.indexing import (
    IndexingRouteDeps,  # noqa: F401 — re-exported for tests
)
from context_engine.api.schemas import (
    AskAxisRequest,  # noqa: F401 — re-exported for endpoint tests
    AskRequest,  # noqa: F401 — re-exported for endpoint tests
    FeedbackRequest,  # noqa: F401 — re-exported for endpoint tests
    HistoryAskRecordRequest,
    IndexFileRequest,  # noqa: F401 — re-exported for endpoint tests
    IndexFilesRequest,  # noqa: F401 — re-exported for endpoint tests
    IndexRequest,  # noqa: F401 — re-exported for endpoint tests
    OverlayRequest,  # noqa: F401 — re-exported for endpoint tests
    UnifiedSearchRequest,  # noqa: F401 — re-exported for endpoint tests
)
from context_engine.api.state import SidecarState, build_context_engine_state
from context_engine.api.workspace_security import (
    authorize_workspace_project_root,
    require_workspace_root_dir,
    sandbox_path,
)
from context_engine.cache.layered import default_cache  # noqa: F401 — route bridge
from context_engine.context_types import PromptContext
from context_engine.database.session import db_session
from context_engine.index_profile import effective_index_workspace_id
from context_engine.indexer.git_delta_poller import GitDeltaTarget
from context_engine.indexer.job_log import IndexJobLog
from context_engine.indexer.queue import EnqueueResult, IndexWorkItem
from context_engine.observability import (
    RequestTrace,
    default_metrics,
    estimate_text_tokens,  # noqa: F401 — route bridge
    new_trace_id,
)
from context_engine.workspace import (
    DEFAULT_WORKSPACE_ID,  # noqa: F401 — re-exported for tests
    Workspace,
)

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


state = build_context_engine_state(load_context_engine_config())

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
    return cast(dict[str, Any], conversation)


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
    # Deliberate late-bound injection of main's (test-patchable) IndexJobLog.
    # It is a class, so the assignment trips mypy's "Cannot assign to a type"
    # (silenced); ruff B010 rules out the setattr alternative.
    index_service_mod.IndexJobLog = IndexJobLog  # type: ignore[misc]
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


app = create_app(state, main_module=sys.modules[__name__])

from context_engine.api.routes import ask as _ask_routes
from context_engine.api.routes import auth as _auth_routes
from context_engine.api.routes import feedback as _feedback_routes
from context_engine.api.routes import health as _health_routes
from context_engine.api.routes import history as _history_routes
from context_engine.api.routes import impact as _impact_routes
from context_engine.api.routes import indexing as _indexing_routes
from context_engine.api.routes import overlay as _overlay_routes
from context_engine.api.routes import search as _search_routes

ask = _ask_routes.ask  # noqa: F401 — re-exported for endpoint tests
ask_axis = _ask_routes.ask_axis  # noqa: F401
ask_stream = _ask_routes.ask_stream  # noqa: F401
audit_actions = _auth_routes.audit_actions  # noqa: F401
auth_token = _auth_routes.auth_token  # noqa: F401
clear_overlay = _overlay_routes.clear_overlay  # noqa: F401
cloud_status = _auth_routes.cloud_status  # noqa: F401
health = _health_routes.health  # noqa: F401
history_conversation = _history_routes.history_conversation  # noqa: F401
history_conversations = _history_routes.history_conversations  # noqa: F401
history_request_bundle = _history_routes.history_request_bundle  # noqa: F401
impact = _impact_routes.impact  # noqa: F401
index = _indexing_routes.index  # noqa: F401
index_docs_endpoint = _indexing_routes.index_docs_endpoint  # noqa: F401
index_file_endpoint = _indexing_routes.index_file_endpoint  # noqa: F401
index_files_endpoint = _indexing_routes.index_files_endpoint  # noqa: F401
index_git_delta_endpoint = _indexing_routes.index_git_delta_endpoint  # noqa: F401
index_git_delta_status = _indexing_routes.index_git_delta_status  # noqa: F401
index_manifest_endpoint = _indexing_routes.index_manifest_endpoint  # noqa: F401
index_queue_status = _indexing_routes.index_queue_status  # noqa: F401
index_stats = _indexing_routes.index_stats  # noqa: F401
list_users = _auth_routes.list_users  # noqa: F401
metrics = _health_routes.metrics  # noqa: F401
record_feedback = _feedback_routes.record_feedback  # noqa: F401
record_history_ask = _history_routes.record_history_ask  # noqa: F401
search = _search_routes.search  # noqa: F401
unified_search = _search_routes.unified_search  # noqa: F401
update_overlay = _overlay_routes.update_overlay  # noqa: F401


def _axis_graph_neighbors(**kwargs):
    return _search_routes._axis_graph_neighbors(**kwargs)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
