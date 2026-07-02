import textwrap

from context_engine.parser.adapters.javascript_adapter import JavaScriptAdapter
from context_engine.parser.adapters.python_adapter import PythonAdapter
from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter
from context_engine.parser.docstring_extract import (
    leading_jsdoc,
    python_docstring,
    strip_jsdoc,
)


def test_strip_jsdoc_normalizes_block():
    raw = "/** Module metadata.\n * @publicApi\n */"
    assert strip_jsdoc(raw) == "Module metadata.\n@publicApi"


def test_python_docstring_first_literal():
    source = textwrap.dedent(
        '''
        class Worker:
            """Build jobs."""

            def run(self):
                """Execute one job."""
                return 1
        '''
    )
    adapter = PythonAdapter()
    tree = adapter._parse(source)
    classes = [n for n in adapter._iter_nodes(tree.root_node) if n.type == "class_definition"]
    methods = [n for n in adapter._iter_nodes(tree.root_node) if n.type == "function_definition"]
    assert python_docstring(source, classes[0]) == "Build jobs."
    assert python_docstring(source, methods[0]) == "Execute one job."


def test_python_adapter_attaches_docstring():
    source = textwrap.dedent(
        '''
        def greet(name: str):
            """Say hello."""
            return name
        '''
    )
    symbols = PythonAdapter().extract_symbols(source, "pkg/greet.py")
    fn = next(s for s in symbols if s.name == "greet")
    assert fn.docstring == "Say hello."


def test_typescript_adapter_attaches_jsdoc():
    source = textwrap.dedent(
        """
        /** Nest module metadata. */
        export class AppModule {}
        """
    )
    symbols = TypeScriptAdapter().extract_symbols(source, "src/app.module.ts")
    assert symbols
    symbol = symbols[0]
    assert symbol is not None
    docstring = symbol.docstring
    assert docstring is not None
    assert docstring == "Nest module metadata."


def test_typescript_jsdoc_skips_decorators():
    source = textwrap.dedent(
        """
        /** Module metadata. */
        @Module({})
        export class FeatureModule {}
        """
    )
    adapter = TypeScriptAdapter()
    tree = adapter._parse(source)
    cls = next(n for n in adapter._iter_nodes(tree.root_node) if n.type == "class_declaration")
    assert leading_jsdoc(source, cls) == "Module metadata."


def test_javascript_adapter_attaches_jsdoc():
    source = textwrap.dedent(
        """
        /** Factory helper. */
        function create() {}
        """
    )
    symbols = JavaScriptAdapter().extract_symbols(source, "lib/create.js")
    assert symbols
    symbol = symbols[0]
    assert symbol is not None
    docstring = symbol.docstring
    assert docstring is not None
    assert docstring == "Factory helper."


def test_python_module_docstring_attaches_to_module_symbol():
    source = '"""Intent-axis ranking - boost candidates."""\n\ndef boost():\n    return 1\n'
    symbols = PythonAdapter().extract_symbols(source, "pkg/axis_ranking.py")
    module = next(s for s in symbols if s.kind == "module")
    assert module.docstring == "Intent-axis ranking - boost candidates."
    fn = next(s for s in symbols if s.name == "boost")
    assert not fn.docstring


def test_python_module_docstring_survives_leading_comment():
    source = '#!/usr/bin/env python\n# comment\n"""Module doc."""\nX = 1\n'
    symbols = PythonAdapter().extract_symbols(source, "pkg/mod.py")
    module = next(s for s in symbols if s.kind == "module")
    assert module.docstring == "Module doc."


def test_python_string_below_code_is_not_module_docstring():
    source = 'X = 1\n"""Not a docstring."""\n'
    symbols = PythonAdapter().extract_symbols(source, "pkg/mod.py")
    module = next(s for s in symbols if s.kind == "module")
    assert not module.docstring
