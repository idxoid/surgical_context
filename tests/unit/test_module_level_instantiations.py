"""Module-level execution is a structural CFG/scope fact, not a parser blind spot.

These tests pin three properties together:

  1. ``extract_symbols`` synthesizes exactly one ``kind="module"`` Symbol per
     file. The module name and uid are deterministic and don't depend on
     source content.
  2. ``extract_instantiations`` anchors module-level constructor calls
     (``app = FastAPI()`` outside any ``def``/``class``) to that module
     Symbol's uid instead of dropping them.
  3. Construction inside functions / methods still routes to the enclosing
     function/method Symbol — the module fallback only kicks in when there is
     no enclosing definition.
"""

from __future__ import annotations

from sidecar.parser.adapters.python_adapter import PythonAdapter


def _adapter() -> PythonAdapter:
    return PythonAdapter()


def test_extract_symbols_synthesizes_one_module_symbol_per_file():
    a = _adapter()
    symbols = a.extract_symbols("def f(): pass\n", "pkg/sub/myapp.py")

    modules = [s for s in symbols if s.kind == "module"]
    assert len(modules) == 1
    module = modules[0]
    # Name is the dotted module qualifier, not a path.
    assert module.qualified_name == module.name
    # Identity is content-independent so re-runs are stable.
    other = a.extract_symbols("", "pkg/sub/myapp.py")
    assert other[0].uid == module.uid


def test_module_level_instantiation_routes_caller_to_module_symbol():
    a = _adapter()
    source = "from fastapi import FastAPI\n\napp = FastAPI()\n"

    symbols = a.extract_symbols(source, "myapp.py")
    instantiations = a.extract_instantiations(source, "myapp.py")
    module = next(s for s in symbols if s.kind == "module")

    assert len(instantiations) == 1
    inst = instantiations[0]
    assert inst["type_qualified_name"] == "fastapi.FastAPI"
    assert inst["caller_uid"] == module.uid


def test_function_level_instantiation_still_routes_to_enclosing_function():
    a = _adapter()
    source = (
        "from fastapi import FastAPI\n"
        "\n"
        "def make_app():\n"
        "    return FastAPI()\n"
    )

    symbols = a.extract_symbols(source, "myapp.py")
    instantiations = a.extract_instantiations(source, "myapp.py")
    make_app = next(s for s in symbols if s.name == "make_app")
    module = next(s for s in symbols if s.kind == "module")

    # One row, attributed to make_app — NOT to the module fallback.
    assert len(instantiations) == 1
    assert instantiations[0]["caller_uid"] == make_app.uid
    assert instantiations[0]["caller_uid"] != module.uid


def test_module_and_function_constructions_coexist_without_double_emission():
    a = _adapter()
    source = (
        "from fastapi import FastAPI\n"
        "\n"
        "app = FastAPI()\n"
        "\n"
        "def make_app():\n"
        "    return FastAPI()\n"
        "\n"
        "class Wrapper:\n"
        "    def __init__(self):\n"
        "        self.app = FastAPI()\n"
    )

    symbols = a.extract_symbols(source, "myapp.py")
    instantiations = a.extract_instantiations(source, "myapp.py")
    by_kind = {s.kind: s for s in symbols}

    callers = {row["caller_uid"] for row in instantiations}
    # Three distinct callers: the module, make_app, Wrapper.__init__.
    assert by_kind["module"].uid in callers
    assert any(row["caller_uid"] == by_kind["module"].uid for row in instantiations)
    assert len(instantiations) == 3
