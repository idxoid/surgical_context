"""Shared progress-reporter and file-diff types for the fast pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from context_engine.indexer.fast.extractor import ExtractedFile

if TYPE_CHECKING:
    pass


def _parse_link_phase_result(result, fallback_count: int) -> tuple[int, set[str]]:
    if isinstance(result, tuple):
        return int(result[0] or 0), {str(uid) for uid in (result[1] or set()) if uid}
    if isinstance(result, set):
        return fallback_count, {str(uid) for uid in result if uid}
    if isinstance(result, int):
        return result, set()
    return fallback_count, set()


def _collect_adapter_facts_from_diffs(
    diffs: list[FileDiff],
    extract_attr: str,
) -> list:
    from context_engine.parser.registry import REGISTRY

    facts: list = []
    for diff in diffs:
        ex = diff.extracted
        try:
            language = REGISTRY.detect_language(ex.path)
            adapter = REGISTRY.get_adapter(language)
        except Exception:
            continue
        extract_fn = getattr(adapter, extract_attr, None)
        if not callable(extract_fn):
            continue
        try:
            facts.extend(extract_fn(ex.source, ex.path))
        except Exception:
            continue
    return facts


def _collect_decorator_facts(
    diffs: list[FileDiff],
    py_adapter,
    ts_adapter,
) -> tuple[list[dict], list[dict]]:
    decorators: list[dict] = []
    compositions: list[dict] = []
    for diff in diffs:
        ex = diff.extracted
        if ex.path.endswith((".py", ".pyi")):
            adapter = py_adapter
        elif ex.path.endswith((".ts", ".tsx")):
            adapter = ts_adapter
        else:
            continue
        try:
            decorators.extend(adapter.extract_decorators(ex.source, ex.path))
        except Exception:
            continue
        extract_compose = getattr(adapter, "extract_decorator_compositions", None)
        if not callable(extract_compose):
            continue
        try:
            compositions.extend(extract_compose(ex.source, ex.path))
        except Exception:
            continue
    return decorators, compositions


class ProgressReporter(Protocol):
    """Optional progress sink. The pipeline calls these on phase boundaries
    and after each unit of per-file work. Implementations decide whether to
    render a tqdm bar, log lines, a GUI event, or nothing at all."""

    def stage_start(self, stage: str, total: int) -> None: ...
    def step(self, stage: str, n: int = 1) -> None: ...
    def stage_end(self, stage: str) -> None: ...


class _NullReporter:
    """Default no-op reporter. Keeps the pipeline quiet when no one is watching."""

    def stage_start(self, stage: str, total: int) -> None:
        pass  # No-op default: pipeline runs without a progress UI attached.

    def step(self, stage: str, n: int = 1) -> None:
        pass  # No-op default: callers may pass a real reporter for tqdm/logging.

    def stage_end(self, stage: str) -> None:
        pass  # No-op default: stage boundaries are ignored unless overridden.


def _symbol_needs_upsert(sym, existing: dict | None) -> bool:
    """Replicates the baseline decision rule verbatim."""
    if existing is None:
        return True
    return bool(
        existing.get("hash") != sym.content_hash
        or int(existing.get("start_line") or 0) != sym.start_line
        or int(existing.get("end_line") or 0) != sym.end_line
    )


@dataclass
class FileDiff:
    """Parse output plus the incremental diff against the stored graph."""

    extracted: ExtractedFile
    current_uids: list[str] = field(default_factory=list)
    changed_uids: list[str] = field(default_factory=list)
    removed_uids: list[str] = field(default_factory=list)
    changed_symbols: list = field(default_factory=list)

    @property
    def edge_refresh_uids(self) -> list[str]:
        return self.changed_uids or self.current_uids
