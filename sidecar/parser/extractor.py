from sidecar.parser.protocol import SymbolMetadata
from sidecar.parser.registry import REGISTRY
from sidecar.parser.uid import project_root_scope


class SymbolExtractor:
    def __init__(self, language: str | None = None, project_root: str | None = None):
        self.language = language  # None means auto-detect per file
        self.project_root = project_root

    def _resolve_language(self, file_path: str) -> str:
        if self.language:
            return self.language
        return REGISTRY.detect_language(file_path)

    def extract(self, file_path: str) -> list[SymbolMetadata]:
        with open(file_path, encoding="utf-8") as f:
            source_code = f.read()
        return self.extract_from_source(source_code, file_path)

    def extract_from_source(self, source_code: str, file_path: str) -> list[SymbolMetadata]:
        language = self._resolve_language(file_path)
        adapter = REGISTRY.get_adapter(language)
        with project_root_scope(self.project_root):
            return adapter.extract_symbols(source_code, file_path)

    def extract_calls(self, file_path: str) -> list[dict]:
        with open(file_path, encoding="utf-8") as f:
            source_code = f.read()
        return self.extract_calls_from_source(source_code, file_path)

    def extract_calls_from_source(self, source_code: str, file_path: str) -> list[dict]:
        language = self._resolve_language(file_path)
        adapter = REGISTRY.get_adapter(language)
        with project_root_scope(self.project_root):
            return adapter.extract_calls_from_source(source_code, file_path)

    def extract_imports(self, file_path: str):
        with open(file_path, encoding="utf-8") as f:
            source_code = f.read()
        language = self._resolve_language(file_path)
        adapter = REGISTRY.get_adapter(language)
        with project_root_scope(self.project_root):
            return adapter.extract_imports(source_code, file_path)

    def extract_inheritance(self, file_path: str):
        with open(file_path, encoding="utf-8") as f:
            source_code = f.read()
        language = self._resolve_language(file_path)
        adapter = REGISTRY.get_adapter(language)
        with project_root_scope(self.project_root):
            return adapter.extract_inheritance(source_code, file_path)

    def extract_proxy_bindings(self, file_path: str) -> list[dict]:
        with open(file_path, encoding="utf-8") as f:
            source_code = f.read()
        language = self._resolve_language(file_path)
        adapter = REGISTRY.get_adapter(language)
        method = getattr(adapter, "extract_proxy_bindings", None)
        if not callable(method):
            return []
        with project_root_scope(self.project_root):
            return method(source_code, file_path)

    def extract_decorators(self, file_path: str) -> list[dict]:
        with open(file_path, encoding="utf-8") as f:
            source_code = f.read()
        language = self._resolve_language(file_path)
        adapter = REGISTRY.get_adapter(language)
        method = getattr(adapter, "extract_decorators", None)
        if not callable(method):
            return []
        with project_root_scope(self.project_root):
            return method(source_code, file_path)

    def extract_type_references(self, file_path: str) -> list[dict]:
        with open(file_path, encoding="utf-8") as f:
            source_code = f.read()
        language = self._resolve_language(file_path)
        adapter = REGISTRY.get_adapter(language)
        method = getattr(adapter, "extract_type_references", None)
        if not callable(method):
            return []
        with project_root_scope(self.project_root):
            return method(source_code, file_path)

    def extract_injections(self, file_path: str) -> list[dict]:
        with open(file_path, encoding="utf-8") as f:
            source_code = f.read()
        language = self._resolve_language(file_path)
        adapter = REGISTRY.get_adapter(language)
        method = getattr(adapter, "extract_injections", None)
        if not callable(method):
            return []
        with project_root_scope(self.project_root):
            return method(source_code, file_path)
