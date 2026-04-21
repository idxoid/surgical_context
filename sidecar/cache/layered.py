"""In-process retrieval caches for context assembly.

The interfaces mirror the Phase 10 cache spec while keeping the default backend
dependency-free for local sidecar use. Redis/disk implementations can slot in
behind the same LayeredCache facade later.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from time import time

from sidecar.context.types import Subgraph
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

    def get(self, prompt_hash: str, workspace_id: str) -> CachedResponse | None:
        item = self._cache.get((workspace_id, prompt_hash))
        if item is None or item.expires_at < time():
            return None
        return item

    def put(self, prompt_hash: str, workspace_id: str, answer: str, metadata: dict) -> None:
        self._cache.put(
            (workspace_id, prompt_hash),
            CachedResponse(answer=answer, metadata=metadata, expires_at=time() + self.ttl_s),
        )


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

    def put_response(self, prompt_hash: str, workspace_id: str, answer: str, metadata: dict):
        self.l3.put(prompt_hash, workspace_id, answer, metadata)


default_cache = LayeredCache()
