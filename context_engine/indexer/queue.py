"""Bounded background queue for coalesced indexing work."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition, Thread
from typing import Any


@dataclass
class IndexWorkItem:
    file_path: str
    workspace_id: str
    user_id: str = "anonymous"
    enqueued_at: float = 0.0
    updated_at: float = 0.0
    generation: int = 1

    @property
    def key(self) -> tuple[str, str]:
        return (self.workspace_id, self.file_path)


@dataclass
class EnqueueResult:
    accepted: bool
    status: str
    file_path: str
    workspace_id: str
    queue_depth: int
    generation: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "file_path": self.file_path,
            "workspace_id": self.workspace_id,
            "queue_depth": self.queue_depth,
            "generation": self.generation,
            "reason": self.reason,
        }


class IndexBatchQueue:
    """Coalesce duplicate file saves and process bounded batches in the background."""

    def __init__(
        self,
        processor: Callable[[list[IndexWorkItem]], None],
        *,
        max_pending: int = 500,
        debounce_ms: int = 500,
        batch_size: int = 50,
        auto_start: bool = True,
    ):
        if max_pending < 1:
            raise ValueError("max_pending must be >= 1")
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")

        self.processor = processor
        self.max_pending = max_pending
        self.debounce_seconds = max(0, debounce_ms) / 1000
        self.batch_size = batch_size
        self._pending: dict[tuple[str, str], IndexWorkItem] = {}
        self._processing = 0
        self._closed = False
        self._last_error = ""
        self._stats = {
            "enqueued": 0,
            "coalesced": 0,
            "rejected": 0,
            "processed": 0,
            "failed_batches": 0,
        }
        self._condition = Condition()
        self._thread: Thread | None = None
        if auto_start:
            self.start()

    def start(self) -> None:
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._closed = False
            self._thread = Thread(target=self._run, name="index-batch-queue", daemon=True)
            self._thread.start()

    def close(self, timeout: float | None = 2.0) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=timeout)

    def enqueue_file(
        self,
        file_path: str,
        *,
        workspace_id: str,
        user_id: str = "anonymous",
    ) -> EnqueueResult:
        now = time.monotonic()
        key = (workspace_id, file_path)
        with self._condition:
            existing = self._pending.get(key)
            if existing:
                existing.updated_at = now
                existing.user_id = user_id
                existing.generation += 1
                self._stats["coalesced"] += 1
                self._condition.notify_all()
                return EnqueueResult(
                    accepted=True,
                    status="coalesced",
                    file_path=file_path,
                    workspace_id=workspace_id,
                    queue_depth=len(self._pending),
                    generation=existing.generation,
                )

            if len(self._pending) >= self.max_pending:
                self._stats["rejected"] += 1
                return EnqueueResult(
                    accepted=False,
                    status="rejected",
                    file_path=file_path,
                    workspace_id=workspace_id,
                    queue_depth=len(self._pending),
                    reason="queue_full",
                )

            item = IndexWorkItem(
                file_path=file_path,
                workspace_id=workspace_id,
                user_id=user_id,
                enqueued_at=now,
                updated_at=now,
            )
            self._pending[key] = item
            self._stats["enqueued"] += 1
            self._condition.notify_all()
            return EnqueueResult(
                accepted=True,
                status="queued",
                file_path=file_path,
                workspace_id=workspace_id,
                queue_depth=len(self._pending),
                generation=item.generation,
            )

    def process_ready_once(self, *, force: bool = False) -> int:
        """Process one ready batch synchronously, useful for deterministic tests."""
        with self._condition:
            batch = self._pop_ready_batch_locked(force=force)
        if not batch:
            return 0
        self._process_batch(batch)
        return len(batch)

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "pending": len(self._pending),
                "processing": self._processing,
                "max_pending": self.max_pending,
                "batch_size": self.batch_size,
                "debounce_ms": int(self.debounce_seconds * 1000),
                "last_error": self._last_error,
                **self._stats,
            }

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
                batch = self._pop_ready_batch_locked(force=False)
                if not batch:
                    self._wait_for_ready_locked()
                    continue
            self._process_batch(batch)

    def _wait_for_ready_locked(self) -> None:
        if not self._pending:
            self._condition.wait(timeout=1.0)
            return

        next_ready_at = min(
            item.updated_at + self.debounce_seconds for item in self._pending.values()
        )
        timeout = max(0.01, next_ready_at - time.monotonic())
        self._condition.wait(timeout=timeout)

    def _pop_ready_batch_locked(self, *, force: bool) -> list[IndexWorkItem]:
        if not self._pending:
            return []

        now = time.monotonic()
        ready = [
            item
            for item in self._pending.values()
            if force or item.updated_at + self.debounce_seconds <= now
        ]
        ready.sort(key=lambda item: (item.updated_at, item.file_path))
        batch = ready[: self.batch_size]
        for item in batch:
            self._pending.pop(item.key, None)
        self._processing += len(batch)
        return batch

    def _process_batch(self, batch: list[IndexWorkItem]) -> None:
        try:
            self.processor(batch)
        except Exception as exc:
            with self._condition:
                self._last_error = str(exc)
                self._stats["failed_batches"] += 1
        finally:
            with self._condition:
                self._processing -= len(batch)
                self._stats["processed"] += len(batch)
                self._condition.notify_all()
