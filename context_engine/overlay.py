from __future__ import annotations

import functools
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from context_engine.index_profile import base_workspace_id as _base_workspace_id
from context_engine.parser.extractor import SymbolExtractor
from context_engine.workspace import DEFAULT_WORKSPACE_ID

if TYPE_CHECKING:
    from context_engine.observability.metrics import MetricsRegistry

DEFAULT_OVERLAY_MAX_ENTRIES = 256
DEFAULT_OVERLAY_TTL_SECONDS = 86_400.0


def _synchronized[F: Callable[..., object]](method: F) -> F:
    """Run ``method`` while holding ``self._lock`` (a reentrant lock).

    The overlay is touched from FastAPI's threadpool (sync route handlers) plus
    the git-delta poller / index-queue worker threads; without this guard a
    concurrent ``update`` mutating ``_files`` while another thread iterates it
    (eviction, ``iter_dirty_files``) raises ``dict changed size during
    iteration``.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class _OverlayEntry:
    content: str
    dirty: bool = True
    updated_at: float = field(default_factory=time.monotonic)


class InMemoryOverlay:
    """Holds editor file content keyed by workspace; re-parses symbols on the fly."""

    def __init__(
        self,
        *,
        max_entries: int | None = None,
        ttl_seconds: float | None = None,
        metrics: MetricsRegistry | None = None,
    ):
        from context_engine.observability.metrics import default_metrics

        self._max_entries = (
            max_entries
            if max_entries is not None
            else _env_int("OVERLAY_MAX_ENTRIES", DEFAULT_OVERLAY_MAX_ENTRIES)
        )
        self._ttl_seconds = (
            ttl_seconds
            if ttl_seconds is not None
            else _env_float("OVERLAY_TTL_SECONDS", DEFAULT_OVERLAY_TTL_SECONDS)
        )
        self._files: dict[tuple[str, str, str], _OverlayEntry] = {}
        self._extractor = SymbolExtractor()
        self._metrics = metrics if metrics is not None else default_metrics
        self._lock = threading.RLock()

    @_synchronized
    def update(
        self,
        file_path: str,
        content: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
        *,
        dirty: bool = True,
    ):
        now = time.monotonic()
        self._evict_expired(now)
        key = self._key(file_path, workspace_id, user_id)
        if key not in self._files:
            self._evict_for_cap()
        self._files[key] = _OverlayEntry(content=content, dirty=dirty, updated_at=now)
        self._metrics.increment("context_engine_overlay_updates_total")
        self._publish_stats()

    @_synchronized
    def clear(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ):
        key = self._key(file_path, workspace_id, user_id)
        if self._files.pop(key, None) is not None:
            self._metrics.increment(
                "context_engine_overlay_evictions_total", labels={"reason": "clear"}
            )
            self._publish_stats()

    @_synchronized
    def has(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ) -> bool:
        self._evict_expired()
        key = self._key(file_path, workspace_id, user_id)
        if key not in self._files:
            return False
        self._touch(key)
        return True

    @_synchronized
    def is_dirty(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ) -> bool:
        self._evict_expired()
        key = self._key(file_path, workspace_id, user_id)
        entry = self._files.get(key)
        if entry is None:
            return False
        self._touch(key)
        return entry.dirty

    @_synchronized
    def read_lines(
        self,
        file_path: str,
        start: int,
        end: int,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ) -> str:
        self._evict_expired()
        key = self._key(file_path, workspace_id, user_id)
        entry = self._files[key]
        self._touch(key)
        lines = entry.content.splitlines(keepends=True)
        return "".join(lines[start - 1 : end])

    @_synchronized
    def get_symbols(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ):
        self._evict_expired()
        key = self._key(file_path, workspace_id, user_id)
        entry = self._files[key]
        self._touch(key)
        try:
            metas = self._extractor.extract_from_source(entry.content, file_path)
        except ValueError:
            # Config/data files (e.g. .json) can live in overlay for line reads but
            # have no registered language adapter for symbol extraction.
            return {}
        return {m.name: (m.start_line, m.end_line) for m in metas}

    @_synchronized
    def get_calls(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ) -> list[dict]:
        self._evict_expired()
        key = self._key(file_path, workspace_id, user_id)
        entry = self._files.get(key)
        if entry is None:
            return []
        self._touch(key)
        try:
            return self._extractor.extract_calls_from_source(entry.content, file_path)
        except ValueError:
            # No symbol-extraction adapter for this extension (config/data file).
            return []

    @_synchronized
    def iter_dirty_files(
        self,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ) -> list[str]:
        """File paths of the unsaved (dirty) buffers for one workspace/user."""
        self._evict_expired()
        target_ws = _base_workspace_id(workspace_id)
        target_user = (user_id or "anonymous").lower().strip() or "anonymous"
        return [
            file_path
            for (ws, user, file_path), entry in self._files.items()
            if ws == target_ws and user == target_user and entry.dirty
        ]

    @_synchronized
    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._files),
            "bytes": sum(len(entry.content.encode("utf-8")) for entry in self._files.values()),
        }

    def _touch(self, key: tuple[str, str, str]) -> None:
        entry = self._files.get(key)
        if entry is not None:
            entry.updated_at = time.monotonic()

    def _evict_expired(self, now: float | None = None) -> int:
        if self._ttl_seconds <= 0:
            return 0
        now = now if now is not None else time.monotonic()
        expired = [
            key for key, entry in self._files.items() if now - entry.updated_at > self._ttl_seconds
        ]
        for key in expired:
            del self._files[key]
            self._metrics.increment(
                "context_engine_overlay_evictions_total", labels={"reason": "ttl"}
            )
        if expired:
            self._publish_stats()
        return len(expired)

    def _evict_for_cap(self) -> int:
        if self._max_entries <= 0:
            return 0
        evicted = 0
        while len(self._files) >= self._max_entries:
            oldest_key = min(self._files, key=lambda key: self._files[key].updated_at)
            del self._files[oldest_key]
            self._metrics.increment(
                "context_engine_overlay_evictions_total", labels={"reason": "cap"}
            )
            evicted += 1
        if evicted:
            self._publish_stats()
        return evicted

    def _publish_stats(self) -> None:
        snapshot = self.stats()
        self._metrics.set_gauge("context_engine_overlay_entries", snapshot["entries"])
        self._metrics.set_gauge("context_engine_overlay_bytes", snapshot["bytes"])

    @staticmethod
    def _key(file_path: str, workspace_id: str, user_id: str) -> tuple[str, str, str]:
        normalized_user = (user_id or "anonymous").lower().strip() or "anonymous"
        # The overlay is the editor's buffer cache, scoped to physical files,
        # not to an index profile. Callers reach it with whatever workspace id
        # they hold: the /overlay route stores under the *base* id, while axis
        # retrieval queries under the profile-*suffixed* index id. Collapse both
        # to the base so a buffer stored on write is found on read regardless of
        # the active index profile.
        normalized_ws = _base_workspace_id(workspace_id)
        return normalized_ws, normalized_user, file_path
