"""Structural build-output exclusion at collect time.

Generated webview bundles (committed outside the pruned build dirs) carry a
compiler sourcemap trailer as their final line, or minified single-line shape.
Both are compiler artifacts — не name patterns — so hand-written source that
merely mentions ``sourceMappingURL`` must stay collected.
"""

from __future__ import annotations

from context_engine.indexer.fast.collector import is_generated_output


def _write(tmp_path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_sourcemap_trailer_marks_generated(tmp_path):
    path = _write(
        tmp_path,
        "chunk-ABC123.js",
        'export function hi() { return "hi"; }\n//# sourceMappingURL=chunk-ABC123.js.map\n',
    )
    assert is_generated_output(path)


def test_legacy_sourcemap_directive_marks_generated(tmp_path):
    path = _write(
        tmp_path,
        "old.js",
        "var x = 1;\n//@ sourceMappingURL=old.js.map\n",
    )
    assert is_generated_output(path)


def test_minified_single_line_marks_generated(tmp_path):
    body = "var a=1;" * 400  # one 3200-char line, no newline
    path = _write(tmp_path, "vendor.min.js", body)
    assert is_generated_output(path)


def test_source_mentioning_sourcemap_is_not_generated(tmp_path):
    # Bundler/tooling source builds the directive in code; the directive is
    # not the final line, so the file stays collected.
    path = _write(
        tmp_path,
        "emit.js",
        'function emit(url) {\n  return "//# sourceMappingURL=" + url;\n}\nmodule.exports = emit;\n',
    )
    assert not is_generated_output(path)


def test_plain_source_is_not_generated(tmp_path):
    path = _write(tmp_path, "app.js", "function main() {\n  return 42;\n}\n")
    assert not is_generated_output(path)


def test_non_js_extensions_are_never_checked(tmp_path):
    path = _write(
        tmp_path,
        "module.py",
        "X = 1\n# sourceMappingURL= is meaningless in Python\n",
    )
    assert not is_generated_output(path)


def test_typescript_source_is_not_checked(tmp_path):
    # .ts is source, not emitter output — compiled TS lands in .js.
    path = _write(
        tmp_path,
        "gen.ts",
        "export const x = 1;\n//# sourceMappingURL=gen.js.map\n",
    )
    assert not is_generated_output(path)


def test_unreadable_file_is_not_generated(tmp_path):
    assert not is_generated_output(str(tmp_path / "missing.js"))
