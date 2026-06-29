"""Indexing HTTP routes."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request

from context_engine.api.errors import INDEX_FAILED_REASON, PUBLIC_INTERNAL_ERROR
from context_engine.api.routes.deps import AuthHeader, UserIdHeader, WorkspaceHeader
from context_engine.api.schemas import (
    IndexDocsRequest,
    IndexFileRequest,
    IndexFileResponse,
    IndexFilesRequest,
    IndexFilesResponse,
    IndexGitDeltaRequest,
    IndexQueueStatusResponse,
    IndexRequest,
    IndexStatsResponse,
    StatusPathResponse,
)
from context_engine.api.state import SidecarState
from context_engine.indexer.job_log import IndexJobLog
from context_engine.indexer.queue import EnqueueResult
from context_engine.indexer.service import IndexingService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["indexing"])


@dataclass(frozen=True)
class IndexingRouteDeps:
    """Per-app service bundle for the indexing routes (resolved from ``Request``)."""

    services: Any
    state: SidecarState
    indexing: IndexingService


_default_deps: IndexingRouteDeps | None = None


def configure_indexing_routes(deps: IndexingRouteDeps) -> None:
    """Bind the direct-call fallback deps (HTTP requests resolve per-app)."""
    global _default_deps
    _default_deps = deps


def _require_deps(request: Request | None = None) -> IndexingRouteDeps:
    if request is not None:
        deps = getattr(request.app.state, "indexing_deps", None)
        if deps is not None:
            return cast(IndexingRouteDeps, deps)
    if _default_deps is None:
        raise RuntimeError("indexing routes are not configured")
    return _default_deps


@router.post("/index", response_model=StatusPathResponse)
def index(
    req: IndexRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    deps = _require_deps(request)
    main = deps.services
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace = main._resolve_workspace_context(x_workspace, authorization)
    workspace_id = workspace.id
    project_root = main._require_workspace_root_dir(req.project_path)
    main._track_git_delta_target(workspace_id, str(project_root), user_id)

    with main.db_session(user_id=user_id) as db:
        main._authorize_workspace_project_root(project_root, workspace=workspace, db=db)
        if req.queue:
            from context_engine.indexer.fast.collector import collect_files

            files = collect_files(str(project_root))
            safe_files = [
                main._sandbox_path(
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
            results = main._enqueue_index_files(safe_files, workspace_id, user_id)
            summary = main._summarize_enqueue_results(results)
            status = "queued"
            if not safe_files:
                status = "no_files"
            elif summary["rejected"]:
                status = "partial_queued"
            return {"status": status, "path": str(project_root), **summary}

        from context_engine.indexer.code import run_indexing
        from context_engine.indexer.fast.collector import collect_files
        from context_engine.retrieval.manifest import register_workspace_project_root

        files = collect_files(str(project_root))
        register_workspace_project_root(
            db=db,
            workspace_id=workspace_id,
            project_path=str(project_root),
            file_count=len(files),
        )
        run_indexing(str(project_root), workspace_id=workspace_id)
    return {"status": "indexed", "path": str(project_root)}


@router.post(
    "/index/file",
    response_model=IndexFileResponse,
    responses={
        400: {"description": "File not found at the sandboxed path"},
        429: {"description": "Index queue rejected the file (backpressure or duplicate)"},
        500: {"description": "Synchronous index failed"},
    },
)
def index_file_endpoint(
    req: IndexFileRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    deps = _require_deps(request)
    main = deps.services
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace_id = main._resolve_workspace(x_workspace, authorization)
    with main.db_session(user_id=user_id) as db:
        safe_path = main._sandbox_path(req.file_path, workspace_id=workspace_id, db=db)
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
        result = main._enqueue_index_file(safe_path, workspace_id, user_id)
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
        job_id = main._index_file_now(safe_path, workspace_id, user_id)
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


@router.post("/index/files", response_model=IndexFilesResponse)
def index_files_endpoint(
    req: IndexFilesRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    deps = _require_deps(request)
    main = deps.services
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace_id = main._resolve_workspace(x_workspace, authorization)
    queue_snapshot = main.index_queue.snapshot()["pending"]
    with main.db_session(user_id=user_id) as db:
        safe_paths = [
            main._sandbox_path(file_path, workspace_id=workspace_id, db=db)
            for file_path in req.file_paths
        ]
    missing = [
        EnqueueResult(
            accepted=False,
            status="skipped",
            file_path=file_path,
            workspace_id=workspace_id,
            queue_depth=queue_snapshot,
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
            queue_depth=queue_snapshot,
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
            *main._enqueue_index_files(indexable_paths, workspace_id, user_id),
        ]
        summary = main._summarize_enqueue_results(results)
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
            job_id = main._index_file_now(file_path, workspace_id, user_id)
            status = "indexed" if job_id > 0 else "skipped"
            reason = "" if job_id > 0 else "uncommitted_or_untracked"
            sync_results.append(
                EnqueueResult(
                    accepted=job_id > 0,
                    status=status,
                    file_path=file_path,
                    workspace_id=workspace_id,
                    queue_depth=main.index_queue.snapshot()["pending"],
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
                    queue_depth=main.index_queue.snapshot()["pending"],
                    reason=INDEX_FAILED_REASON,
                )
            )
    summary = main._summarize_enqueue_results(sync_results)
    return {
        "status": "indexed" if not summary["rejected"] else "partial_indexed",
        "workspace_id": workspace_id,
        "results": [result.to_dict() for result in sync_results],
        **summary,
    }


@router.post(
    "/index/git-delta",
    responses={
        400: {"description": "project_path required when workspace has no registered project root"},
    },
)
def index_git_delta_endpoint(
    req: IndexGitDeltaRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    """Incremental post-commit sync: index only files in ``prev..HEAD`` git diff."""
    from context_engine.workspace_paths import registered_workspace_root

    deps = _require_deps(request)
    main = deps.services
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace = main._resolve_workspace_context(x_workspace, authorization)
    workspace_id = workspace.id
    with main.db_session(user_id=user_id) as db:
        if req.project_path:
            project_root = main._require_workspace_root_dir(req.project_path)
            main._authorize_workspace_project_root(project_root, workspace=workspace, db=db)
        else:
            manifest_root = registered_workspace_root(db, workspace_id)
            if manifest_root is None:
                raise HTTPException(
                    status_code=400,
                    detail="project_path required when workspace has no registered project root",
                )
            project_root = manifest_root
        main._track_git_delta_target(workspace_id, str(project_root), user_id)

        stats = main._apply_git_head_delta_for_workspace(
            workspace_id=workspace_id,
            user_id=user_id,
            project_root=project_root,
            db=db,
            queue=req.queue,
        )
    return {"status": "ok", "workspace_id": workspace_id, **stats}


@router.get("/index/git-delta/status")
def index_git_delta_status(
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    request: Request = None,
):
    deps = _require_deps(request)
    deps.services._resolve_request_user(x_user_id, authorization)
    return {"status": "ok", "poller": deps.state.git_delta_poller.snapshot()}


@router.get("/index/queue", response_model=IndexQueueStatusResponse)
def index_queue_status(
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    request: Request = None,
):
    deps = _require_deps(request)
    deps.services._resolve_request_user(x_user_id, authorization)
    return {"status": "ok", "queue": deps.services.index_queue.snapshot()}


@router.get("/index/stats", response_model=IndexStatsResponse)
def index_stats(
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    """Return live catalog counts for the dashboard's active workspace."""
    deps = _require_deps(request)
    main = deps.services
    user_id = main._resolve_request_user(x_user_id, authorization)
    base_workspace_id = main._resolve_workspace(x_workspace, authorization)
    index_workspace_id = main.effective_index_workspace_id(base_workspace_id)

    with main.db_session(user_id=user_id) as db:
        counts = db.get_workspace_dashboard_counts(workspace_id=index_workspace_id)

    return {
        "status": "ok",
        "workspace_id": base_workspace_id,
        "indexed_files": int(counts.get("files") or 0),
        "indexed_symbols": int(counts.get("symbols") or 0),
        "doc_chunks": int(deps.state.vector_db.count_docs_workspace(index_workspace_id)),
        "symbols_with_docs": int(counts.get("symbols_with_docs") or 0),
        "storage_bytes": int(deps.state.vector_db.storage_size_bytes()),
    }


@router.get(
    "/index/manifest",
    responses={
        404: {"description": "Index manifest not found for this workspace (run indexing first)"},
    },
)
def index_manifest_endpoint(
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    """Return the latest index manifest stored on the Workspace node (Neo4j)."""
    deps = _require_deps(request)
    main = deps.services
    user_id = main._resolve_request_user(x_user_id, authorization)
    base_workspace_id = main._resolve_workspace(x_workspace, authorization)
    index_workspace_id = main.effective_index_workspace_id(base_workspace_id)
    with main.db_session(user_id=user_id) as db:
        get_m = getattr(db, "get_index_manifest", None)
        manifest = get_m(workspace_id=index_workspace_id) if callable(get_m) else None
    if not manifest:
        raise HTTPException(
            status_code=404,
            detail="Index manifest not found for this workspace (run indexing first)",
        )
    return manifest


@router.post(
    "/index/docs",
    response_model=StatusPathResponse,
    responses={
        400: {"description": "Docs path not found at the sandboxed directory"},
    },
)
def index_docs_endpoint(
    req: IndexDocsRequest,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    deps = _require_deps(request)
    main = deps.services
    user_id = main._resolve_request_user(x_user_id, authorization)
    base_workspace_id = main._resolve_workspace(x_workspace, authorization)
    index_workspace_id = main.effective_index_workspace_id(base_workspace_id)
    with main.db_session(user_id=user_id) as db:
        safe_docs_path = main._sandbox_path(req.docs_path, workspace_id=base_workspace_id, db=db)
    if not os.path.isdir(safe_docs_path):
        raise HTTPException(status_code=400, detail=f"Path not found: {req.docs_path}")

    from context_engine.indexer.docs import index_docs

    index_docs(safe_docs_path, workspace_id=index_workspace_id)
    return {"status": "indexed", "path": safe_docs_path}
