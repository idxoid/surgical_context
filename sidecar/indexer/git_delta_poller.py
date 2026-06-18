"""Background polling for post-commit git HEAD deltas."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Lock, Thread
from typing import Any

from sidecar.indexer.git_committed import git_root_for

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 60


@dataclass(frozen=True)
class GitDeltaTarget:
    workspace_id: str
    project_path: str
    user_id: str = "anonymous"


class GitDeltaRegistry:
    """In-memory workspace → project root map for the background poller."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._targets: dict[str, GitDeltaTarget] = {}

    def register(
        self,
        workspace_id: str,
        project_path: str,
        *,
        user_id: str = "anonymous",
    ) -> None:
        resolved = str(Path(project_path).expanduser().resolve())
        with self._lock:
            self._targets[workspace_id] = GitDeltaTarget(
                workspace_id=workspace_id,
                project_path=resolved,
                user_id=user_id,
            )

    def unregister(self, workspace_id: str) -> None:
        with self._lock:
            self._targets.pop(workspace_id, None)

    def snapshot(self) -> list[GitDeltaTarget]:
        with self._lock:
            return list(self._targets.values())


class GitDeltaPoller:
    """Daemon thread that runs ``apply_git_head_delta`` on a fixed interval."""

    def __init__(
        self,
        registry: GitDeltaRegistry,
        poll_fn: Callable[[GitDeltaTarget], dict[str, Any] | None],
        *,
        interval_seconds: float = DEFAULT_POLL_SECONDS,
        auto_start: bool = False,
    ) -> None:
        if interval_seconds < 0:
            raise ValueError("interval_seconds must be >= 0")
        self.registry = registry
        self.poll_fn = poll_fn
        self.interval_seconds = interval_seconds
        self._closed = False
        self._condition = Condition()
        self._thread: Thread | None = None
        self._stats = {
            "ticks": 0,
            "syncs": 0,
            "errors": 0,
            "last_error": "",
        }
        if auto_start and interval_seconds > 0:
            self.start()

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0

    def start(self) -> None:
        if not self.enabled:
            return
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._closed = False
            self._thread = Thread(target=self._run, name="git-delta-poller", daemon=True)
            self._thread.start()
            logger.info(
                "Git delta poller started (interval=%ss)",
                self.interval_seconds,
            )

    def close(self, timeout: float | None = 2.0) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=timeout)

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "enabled": self.enabled,
                "interval_seconds": self.interval_seconds,
                "targets": [
                    {
                        "workspace_id": target.workspace_id,
                        "project_path": target.project_path,
                        "user_id": target.user_id,
                    }
                    for target in self.registry.snapshot()
                ],
                **self._stats,
            }

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
                self._condition.wait(timeout=self.interval_seconds)
                if self._closed:
                    return
            self._tick()

    def _tick(self) -> None:
        self._stats["ticks"] += 1
        for target in self.registry.snapshot():
            project = Path(target.project_path)
            if not project.is_dir():
                continue
            if git_root_for(project) is None:
                continue
            try:
                result = self.poll_fn(target)
            except Exception:
                self._stats["errors"] += 1
                logger.exception(
                    "Git delta poll failed for workspace=%s project=%s",
                    target.workspace_id,
                    target.project_path,
                )
                continue
            if not result:
                continue
            changed = bool(
                result.get("indexed") or result.get("queued") or result.get("tombstoned")
            )
            if changed:
                self._stats["syncs"] += 1
                logger.info(
                    "Git delta sync workspace=%s head=%s indexed=%d queued=%d tombstoned=%d",
                    target.workspace_id,
                    result.get("current_head", ""),
                    len(result.get("indexed") or []),
                    len(result.get("queued") or []),
                    len(result.get("tombstoned") or []),
                )


def poll_interval_seconds() -> float:
    raw = os.getenv("GIT_DELTA_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(DEFAULT_POLL_SECONDS)


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "GitDeltaPoller",
    "GitDeltaRegistry",
    "GitDeltaTarget",
    "poll_interval_seconds",
]
