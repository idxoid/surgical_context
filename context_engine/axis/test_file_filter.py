"""Test-file fencing — keep test symbols out of role retrieval.

The user's framing: *"тестовые файлы и символы должны индексироваться с
жёсткими метками :TestFile и :TestSymbol; L4 Resolver должен использовать
тесты только тогда, когда интент явно определён как impact_analysis"*.

Until the indexer carries those labels natively, this module provides
the structural predicate and the query-time fencing helpers that:

  - distinguish test paths from production paths via repository
    convention (``/tests/``, ``/t/``, ``/test/``, ``test_*.py`` files,
    ``conftest.py``), and
  - emit the Cypher fragment that excludes those files from a graph
    walk — used by ``role_lookahead``, ``cross_role_boost``, and
    ``structural_neighbours``.

The ``impact_traversal`` pass deliberately bypasses this fence: when
the question is "what tests will break if X changes?", tests are the
answer, not noise.
"""

from __future__ import annotations

import re

# Tokens that mark a path segment as part of a test surface. ``t`` is
# Celery's convention (``t/unit``, ``t/integration``); ``test`` and
# ``tests`` are the universal ones. We match on full path segments only
# (case-insensitive) so a directory called ``contests`` or ``startests``
# never gets accidentally fenced.
#
# NB: ``qa`` is deliberately NOT here. This repo clones benchmark targets
# under ``QA/repos/<repo>/``, so fencing a ``qa`` segment excluded every
# indexed benchmark repo from role scans and collapsed retrieval recall
# (fastapi 1.0 -> 0.62). Fencing the tool's own QA harness is a
# workspace-specific concern, not a universal path-segment rule.
_TEST_DIR_SEGMENTS: frozenset[str] = frozenset(
    {
        "tests",
        "test",
        "t",
        "__tests__",
        "testfixtures",
        "__testfixtures__",
        "integration",
        "e2e",
    }
)

# File-name patterns that mark a single ``.py`` as test surface. The
# regexes match the full file name (no path).
_TEST_FILE_NAME_RE: re.Pattern[str] = re.compile(r"^(test_[^/]+\.py|[^/]+_test\.py|conftest\.py)$")


def is_test_path(path: str) -> bool:
    """True when ``path`` lives under a conventional test surface.

    The check is purely structural-convention — a path segment named
    ``tests``/``test``/``t`` OR a file matching ``test_*.py`` /
    ``*_test.py`` / ``conftest.py``. It is the smallest pattern that
    captures Flask's ``tests/``, FastAPI's ``tests/``, Celery's
    ``t/unit`` and ``t/integration``, and Pytest's standard naming
    conventions.
    """
    if not path:
        return False
    norm = path.replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    for segment in parts[:-1]:  # directory components, excluding the file name
        if segment.lower() in _TEST_DIR_SEGMENTS:
            return True
    if parts and _TEST_FILE_NAME_RE.match(parts[-1]):
        return True
    return False


# Cypher fragment that excludes Symbol nodes whose containing File is a
# test surface. The caller composes it into a WHERE clause; it expects
# ``fn`` to be the bound File variable name. Kept as a single-line
# OR-chain of CONTAINS checks (cheaper than regex matches at Neo4j
# scale) — Cypher does not let us call the Python predicate directly.
_CYPHER_TEST_PATH_EXCLUSION = (
    "NOT ("
    "  fn.path CONTAINS '/tests/' "
    "  OR fn.path CONTAINS '/test/' "
    "  OR fn.path CONTAINS '/t/' "
    "  OR fn.path CONTAINS '/__tests__/' "
    "  OR fn.path CONTAINS '/integration/' "
    "  OR fn.path CONTAINS '/e2e/' "
    "  OR fn.path ENDS WITH '/conftest.py' "
    "  OR fn.path CONTAINS '/test_' "
    "  OR fn.path ENDS WITH '_test.py'"
    ")"
)


def cypher_test_exclusion_clause(file_variable: str = "fn") -> str:
    """Return a Cypher fragment that excludes test-surface symbols.

    The fragment is intended to be inserted into a ``WHERE`` clause
    that already has a bound File variable (default name ``fn``).
    The caller can prepend ``AND`` if other conditions exist.
    """
    return _CYPHER_TEST_PATH_EXCLUSION.replace("fn.path", f"{file_variable}.path")


__all__ = [
    "cypher_test_exclusion_clause",
    "is_test_path",
]
