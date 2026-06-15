from sidecar.parser.extractor import SymbolExtractor
from sidecar.workspace import DEFAULT_WORKSPACE_ID


class InMemoryOverlay:
    """Holds unsaved file content keyed by workspace and re-parses symbols on the fly."""

    def __init__(self):
        self._files: dict[tuple[str, str, str], str] = {}
        self._extractor = SymbolExtractor()  # Auto-detect language per file

    def update(
        self,
        file_path: str,
        content: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ):
        self._files[self._key(file_path, workspace_id, user_id)] = content

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

    def read_lines(
        self,
        file_path: str,
        start: int,
        end: int,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ) -> str:
        lines = self._files[self._key(file_path, workspace_id, user_id)].splitlines(keepends=True)
        return "".join(lines[start - 1 : end])

    def get_symbols(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        user_id: str = "anonymous",
    ):
        content = self._files[self._key(file_path, workspace_id, user_id)]
        metas = self._extractor.extract_from_source(content, file_path)
        return {m.name: (m.start_line, m.end_line) for m in metas}

    @staticmethod
    def _key(file_path: str, workspace_id: str, user_id: str) -> tuple[str, str, str]:
        normalized_user = (user_id or "anonymous").lower().strip() or "anonymous"
        return workspace_id, normalized_user, file_path
