"""Shared pytest fixtures and configuration for evaluation harness."""

import pytest

from context_engine.parser.adapters.python_adapter import PythonAdapter
from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter


def pytest_addoption(parser):
    """Add custom command-line options."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that require live Neo4j/LanceDB",
    )


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless --run-integration is passed."""
    for item in items:
        nodeid = str(getattr(item, "path", item.fspath))
        if "/tests/integration/" in nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.integration)

    if config.getoption("--run-integration"):
        return

    skip_integration = pytest.mark.skip(
        reason="needs --run-integration flag and live Neo4j/LanceDB"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


@pytest.fixture
def python_adapter():
    """Return a configured Python language adapter."""
    return PythonAdapter()


@pytest.fixture
def typescript_adapter():
    """Return a configured TypeScript language adapter."""
    return TypeScriptAdapter()
