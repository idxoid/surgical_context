"""Request-facing service bundle.

``RouteServices`` is the adapter layer the HTTP handlers reach through (via
``require_services``). It wraps a :class:`SidecarState` and exposes the service
handles, config flags, and the small ``_*`` delegate helpers that route
handlers call. Lifting these off the ``main`` module breaks the old
``main_module`` back-reference: ``create_app`` builds one of these from state
instead of pointing routes back at ``context_engine.main``.

Service handles and config flags are plain instance attributes so tests can
rebind a single seam (``services.db_session = fake``) without monkeypatching
module globals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi import HTTPException

from context_engine.api.deps import (
    canonical_user_id,
    header_value,
    resolve_request_user,
    resolve_workspace,
    resolve_workspace_context,
)
from context_engine.api.schemas import HistoryAskRecordRequest
from context_engine.api.state import SidecarState
from context_engine.api.workspace_security import (
    authorize_workspace_project_root,
    require_workspace_root_dir,
    sandbox_path,
)
from context_engine.cache.layered import default_cache
from context_engine.context_types import PromptContext
from context_engine.database.session import db_session
from context_engine.index_profile import effective_index_workspace_id
from context_engine.indexer.git_delta_poller import GitDeltaTarget
from context_engine.indexer.queue import EnqueueResult, IndexWorkItem
from context_engine.observability import (
    RequestTrace,
    default_metrics,
    estimate_text_tokens,
    new_trace_id,
)
from context_engine.workspace import Workspace


class RouteServices:
    """Per-process bundle of service handles + route helper delegates."""

    def __init__(self, state: SidecarState) -> None:
        self.state = state
        config = state.config
        self.config = config

        # Config flags (mutable instance attrs — tests flip these directly).
        self.MODEL_PREFERENCE = config.model_preference
        self.ALLOW_CLOUD_LLM = config.allow_cloud_llm
        self.AUTH_REQUIRED = config.auth_required
        self.TRUST_CLIENT_USER_HEADER = config.trust_client_user_header
        self.TRUST_CLIENT_WORKSPACE_HEADER = config.trust_client_workspace_header

        # Service handles.
        self.overlay = state.overlay
        self.vector_db = state.vector_db
        self.ai_engine = state.ai_engine
        self.user_auth = state.user_auth
        self.audit_log = state.audit_log
        self.workspace_resolver = state.workspace_resolver
        self.feedback_store = state.feedback_store
        self.history_provider = state.history_provider
        self.index_queue = state.index_queue
        self.git_delta_registry = state.git_delta_registry
        self.git_delta_poller = state.git_delta_poller
        self.indexing_service = state.indexing_service
        self.ask_context_builder = state.ask_context_builder
        self.ask_service = state.ask_service

        # Free-function bridges routes reach as attributes.
        self.db_session = db_session
        self.default_cache = default_cache
        self.default_metrics = default_metrics
        self.effective_index_workspace_id = effective_index_workspace_id
        self.estimate_text_tokens = estimate_text_tokens
        self._header_value = header_value
        self._canonical_user_id = canonical_user_id

    # ------------------------------------------------------------------ #
    # Trace / workspace helpers
    # ------------------------------------------------------------------ #
    def _resolve_index_workspace(self, x_workspace: Any = None, authorization: Any = None) -> str:
        """Physical index namespace for the active profile (Neo4j/LanceDB reads/writes)."""
        return effective_index_workspace_id(self._resolve_workspace(x_workspace, authorization))

    def _start_trace(
        self, endpoint: str, x_trace_id: Any = None, workspace_id: str = ""
    ) -> RequestTrace:
        return RequestTrace(
            trace_id=new_trace_id(header_value(x_trace_id)),
            endpoint=endpoint,
            workspace_id=workspace_id,
        )

    def _resolve_request_user(
        self,
        x_user_id: Any = None,
        authorization: Any = None,
        *,
        require_auth: bool | None = None,
    ) -> str:
        return resolve_request_user(
            self.state,
            x_user_id,
            authorization,
            require_auth=self.AUTH_REQUIRED if require_auth is None else require_auth,
            trust_client_user_header=self.TRUST_CLIENT_USER_HEADER,
        )

    def _resolve_workspace_context(
        self,
        x_workspace: Any = None,
        authorization: Any = None,
    ) -> Workspace:
        return resolve_workspace_context(
            self.state,
            x_workspace,
            authorization,
            trust_client_workspace_header=self.TRUST_CLIENT_WORKSPACE_HEADER,
        )

    def _resolve_workspace(self, x_workspace: Any = None, authorization: Any = None) -> str:
        return resolve_workspace(self.state, x_workspace, authorization)

    def _require_workspace_root_dir(self, raw_project_path: str) -> Path:
        return require_workspace_root_dir(raw_project_path)

    def _authorize_workspace_project_root(
        self,
        project_root: Path,
        *,
        workspace: Workspace,
        db: Any,
    ) -> None:
        authorize_workspace_project_root(project_root, workspace=workspace, db=db)

    def _sandbox_path(
        self,
        raw_path: str,
        *,
        workspace_id: str,
        db: Any,
        workspace_root: Any = None,
    ) -> str:
        return sandbox_path(
            raw_path,
            workspace_id=workspace_id,
            db=db,
            workspace_root=workspace_root,
        )

    # ------------------------------------------------------------------ #
    # Ask context builder delegates
    # ------------------------------------------------------------------ #
    def _context_from_file(self, **kwargs: Any) -> Any:
        return self.ask_context_builder.context_from_file(**kwargs)

    def _context_from_workspace(self, *args: Any, **kwargs: Any) -> Any:
        return self.ask_context_builder.context_from_workspace(*args, **kwargs)

    def _context_from_direct(self, *args: Any, **kwargs: Any) -> Any:
        return self.ask_context_builder.context_from_direct(*args, **kwargs)

    def _context_from_axis(self, *args: Any, **kwargs: Any) -> Any:
        return self.ask_context_builder.context_from_axis(*args, **kwargs)

    def _ask_axis_first_enabled(self) -> bool:
        return self.ask_context_builder.ask_axis_first_enabled()

    def _try_axis_context(self, **kwargs: Any) -> Any:
        return self.ask_context_builder.try_axis_context(
            **kwargs,
            context_from_axis=self._context_from_axis,
        )

    def _context_budget(self, ctx: Any) -> dict[str, Any]:
        return self.ask_context_builder.context_budget(ctx)

    def _resolve_ask_context(self, **kwargs: Any) -> Any:
        return self.ask_context_builder.resolve_ask_context(
            **kwargs,
            sandbox_path=self._sandbox_path,
            context_from_axis=self._context_from_axis,
            context_from_file=self._context_from_file,
            context_from_workspace=self._context_from_workspace,
            context_from_direct=self._context_from_direct,
        )

    def _context_file_paths(self, ctx: PromptContext) -> list[str]:
        return self.ask_context_builder.context_file_paths(ctx)

    # ------------------------------------------------------------------ #
    # Ask service delegates
    # ------------------------------------------------------------------ #
    def _index_manifest_fields(
        self, db: Any, workspace_id: str
    ) -> tuple[str | None, int | None]:
        return self.ask_service.index_manifest_fields(db, workspace_id)

    def _attach_index_manifest(self, ctx: PromptContext, db: Any, workspace_id: str) -> None:
        self.ask_service.attach_index_manifest(ctx, db, workspace_id)

    def _attach_trace_metadata(self, ctx: PromptContext, trace: RequestTrace) -> None:
        self.ask_service.attach_trace_metadata(ctx, trace)

    def _request_metrics(self, trace: RequestTrace) -> dict[str, Any]:
        return self.ask_service.request_metrics(trace)

    def _stream_trace_payload(
        self,
        trace: RequestTrace,
        *,
        stage: str | None = None,
        ctx: PromptContext | None = None,
    ) -> dict[str, Any]:
        return self.ask_service.stream_trace_payload(trace, stage=stage, ctx=ctx)

    def _system_prompt_for_context(self, ctx: PromptContext) -> str:
        return self.ask_service.system_prompt_for_context(ctx)

    # ------------------------------------------------------------------ #
    # Vector search
    # ------------------------------------------------------------------ #
    def _vector_search_docs(
        self, query: str, limit: int, *, workspace_id: str
    ) -> list[dict[str, Any]]:
        try:
            return self.vector_db.search(query, limit, workspace_id=workspace_id)
        except TypeError:
            return self.vector_db.search(query, limit)

    def _vector_search_symbols(
        self, query: str, limit: int, *, workspace_id: str
    ) -> list[dict[str, Any]]:
        search_symbols = getattr(self.vector_db, "search_symbols", None)
        if not callable(search_symbols):
            return []
        try:
            return cast(
                list[dict[str, Any]],
                search_symbols(query, limit, threshold=1.0, workspace_id=workspace_id),
            )
        except TypeError:
            return cast(list[dict[str, Any]], search_symbols(query, limit, threshold=1.0))

    def _axis_graph_neighbors(self, **kwargs: Any) -> list[dict[str, Any]]:
        from context_engine.api.routes import search as _search_routes

        return _search_routes._axis_graph_neighbors(**kwargs)

    # ------------------------------------------------------------------ #
    # History
    # ------------------------------------------------------------------ #
    def _history_conversation_for_scope(
        self,
        conversation_id: str,
        *,
        workspace_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        conversation = self.history_provider.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Unknown history conversation")
        if conversation["workspace_id"] != workspace_id:
            raise HTTPException(
                status_code=403, detail="History conversation belongs to another workspace"
            )
        if conversation["user_id"] != user_id:
            raise HTTPException(
                status_code=403, detail="History conversation belongs to another user"
            )
        return cast(dict[str, Any], conversation)

    def _history_enabled(self) -> bool:
        return bool(getattr(self.history_provider, "enabled", True))

    def _history_snapshot(
        self,
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

    # ------------------------------------------------------------------ #
    # Indexing
    # ------------------------------------------------------------------ #
    def _index_file_now(self, file_path: str, base_workspace_id: str, user_id: str) -> int:
        return self.indexing_service.index_file_now(file_path, base_workspace_id, user_id)

    def _enqueue_index_file(
        self, file_path: str, workspace_id: str, user_id: str
    ) -> EnqueueResult:
        self.indexing_service.attach_queue(self.index_queue)
        return self.indexing_service.enqueue_index_file(file_path, workspace_id, user_id)

    def _enqueue_index_files(
        self,
        file_paths: list[str],
        workspace_id: str,
        user_id: str,
    ) -> list[EnqueueResult]:
        return self.indexing_service.enqueue_index_files(file_paths, workspace_id, user_id)

    def _summarize_enqueue_results(self, results: list[EnqueueResult]) -> dict[str, int]:
        return self.indexing_service.summarize_enqueue_results(results)

    def _process_index_batch(self, items: list[IndexWorkItem]) -> None:
        self.indexing_service.process_index_batch(items)

    def _track_git_delta_target(
        self, workspace_id: str, project_path: str, user_id: str
    ) -> None:
        self.indexing_service.track_git_delta_target(workspace_id, project_path, user_id)

    def _apply_git_head_delta_for_workspace(
        self,
        *,
        workspace_id: str,
        user_id: str,
        project_root: Path,
        db: Any,
        queue: bool,
    ) -> dict[str, Any]:
        return self.indexing_service.apply_git_head_delta_for_workspace(
            workspace_id=workspace_id,
            user_id=user_id,
            project_root=project_root,
            db=db,
            queue=queue,
        )

    def _poll_git_delta_target(self, target: GitDeltaTarget) -> dict[str, Any] | None:
        return self.indexing_service.poll_git_delta_target(target)
