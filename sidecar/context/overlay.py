from sidecar.parser.extractor import SymbolExtractor


class InMemoryOverlay:
    """Holds unsaved file content and re-parses symbols on the fly."""

    def __init__(self):
        self._files: dict[str, str] = {}
        self._extractor = SymbolExtractor()  # Auto-detect language per file

    def update(self, file_path: str, content: str):
        self._files[file_path] = content

    def clear(self, file_path: str):
        self._files.pop(file_path, None)

    def has(self, file_path: str) -> bool:
        return file_path in self._files

    def read_lines(self, file_path: str, start: int, end: int) -> str:
        lines = self._files[file_path].splitlines(keepends=True)
        return "".join(lines[start - 1:end])

    def get_symbols(self, file_path: str):
        content = self._files[file_path]
        metas = self._extractor.extract_from_source(content, file_path)
        return {m.name: (m.start_line, m.end_line) for m in metas}
