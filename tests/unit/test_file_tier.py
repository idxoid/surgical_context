"""Tests for the structural file-tier derivation."""

from __future__ import annotations

import pytest

from sidecar.indexer.file_tier import (
    TIER_CORE,
    TIER_DOC,
    TIER_EXAMPLE,
    TIER_REEXPORT,
    TIER_STUB,
    TIER_TEST,
    classify_file_tier,
    is_core_tier,
    is_pure_reexport_source,
)


@pytest.mark.parametrize(
    "path,expected",
    [
        # --- test surface (mirrors is_test_path + JS/TS) ---
        ("celery/t/unit/app/test_routes.py", TIER_TEST),  # Celery 't/' convention
        ("tests/test_warnings.py", TIER_TEST),
        ("src/flask/tests/conftest.py", TIER_TEST),
        ("pkg/foo_test.py", TIER_TEST),
        ("app/components/Button.spec.tsx", TIER_TEST),
        ("QA/axis_benchmark.py", TIER_TEST),
        ("startests/notatest.py", TIER_CORE),  # full-segment match only
        # --- example / tutorial / peripheral ---
        ("examples/flaskr/__init__.py", TIER_EXAMPLE),  # flask_q02 noise source
        ("fastapi/docs_src/websockets/tutorial001.py", TIER_EXAMPLE),
        ("benchmarks/bench_validate.py", TIER_EXAMPLE),
        # --- doc ---
        ("docs/migration.md", TIER_DOC),  # pydantic_q05 'docs' source
        ("pydantic/docs/version-policy.md", TIER_DOC),
        ("README.rst", TIER_DOC),
        # --- stub ---
        ("pydantic-core/python/pydantic_core/_pydantic_core.pyi", TIER_STUB),
        # --- core (the answer-bearing default) ---
        ("src/flask/blueprints.py", TIER_CORE),  # flask_q02 real answer
        ("celery/app/amqp.py", TIER_CORE),  # celery_q02 real answer
        ("pydantic/_migration.py", TIER_CORE),  # pydantic_q05 real answer
        ("lib/sqlalchemy/util/concurrency.py", TIER_CORE),
        ("", TIER_CORE),
    ],
)
def test_classify_path_tiers(path: str, expected: str) -> None:
    assert classify_file_tier(path) == expected


def test_reexport_needs_body_signal() -> None:
    # Without the body signal a plain .py is core...
    assert classify_file_tier("fastapi/websockets.py") == TIER_CORE
    # ...with it, it is a re-export surface.
    assert classify_file_tier("fastapi/websockets.py", pure_reexport=True) == TIER_REEXPORT


def test_path_tier_beats_shape_tier() -> None:
    # A re-export living under examples/ is still example (path wins).
    assert classify_file_tier("examples/app/__init__.py", pure_reexport=True) == TIER_EXAMPLE
    # A .pyi under tests/ is still test.
    assert classify_file_tier("tests/stubs/foo.pyi") == TIER_TEST


def test_is_pure_reexport_source() -> None:
    assert is_pure_reexport_source("from starlette.websockets import WebSocket as WebSocket\n")
    assert is_pure_reexport_source('"""docstring."""\nfrom x import a as a\n__all__ = ["a"]\n')
    assert not is_pure_reexport_source("from x import a\n\ndef foo():\n    return a()\n")
    assert not is_pure_reexport_source("x = 1\n")
    assert not is_pure_reexport_source("")
    assert not is_pure_reexport_source("def (:\n")  # syntax error → not a reexport


def test_is_core_tier() -> None:
    assert is_core_tier(TIER_CORE)
    assert not is_core_tier(TIER_TEST)
    assert not is_core_tier(TIER_EXAMPLE)
