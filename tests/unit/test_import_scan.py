from context_engine.parser.import_scan import (
    iter_typescript_body_call_fallback_names,
    split_python_from_import,
    split_python_import_clause,
)


def test_split_python_from_import_relative_module():
    assert split_python_from_import("from .sibling import handler") == (".sibling", "handler")


def test_split_python_import_clause():
    assert split_python_import_clause("import os, sys") == "os, sys"
    assert split_python_import_clause("from x import y") is None


def test_typescript_body_call_fallback_skips_unclosed_generics():
    body = "foo<" + ("x" * 20_000)
    assert list(iter_typescript_body_call_fallback_names(body)) == []


def test_typescript_body_call_fallback_finds_generic_call():
    body = "doWork<Params>(arg)"
    assert list(iter_typescript_body_call_fallback_names(body)) == [("doWork", 0)]
