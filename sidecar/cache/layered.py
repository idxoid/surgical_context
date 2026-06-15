"""In-process retrieval caches for context assembly.

The interfaces mirror the Phase 10 cache spec while keeping the default backend
dependency-free for local sidecar use. Redis/disk implementations can slot in
behind the same LayeredCache facade later.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from time import time

from sidecar.context_types import Subgraph
from sidecar.observability.metrics import default_metrics


@dataclass(frozen=True)
class CachedBody:
    code: str
    token_count: int
    is_dirty: bool = False


@dataclass(frozen=True)
class CachedResponse:
    answer: str
    metadata: dict
    expires_at: float


class _LRU[T]:
    def __init__(self, capacity: int):
        self.capacity = max(1, capacity)
        self._items: OrderedDict[tuple, T] = OrderedDict()

    def get(self, key: tuple) -> T | None:
        item = self._items.get(key)
        if item is None:
            return None
        self._items.move_to_end(key)
        return item

    def put(self, key: tuple, value: T) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        if len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def clear_prefix(self, prefix: tuple) -> None:
        for key in list(self._items):
            if key[: len(prefix)] == prefix:
                del self._items[key]


class InMemoryBodyCache:
    def __init__(self, capacity: int = 10_000):
        self._cache: _LRU[CachedBody] = _LRU(capacity)

    def get(self, file_path: str, line_range: tuple[int, int], file_hash: str) -> CachedBody | None:
        return self._cache.get((file_path, line_range, file_hash))

    def put(
        self,
        file_path: str,
        line_range: tuple[int, int],
        file_hash: str,
        body: CachedBody,
    ) -> None:
        self._cache.put((file_path, line_range, file_hash), body)

    def invalidate_file(self, file_path: str) -> None:
        self._cache.clear_prefix((file_path,))


class InMemorySubgraphCache:
    def __init__(self, capacity: int = 1_000):
        self._cache: _LRU[Subgraph] = _LRU(capacity)

    def get(
        self,
        primary_uid: str,
        intent_hash: str,
        budget: int,
        workspace_id: str,
        graph_version: int,
    ) -> Subgraph | None:
        return self._cache.get((workspace_id, graph_version, primary_uid, intent_hash, budget))

    def put(
        self,
        primary_uid: str,
        intent_hash: str,
        budget: int,
        workspace_id: str,
        graph_version: int,
        subgraph: Subgraph,
    ) -> None:
        self._cache.put((workspace_id, graph_version, primary_uid, intent_hash, budget), subgraph)


class InMemoryResponseCache:
    def __init__(self, capacity: int = 1_000, ttl_s: int = 86_400):
        self._cache: _LRU[CachedResponse] = _LRU(capacity)
        self.ttl_s = ttl_s
        # file_path → set of cache keys that reference it, for targeted invalidation
        self._file_index: dict[str, set[tuple]] = {}
        # cache key → file paths currently indexed for that entry
        self._key_paths: dict[tuple, set[str]] = {}

    def get(self, prompt_hash: str, workspace_id: str) -> CachedResponse | None:
        item = self._cache.get((workspace_id, prompt_hash))
        if item is None or item.expires_at < time():
            return None
        return item

    def _unindex_key(self, key: tuple) -> None:
        for path in self._key_paths.pop(key, set()):
            bucket = self._file_index.get(path)
            if not bucket:
                continue
            bucket.discard(key)
            if not bucket:
                del self._file_index[path]

    def put(
        self,
        prompt_hash: str,
        workspace_id: str,
        answer: str,
        metadata: dict,
        file_paths: list[str] | None = None,
    ) -> None:
        key = (workspace_id, prompt_hash)
        if key in self._cache._items:
            self._unindex_key(key)
        elif len(self._cache._items) >= self._cache.capacity:
            oldest_key = next(iter(self._cache._items))
            self._unindex_key(oldest_key)
        self._cache.put(
            key,
            CachedResponse(answer=answer, metadata=metadata, expires_at=time() + self.ttl_s),
        )
        indexed_paths = set(file_paths or [])
        self._key_paths[key] = indexed_paths
        for path in indexed_paths:
            self._file_index.setdefault(path, set()).add(key)

    def invalidate_files(self, file_paths: list[str], workspace_id: str | None = None) -> int:
        """Evict cached responses that reference any of the given file paths.

        When ``workspace_id`` is provided only entries belonging to that
        workspace are evicted; entries from other workspaces that share the
        same file path are left intact.  When ``workspace_id`` is ``None``
        every entry referencing the paths is dropped (full-flush semantics).

        Returns the number of entries removed.
        """
        candidate_keys: set[tuple] = set()
        for path in file_paths:
            candidate_keys.update(self._file_index.get(path, set()))

        if workspace_id is not None:
            keys_to_drop = {k for k in candidate_keys if k[0] == workspace_id}
        else:
            keys_to_drop = candidate_keys

        removed = 0
        for key in keys_to_drop:
            if key in self._cache._items:
                del self._cache._items[key]
                removed += 1
            self._unindex_key(key)

        # Remove dropped keys from every path bucket in the file index.
        for path in list(self._file_index):
            self._file_index[path].difference_update(keys_to_drop)
            if not self._file_index[path]:
                del self._file_index[path]

        return removed


class LayeredCache:
    def __init__(
        self,
        l1: InMemoryBodyCache | None = None,
        l2: InMemorySubgraphCache | None = None,
        l3: InMemoryResponseCache | None = None,
    ):
        self.l1 = l1 or InMemoryBodyCache()
        self.l2 = l2 or InMemorySubgraphCache()
        self.l3 = l3 or InMemoryResponseCache()

    def get_body(self, file_path: str, line_range: tuple[int, int], file_hash: str):
        item = self.l1.get(file_path, line_range, file_hash)
        default_metrics.increment(
            "cache_hits_total" if item else "cache_misses_total",
            labels={"layer": "l1_body"},
        )
        return item

    def put_body(self, file_path: str, line_range: tuple[int, int], file_hash: str, body):
        self.l1.put(file_path, line_range, file_hash, body)

    def get_subgraph(
        self,
        primary_uid: str,
        intent_hash: str,
        budget: int,
        workspace_id: str,
        graph_version: int,
    ):
        item = self.l2.get(primary_uid, intent_hash, budget, workspace_id, graph_version)
        default_metrics.increment(
            "cache_hits_total" if item else "cache_misses_total",
            labels={"layer": "l2_subgraph"},
        )
        return item

    def put_subgraph(
        self,
        primary_uid: str,
        intent_hash: str,
        budget: int,
        workspace_id: str,
        graph_version: int,
        subgraph: Subgraph,
    ) -> None:
        self.l2.put(primary_uid, intent_hash, budget, workspace_id, graph_version, subgraph)

    def get_response(self, prompt_hash: str, workspace_id: str):
        item = self.l3.get(prompt_hash, workspace_id)
        default_metrics.increment(
            "cache_hits_total" if item else "cache_misses_total",
            labels={"layer": "l3_response"},
        )
        return item

    def put_response(
        self,
        prompt_hash: str,
        workspace_id: str,
        answer: str,
        metadata: dict,
        file_paths: list[str] | None = None,
    ) -> None:
        self.l3.put(prompt_hash, workspace_id, answer, metadata, file_paths=file_paths)

    def invalidate_files(self, file_paths: list[str], workspace_id: str) -> None:
        """Evict L1 body entries and L3 response entries for the given files."""
        for path in file_paths:
            self.l1.invalidate_file(path)
        removed = self.l3.invalidate_files(file_paths, workspace_id=workspace_id)
        default_metrics.increment(
            "cache_invalidations_total",
            value=len(file_paths),
            labels={"layer": "l1_body", "workspace": workspace_id},
        )
        if removed:
            default_metrics.increment(
                "cache_invalidations_total",
                value=removed,
                labels={"layer": "l3_response", "workspace": workspace_id},
            )


default_cache = LayeredCache()
