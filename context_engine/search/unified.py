"""Unified ranking for docs, symbol vectors, and graph neighbors."""

from __future__ import annotations

from typing import Any, TypedDict


class UnifiedSearchResult(TypedDict):
    type: str
    title: str
    file_path: str
    content: str
    score: float
    scores: dict[str, float | None]
    provenance: list[str]
    metadata: dict[str, Any]


def dedupe_and_rank(results: list[UnifiedSearchResult], limit: int) -> list[UnifiedSearchResult]:
    """Keep the best result per source object and return a score-sorted slice."""
    best_by_key: dict[tuple[str, str, str], UnifiedSearchResult] = {}
    for item in results:
        key = (item["type"], item["file_path"], item["title"])
        current = best_by_key.get(key)
        if current is None or item["score"] > current["score"]:
            best_by_key[key] = item
    ranked = sorted(
        best_by_key.values(),
        key=lambda item: (item["score"], len(item["provenance"])),
        reverse=True,
    )
    return ranked[:limit]
