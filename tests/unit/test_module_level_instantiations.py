"""Module-level execution is a structural CFG/scope fact, not a parser blind spot.

These tests pin the parser's authority over module-level execution:

  1. ``extract_symbols`` synthesizes exactly one ``kind="module"`` Symbol per
     file. The module name and uid are deterministic and don't depend on
     source content.
  2. Module-level ``name = SomeClass(...)`` assignments become their own
     ``kind="variable"`` Symbols — DFG anchors decorators / cross-file
     lookups attach to.
  3. ``extract_instantiations`` anchors module-level constructor rows to the
     Variable Symbol (when one exists) or to the module Symbol (last
     resort), never silently drops them.
  4. Externality is decided here, with the file's imports table as proof —
     ``is_external`` lands on every row.
  5. Unresolvable names (typos, unimported references) and Python built-ins
     (``dict()``, ``list()``) are dropped at the parser stage and never reach
     the linker.
"""

from __future__ import annotations

from context_engine.parser.adapters.python_adapter import PythonAdapter


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


def test_module_level_assignment_emits_variable_symbol_as_caller():
    a = _adapter()
    source = "from fastapi import FastAPI\n\napp = FastAPI()\n"

    symbols = a.extract_symbols(source, "myapp.py")
    instantiations = a.extract_instantiations(source, "myapp.py")

    # The Variable Symbol exists and matches the assignment's LHS.
    app = next(s for s in symbols if s.name == "app" and s.kind == "variable")
    assert app.qualified_name == "myapp.app"

    # Exactly one instantiation row, anchored to ``app`` (the DFG anchor —
    # not to the module fallback), marked external by parser proof.
    assert len(instantiations) == 1
    inst = instantiations[0]
    assert inst["type_qualified_name"] == "fastapi.FastAPI"
    assert inst["caller_uid"] == app.uid
    assert inst["is_external"] is True


def test_function_level_instantiation_still_routes_to_enclosing_function():
    a = _adapter()
    source = "from fastapi import FastAPI\n\ndef make_app():\n    return FastAPI()\n"

    symbols = a.extract_symbols(source, "myapp.py")
    instantiations = a.extract_instantiations(source, "myapp.py")
    make_app = next(s for s in symbols if s.name == "make_app")

    assert len(instantiations) == 1
    assert instantiations[0]["caller_uid"] == make_app.uid
    assert instantiations[0]["is_external"] is True


def test_module_function_and_method_anchors_coexist_without_double_emission():
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
    by_name = {s.name: s for s in symbols}

    callers = {row["caller_uid"] for row in instantiations}
    # Three distinct callers: app (variable), make_app (function),
    # Wrapper.__init__ (method). The module symbol does NOT take part — its
    # job is only the unreachable fallback when there is no other anchor.
    assert by_name["app"].uid in callers
    assert by_name["make_app"].uid in callers
    assert by_name["__init__"].uid in callers
    assert len(instantiations) == 3


def test_parser_drops_unresolvable_typo_and_builtins():
    a = _adapter()
    source = (
        "from fastapi import FastAPI\n"
        "\n"
        "app = FastAPI()\n"
        "ghost = FsatAPI()  # typo: unimported, dropped\n"
        "data = dict()      # built-in: not modelled as a graph anchor\n"
    )

    symbols = a.extract_symbols(source, "myapp.py")
    instantiations = a.extract_instantiations(source, "myapp.py")
    variable_names = {s.name for s in symbols if s.kind == "variable"}

    assert variable_names == {"app"}
    assert {row["type_qualified_name"] for row in instantiations} == {"fastapi.FastAPI"}


def test_dotted_attribute_constructor_resolves_through_module_import():
    a = _adapter()
    source = "from starlette import routing\n\nsub_app = routing.Router()\n"

    symbols = a.extract_symbols(source, "myapp.py")
    instantiations = a.extract_instantiations(source, "myapp.py")
    sub_app = next(s for s in symbols if s.name == "sub_app" and s.kind == "variable")

    assert len(instantiations) == 1
    inst = instantiations[0]
    assert inst["caller_uid"] == sub_app.uid
    # ``routing.Router`` resolved through ``from starlette import routing`` →
    # the upstream qualified name is the imported module path + the attr.
    assert inst["type_qualified_name"] == "starlette.routing.Router"
    assert inst["is_external"] is True


def test_local_class_constructor_marks_row_internal():
    a = _adapter()
    source = "class MyBase:\n    pass\n\nlocal_inst = MyBase()\n"

    symbols = a.extract_symbols(source, "myapp.py")
    instantiations = a.extract_instantiations(source, "myapp.py")
    local_inst = next(s for s in symbols if s.name == "local_inst" and s.kind == "variable")

    assert len(instantiations) == 1
    inst = instantiations[0]
    assert inst["caller_uid"] == local_inst.uid
    assert inst["type_qualified_name"] == "myapp.MyBase"
    assert inst["is_external"] is False
