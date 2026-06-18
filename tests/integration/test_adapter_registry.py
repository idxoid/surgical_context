import pytest

from context_engine.parser.registry import bootstrap_adapters


class TestAdapterRegistry:
    @pytest.fixture
    def registry(self):
        return bootstrap_adapters()

    def test_registry_loads_all_adapters(self, registry):
        languages = registry.supported_languages()
        assert "python" in languages
        assert "typescript" in languages

    def test_detect_language_py(self, registry):
        lang = registry.detect_language("foo.py")
        assert lang == "python"

    def test_detect_language_pyi(self, registry):
        lang = registry.detect_language("foo.pyi")
        assert lang == "python"

    def test_detect_language_ts(self, registry):
        lang = registry.detect_language("bar.ts")
        assert lang == "typescript"

    def test_detect_language_tsx(self, registry):
        lang = registry.detect_language("bar.tsx")
        assert lang == "typescript"

    def test_unknown_extension_raises(self, registry):
        with pytest.raises(ValueError, match="Unknown file extension"):
            registry.detect_language("foo.xyz")

    def test_get_adapter_python(self, registry):
        adapter = registry.get_adapter("python")
        assert adapter.language_name == "python"

    def test_get_adapter_typescript(self, registry):
        adapter = registry.get_adapter("typescript")
        assert adapter.language_name == "typescript"

    def test_get_adapter_unknown_raises(self, registry):
        with pytest.raises(ValueError, match="No adapter registered"):
            registry.get_adapter("unknown_lang")

    def test_supported_adapters(self, registry):
        adapters = registry.supported_adapters()
        assert len(adapters) >= 2
        assert any(a.language_name == "python" for a in adapters)
        assert any(a.language_name == "typescript" for a in adapters)
