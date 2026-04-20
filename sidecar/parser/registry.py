"""Language adapter registry — ADR-005 plugin discovery."""

import importlib
import os
from pathlib import Path

from sidecar.parser.protocol import LanguageAdapter


class LanguageAdapterRegistry:
    """Central registry of language adapters."""

    def __init__(self):
        self._adapters: dict[str, LanguageAdapter] = {}
        self._ext_to_lang: dict[str, str] = {}

    def register(self, adapter: LanguageAdapter) -> None:
        """Register an adapter instance."""
        lang = adapter.language_name
        if lang in self._adapters:
            raise ValueError(f"Adapter for {lang!r} already registered")
        self._adapters[lang] = adapter
        for ext in adapter.file_extensions:
            if ext in self._ext_to_lang:
                raise ValueError(f"Extension {ext!r} already mapped to {self._ext_to_lang[ext]!r}")
            self._ext_to_lang[ext] = lang

    def get_adapter(self, language: str) -> LanguageAdapter:
        """Fetch adapter by language name."""
        if language not in self._adapters:
            raise ValueError(f"No adapter registered for language: {language!r}")
        return self._adapters[language]

    def detect_language(self, file_path: str) -> str:
        """Auto-detect language from file extension."""
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in self._ext_to_lang:
            raise ValueError(f"Unknown file extension: {ext!r}")
        return self._ext_to_lang[ext]

    def supported_languages(self) -> list[str]:
        """Return list of registered language names."""
        return sorted(self._adapters.keys())

    def supported_adapters(self) -> list[LanguageAdapter]:
        """Return list of registered adapters."""
        return [self._adapters[lang] for lang in self.supported_languages()]


def bootstrap_adapters() -> LanguageAdapterRegistry:
    """Auto-discover and register adapters from adapters/ directory."""
    registry = LanguageAdapterRegistry()
    adapters_dir = Path(__file__).parent / "adapters"

    for module_file in sorted(adapters_dir.glob("*_adapter.py")):
        module_name = module_file.stem  # e.g., "python_adapter"
        try:
            mod = importlib.import_module(f"sidecar.parser.adapters.{module_name}")
            if hasattr(mod, "make_adapter"):
                adapter = mod.make_adapter()
                registry.register(adapter)
        except Exception:
            # Silent failure on missing adapters — allows for optional adapters in future
            pass

    return registry


# Global singleton
REGISTRY = bootstrap_adapters()
