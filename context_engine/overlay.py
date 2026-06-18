from __future__ import annotations

from dataclasses import dataclass

from context_engine.parser.extractor import SymbolExtractor
from context_engine.workspace import DEFAULT_WORKSPACE_ID


@dataclass
class _OverlayEntry:
    content: str
    dirty: bool = True


class InMemoryOverlay:
    """Holds editor file content keyed by workspace; re-parses symbols on the fly."""

    def __init__(self):
        self._files: dict[tuple[str, str, str], _OverlayEntry] = {}
        self._extractor = SymbolExtractor()  # Auto-detect language per file

    def update(
        self,
        file_path: str,
        content: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
        *,
        dirty: bool = True,
    ):
        self._files[self._key(file_path, workspace_id, user_id)] = _OverlayEntry(
            content=content,
            dirty=dirty,
        )

    def clear(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ):
        self._files.pop(self._key(file_path, workspace_id, user_id), None)

    def has(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ) -> bool:
        return self._key(file_path, workspace_id, user_id) in self._files

    def is_dirty(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ) -> bool:
        entry = self._files.get(self._key(file_path, workspace_id, user_id))
        return entry.dirty if entry is not None else False

    def read_lines(
        self,
        file_path: str,
        start: int,
        end: int,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ) -> str:
        lines = self._files[self._key(file_path, workspace_id, user_id)].content.splitlines(
            keepends=True
        )
        return "".join(lines[start - 1 : end])

    def get_symbols(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ):
        content = self._files[self._key(file_path, workspace_id, user_id)].content
        metas = self._extractor.extract_from_source(content, file_path)
        return {m.name: (m.start_line, m.end_line) for m in metas}

    @staticmethod
    def _key(file_path: str, workspace_id: str, user_id: str) -> tuple[str, str, str]:
        normalized_user = (user_id or "anonymous").lower().strip() or "anonymous"
        return workspace_id, normalized_user, file_path
