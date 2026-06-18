"""Shared pytest fixtures and configuration for evaluation harness."""

from pathlib import Path

import pytest

from sidecar.parser.adapters.python_adapter import PythonAdapter
from sidecar.parser.adapters.typescript_adapter import TypeScriptAdapter


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
def sample_project_path():
    """Return the path to the golden fixture project."""
    return Path(__file__).parent / "sample_project"


@pytest.fixture
def sample_questions_path():
    """Return the path to the golden questions file."""
    return Path(__file__).parent / "sample_project" / "questions.yaml"


@pytest.fixture
def python_adapter():
    """Return a configured Python language adapter."""
    return PythonAdapter()


@pytest.fixture
def typescript_adapter():
    """Return a configured TypeScript language adapter."""
    return TypeScriptAdapter()
