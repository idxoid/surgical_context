
from sidecar.parser.protocol import SymbolMetadata
from sidecar.parser.registry import REGISTRY


class SymbolExtractor:
    def __init__(self, language: str | None = None):
        self.language = language  # None means auto-detect per file

    def _resolve_language(self, file_path: str) -> str:
        if self.language:
            return self.language
        return REGISTRY.detect_language(file_path)

    def extract(self, file_path: str) -> list[SymbolMetadata]:
        with open(file_path, encoding='utf-8') as f:
            source_code = f.read()
        return self.extract_from_source(source_code, file_path)

    def extract_from_source(self, source_code: str, file_path: str) -> list[SymbolMetadata]:
        language = self._resolve_language(file_path)
        adapter = REGISTRY.get_adapter(language)
        return adapter.extract_symbols(source_code, file_path)

    def extract_calls(self, file_path: str) -> list[dict]:
        with open(file_path, encoding='utf-8') as f:
            source_code = f.read()
        return self.extract_calls_from_source(source_code, file_path)

    def extract_calls_from_source(self, source_code: str, file_path: str) -> list[dict]:
        language = self._resolve_language(file_path)
        adapter = REGISTRY.get_adapter(language)
        return adapter.extract_calls_from_source(source_code, file_path)
