"""Structural file-tier classification — one index-time signal.

A file's *tier* is its role in the repository's **structure**, derived
purely from path topology and file shape — never from semantic content.
It unifies two retrieval problems that are really the same missing
notion (see ``docs/file_tier_signal.md``):

* **noise pollution** — ``examples/`` apps, ``__init__`` re-exports and
  ``.pyi`` stubs out-ranking the real answer in seed retrieval, and
* **scope / test expectation** — the same ``tests/`` / ``docs/`` files
  being the *expected answer* for impact questions.

The ranker resolves both by weighting the tier with a sign chosen by
intent (demote non-core for behaviour questions, promote the test tier
for impact/trace modes). This module owns only the **derivation**; the
weight policy lives ranker-side.

It supersedes the binary ``axis/test_file_filter.is_test_path`` shim and
the cascade's ``NOISE_PATH_PATTERNS`` demotion list, generalising both
to six tiers. The ``test`` rules deliberately mirror ``is_test_path`` so
the existing fence behaviour is preserved exactly when the tier is read.
"""

from __future__ import annotations

import ast
import re

# Tier identifiers — string constants (cheap to store as a node prop).
TIER_CORE = "core"
TIER_TEST = "test"
TIER_EXAMPLE = "example"
TIER_DOC = "doc"
TIER_STUB = "stub"
TIER_REEXPORT = "reexport"

#: All tiers, in precedence order (first match wins). Path tiers are
#: resolved before shape tiers; ``core`` is the default fallthrough.
TIERS: tuple[str, ...] = (
    TIER_TEST,
    TIER_EXAMPLE,
    TIER_DOC,
    TIER_STUB,
    TIER_REEXPORT,
    TIER_CORE,
)

# --- test surface (mirrors is_test_path, plus JS/TS conventions) -----------
# Full path-segment match only, so ``contests`` / ``startests`` never
# get fenced. ``t`` is Celery's convention (``t/unit``, ``t/integration``).
_TEST_DIR_SEGMENTS: frozenset[str] = frozenset(
    {"tests", "test", "t", "qa", "__tests__", "testfixtures", "__testfixtures__"}
)
_TEST_FILE_NAME_RE: re.Pattern[str] = re.compile(
    r"^("
    r"test_[^/]+\.(py|js|jsx|ts|tsx)"
    r"|[^/]+_test\.(py|js|jsx|ts|tsx)"
    r"|[^/]+\.(spec|test)\.[a-z]+"
    r"|conftest\.py"
    r")$"
)

# --- example / tutorial / peripheral non-answer code -----------------------
_EXAMPLE_DIR_SEGMENTS: frozenset[str] = frozenset(
    {
        "examples",
        "example",
        "tutorial",
        "tutorials",
        "demo",
        "demos",
        "samples",
        "docs_src",
        "benchmarks",
        "codemods",
    }
)

# --- documentation ---------------------------------------------------------
_DOC_DIR_SEGMENTS: frozenset[str] = frozenset({"docs", "doc"})
_DOC_EXTS: tuple[str, ...] = (".md", ".rst", ".txt")


def _segments(path: str) -> tuple[list[str], str]:
    norm = path.replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    name = parts[-1] if parts else ""
    return parts, name


def is_pure_reexport_source(source: str) -> bool:
    """True when a module body is *only* imports, ``__all__`` and a
    docstring — a re-export surface that carries no answer of its own
    (e.g. ``from starlette.websockets import WebSocket as WebSocket``).
    """
    if not source.strip():
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    has_import = False
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            has_import = True
        elif isinstance(node, ast.Assign) and all(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            continue  # ``__all__ = [...]``
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue  # docstring / bare string
        else:
            return False
    return has_import


def classify_file_tier(path: str, *, pure_reexport: bool = False) -> str:
    """Return the structural tier of ``path``.

    ``pure_reexport`` is supplied by the caller (the indexer, which has
    the parsed body) for the shape tier; it is ignored for files that
    already match a path tier. Path topology alone decides
    ``test`` / ``example`` / ``doc``; ``stub`` is the ``.pyi`` extension;
    ``reexport`` needs the body signal.
    """
    if not path:
        return TIER_CORE
    parts, name = _segments(path)
    segs = {p.lower() for p in parts[:-1]}  # directory components only
    lname = name.lower()

    # Path tiers first.
    if segs & _TEST_DIR_SEGMENTS or _TEST_FILE_NAME_RE.match(name):
        return TIER_TEST
    if segs & _EXAMPLE_DIR_SEGMENTS:
        return TIER_EXAMPLE
    if segs & _DOC_DIR_SEGMENTS or lname.endswith(_DOC_EXTS):
        return TIER_DOC

    # Shape tiers.
    if lname.endswith(".pyi"):
        return TIER_STUB
    if pure_reexport:
        return TIER_REEXPORT
    return TIER_CORE


def is_core_tier(tier: str) -> bool:
    """True for the answer-bearing library code (the default tier)."""
    return tier == TIER_CORE


__all__ = [
    "TIERS",
    "TIER_CORE",
    "TIER_TEST",
    "TIER_EXAMPLE",
    "TIER_DOC",
    "TIER_STUB",
    "TIER_REEXPORT",
    "classify_file_tier",
    "is_pure_reexport_source",
    "is_core_tier",
]
