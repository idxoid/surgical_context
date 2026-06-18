"""Test-file fencing predicate + Cypher exclusion helper."""

from __future__ import annotations

import pytest

from context_engine.axis.test_file_filter import (
    cypher_test_exclusion_clause,
    is_test_path,
)


@pytest.mark.parametrize(
    "path",
    [
        "/repo/flask/tests/test_app.py",
        "/repo/fastapi/tests/test_routing.py",
        "/repo/celery/t/unit/test_app.py",
        "/repo/celery/t/integration/test_canvas.py",
        "/repo/django/test/test_models.py",
        "/repo/proj/tests/conftest.py",
        "/repo/proj/api/conftest.py",  # conftest at any depth
        "/repo/proj/something_test.py",
        "/repo/proj/test_main.py",
    ],
)
def test_canonical_test_paths_are_flagged(path: str) -> None:
    """Every conventional test path should be flagged — repo
    conventions across pytest, Flask, FastAPI, Django and Celery are
    all covered by this single predicate."""
    assert is_test_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/repo/flask/src/flask/app.py",
        "/repo/fastapi/fastapi/routing.py",
        "/repo/celery/celery/app/base.py",
        "/repo/proj/api/handlers.py",
        "/repo/proj/contests/winner.py",  # 'contests' is not 'tests'
        "/repo/proj/restart.py",  # 'restart' is not 'test'
        "",
    ],
)
def test_production_paths_are_not_flagged(path: str) -> None:
    """A directory called ``contests`` or a file called ``restart`` is
    not a test surface — the predicate must respect full path
    segments, not arbitrary substrings."""
    assert is_test_path(path) is False


def test_cypher_clause_excludes_test_surfaces():
    """The Cypher fragment must match the predicate's intent — a
    CONTAINS chain that catches every conventional test path
    composition. We keep the contract loose (substring presence) so the
    test stays valid as long as the clause covers each surface."""
    clause = cypher_test_exclusion_clause("fn")
    assert "fn.path" in clause
    for needle in (
        "'/tests/'",
        "'/test/'",
        "'/t/'",
        "/conftest.py'",
        "/test_'",
        "_test.py'",
    ):
        assert needle in clause, f"missing exclusion of {needle}"


def test_cypher_clause_respects_variable_name():
    """The caller's File variable might not be named ``fn``. The helper
    must substitute the supplied name everywhere — otherwise the
    Cypher query would fail with an unbound-variable error."""
    clause = cypher_test_exclusion_clause("file_node")
    assert "file_node.path" in clause
    assert "fn.path" not in clause
