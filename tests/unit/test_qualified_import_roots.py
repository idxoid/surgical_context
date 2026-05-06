from sidecar.parser.qualified_import_roots import (
    clear_qualified_import_roots_cache,
    get_qualified_import_roots,
)


class TestQualifiedImportRoots:
    def setup_method(self):
        clear_qualified_import_roots_cache()

    def teardown_method(self):
        clear_qualified_import_roots_cache()

    def test_python_includes_configured_roots(self):
        roots = get_qualified_import_roots("python")
        assert "fastapi" in roots

    def test_unknown_language_empty(self):
        assert get_qualified_import_roots("nonexistent_lang_xyz") == frozenset()

    def test_case_insensitive_key(self):
        clear_qualified_import_roots_cache()
        assert get_qualified_import_roots("PYTHON") == get_qualified_import_roots("python")
