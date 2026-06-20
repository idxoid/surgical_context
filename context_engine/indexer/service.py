"""Queued and synchronous indexing orchestration."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from context_engine.api.config import SidecarConfig
from context_engine.cache.layered import default_cache
from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.session import db_session
from context_engine.index_profile import effective_index_workspace_id
from context_engine.indexer.git_delta_poller import GitDeltaRegistry, GitDeltaTarget
from context_engine.indexer.job_log import IndexJobLog
from context_engine.indexer.queue import EnqueueResult, IndexBatchQueue, IndexWorkItem
from context_engine.observability.metrics import MetricsRegistry, default_metrics
from context_engine.overlay import InMemoryOverlay

logger = logging.getLogger(__name__)


class IndexingService:
    """Index files synchronously, via the batch queue, or through git HEAD deltas."""

    def __init__(
        self,
        *,
        overlay: InMemoryOverlay,
        vector_db: LanceDBClient,
        config: SidecarConfig,
        git_delta_registry: GitDeltaRegistry,
        metrics: MetricsRegistry | None = None,
    ):
        self.overlay = overlay
        self.vector_db = vector_db
        self.config = config
        self.git_delta_registry = git_delta_registry
        self.metrics = metrics if metrics is not None else default_metrics
        self._index_queue: IndexBatchQueue | None = None

    def attach_queue(self, index_queue: IndexBatchQueue) -> None:
        self._index_queue = index_queue

    @property
    def index_queue(self) -> IndexBatchQueue:
        if self._index_queue is None:
            raise RuntimeError("IndexingService.index_queue is not attached yet")
        return self._index_queue

    def index_file_now(self, file_path: str, base_workspace_id: str, user_id: str) -> int:
        from context_engine.indexer.anchor import resolve_pending_anchors
        from context_engine.indexer.code import hash_file, index_file
        from context_engine.indexer.git_committed import should_index_file
        from context_engine.parser.extractor import SymbolExtractor

        if not should_index_file(file_path):
            return 0

        index_workspace_id = effective_index_workspace_id(base_workspace_id)
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
                    self.vector_db,
                    extractor,
                    workspace_id=index_workspace_id,
                )
                resolve_pending_anchors(db, self.vector_db, workspace_id=index_workspace_id)
        default_cache.invalidate_files([file_path], base_workspace_id)
        self.overlay.clear(file_path, workspace_id=base_workspace_id, user_id=user_id)
        return tracked_job_id

    def enqueue_index_file(
        self,
        file_path: str,
        workspace_id: str,
        user_id: str,
    ) -> EnqueueResult:
        result = self.index_queue.enqueue_file(
            file_path,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        self.metrics.increment(
            "sidecar_index_queue_events_total",
            labels={"status": result.status, "workspace": workspace_id},
        )
        return result

    def enqueue_index_files(
        self,
        file_paths: list[str],
        workspace_id: str,
        user_id: str,
    ) -> list[EnqueueResult]:
        return [self.enqueue_index_file(path, workspace_id, user_id) for path in file_paths]

    def summarize_enqueue_results(self, results: list[EnqueueResult]) -> dict[str, int]:
        queued = sum(1 for result in results if result.status == "queued")
        coalesced = sum(1 for result in results if result.status == "coalesced")
        rejected = sum(1 for result in results if not result.accepted)
        queue_depth = max(
            (result.queue_depth for result in results),
            default=self.index_queue.snapshot()["pending"],
        )
        return {
            "queued": queued,
            "coalesced": coalesced,
            "rejected": rejected,
            "queue_depth": queue_depth,
        }

    def process_index_batch(self, items: list[IndexWorkItem]) -> None:
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
        for (user_id, base_workspace_id), group in grouped.items():
            index_workspace_id = effective_index_workspace_id(base_workspace_id)
            existing_paths = [item.file_path for item in group if os.path.isfile(item.file_path)]
            missing_paths = [item.file_path for item in group if not os.path.isfile(item.file_path)]
            unsupported_paths = [path for path in existing_paths if not is_indexable_file(path)]
            indexable_paths = [path for path in existing_paths if is_indexable_file(path)]
            indexable_paths = [path for path in indexable_paths if should_index_file(path)]
            for path in missing_paths:
                logger.warning("Skipping queued index for missing file: %s", path)
                self.metrics.increment(
                    "sidecar_index_queue_skipped_total",
                    labels={"reason": "missing_file", "workspace": base_workspace_id},
                )
            for path in unsupported_paths:
                logger.info("Skipping queued index for unsupported file type: %s", path)
                self.metrics.increment(
                    "sidecar_index_queue_skipped_total",
                    labels={"reason": "unsupported_extension", "workspace": base_workspace_id},
                )
            if not indexable_paths:
                continue
            extractor.project_root = (
                os.path.commonpath(indexable_paths) if indexable_paths else None
            )

            current_hashes = {path: hash_file(path) for path in indexable_paths}
            completed = 0
            all_changed_uids: list[str] = []
            indexed_paths: list[str] = []
            adjacency_seeds: set[str] = set()
            if len(indexable_paths) == 1:
                batch_project_path = str(Path(indexable_paths[0]).parent)
            else:
                batch_project_path = os.path.commonpath(indexable_paths)
            with db_session(user_id=user_id) as db:
                get_file_hashes = getattr(db, "get_file_hashes", None)
                stored_hashes = (
                    get_file_hashes(indexable_paths, workspace_id=index_workspace_id)
                    if callable(get_file_hashes)
                    else {}
                )
                for path in indexable_paths:
                    file_hash = current_hashes[path]
                    if stored_hashes.get(path) == file_hash:
                        self.metrics.increment(
                            "sidecar_index_queue_skipped_total",
                            labels={"reason": "unchanged_hash", "workspace": base_workspace_id},
                        )
                        continue
                    try:
                        with job_log.track_file_job(path, file_hash=file_hash):
                            changed = index_file(
                                path,
                                db,
                                self.vector_db,
                                extractor,
                                workspace_id=index_workspace_id,
                                skip_affects=True,
                                collected_adjacency_seeds=adjacency_seeds,
                            )
                            all_changed_uids.extend(changed)
                            indexed_paths.append(path)
                            completed += 1
                            self.overlay.clear(
                                path,
                                workspace_id=base_workspace_id,
                                user_id=user_id,
                            )
                    except Exception:
                        logger.exception("Queued indexing failed for %s", path)
                        self.metrics.increment(
                            "sidecar_index_queue_failures_total",
                            labels={"workspace": base_workspace_id},
                        )
                if completed:
                    if all_changed_uids:
                        from context_engine.indexer.affects import AFFECTSIndexer

                        AFFECTSIndexer(db).rebuild_affects(
                            list(dict.fromkeys(all_changed_uids)),
                            workspace_id=index_workspace_id,
                        )
                    from context_engine.indexer.fast.pipeline import run_axis_incremental_finalize

                    adjacency_seeds.update(all_changed_uids)
                    run_axis_incremental_finalize(
                        db,
                        self.vector_db,
                        index_workspace_id,
                        seed_uids=adjacency_seeds,
                        project_path=batch_project_path,
                    )
                    resolve_pending_anchors(
                        db,
                        self.vector_db,
                        workspace_id=index_workspace_id,
                    )
                    default_cache.invalidate_files(indexed_paths, base_workspace_id)
                    self.metrics.increment(
                        "sidecar_index_queue_completed_files_total",
                        value=completed,
                        labels={"workspace": base_workspace_id},
                    )

    def track_git_delta_target(
        self,
        workspace_id: str,
        project_path: str,
        user_id: str,
    ) -> None:
        self.git_delta_registry.register(workspace_id, project_path, user_id=user_id)

    def apply_git_head_delta_for_workspace(
        self,
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

        def index_one(path: str, db: Any, lance: Any, *, workspace_id: str) -> list[str]:
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
            lance=self.vector_db,
            workspace_id=workspace_id,
            user_id=user_id,
            index_file_fn=index_one,
            enqueue_file_fn=self.enqueue_index_file,
            queue=queue,
        )

    def poll_git_delta_target(self, target: GitDeltaTarget) -> dict[str, Any] | None:
        with db_session(user_id=target.user_id) as db:
            return self.apply_git_head_delta_for_workspace(
                workspace_id=target.workspace_id,
                user_id=target.user_id,
                project_root=Path(target.project_path),
                db=db,
                queue=True,
            )
