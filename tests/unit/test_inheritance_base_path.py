"""Parser-level checks for ``InheritanceEdge.superclass_path``.

The bare ``superclass_name`` is the head (``Router`` for
``class C(routing.Router):``) used by the local DEPENDS_ON match.
``superclass_path`` is the dotted expression (``routing.Router``) consumed
by the EXTENDS_EXTERNAL post-pass to reach external symbols through the
file's IMPORTS_EXTERNAL_SYMBOL edges. These tests pin the contract between
those two fields without going through the database.
"""

from __future__ import annotations

from sidecar.parser.adapters.python_adapter import PythonAdapter


def _bases(source: str) -> list[tuple[str, str, str]]:
    """Return ``(class_uid, superclass_name, superclass_path)`` per parsed base."""
    adapter = PythonAdapter()
    edges = adapter.extract_inheritance(source, "/tmp/synthetic.py")
    return [(e.subclass_uid, e.superclass_name, e.superclass_path) for e in edges]


def test_bare_base_path_falls_back_to_bare_name():
    source = "class C(Starlette):\n    pass\n"

    bases = _bases(source)

    assert len(bases) == 1
    _, name, path = bases[0]
    assert name == "Starlette"
    assert path == "Starlette"


def test_dotted_base_keeps_module_attr_chain():
    source = "from starlette import routing\nclass APIRouter(routing.Router):\n    pass\n"

    bases = _bases(source)

    assert len(bases) == 1
    _, name, path = bases[0]
    # The bare head still matches what a local Symbol lookup needs.
    assert name == "Router"
    # The dotted form is what EXTENDS_EXTERNAL uses to reach the imported module.
    assert path == "routing.Router"


def test_two_segment_dotted_base_is_preserved():
    source = "import a.b\nclass C(a.b.Base):\n    pass\n"

    bases = _bases(source)

    assert len(bases) == 1
    _, name, path = bases[0]
    assert name == "Base"
    assert path == "a.b.Base"


def test_generic_base_uses_underlying_path():
    source = (
        "from typing import Generic, TypeVar\n"
        "from collections.abc import Iterable\n"
        "T = TypeVar('T')\n"
        "class C(Iterable[T]):\n"
        "    pass\n"
    )

    bases = _bases(source)

    # ``Iterable[T]`` is one base; generics don't add a separate one.
    assert any(name == "Iterable" and path == "Iterable" for _, name, path in bases)
