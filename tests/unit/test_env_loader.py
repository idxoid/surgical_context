"""Tests for repo-root .env loading."""

from __future__ import annotations

import os
from pathlib import Path

from context_engine.env_loader import load_repo_dotenv


def test_load_repo_dotenv_parses_spaced_keys(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "NEO4J_URI = bolt://localhost:7687\n"
        "NEO4J_PASSWORD = secret\n"
        "# comment\n"
        "EMPTY=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

    assert load_repo_dotenv(path=env_file) is True
    assert os.environ["NEO4J_URI"] == "bolt://localhost:7687"
    assert os.environ["NEO4J_PASSWORD"] == "secret"


def test_load_repo_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("NEO4J_PASSWORD=from_file\n", encoding="utf-8")
    monkeypatch.setenv("NEO4J_PASSWORD", "from_shell")

    load_repo_dotenv(path=env_file)

    assert os.environ["NEO4J_PASSWORD"] == "from_shell"


def test_load_repo_dotenv_missing_file(tmp_path):
    assert load_repo_dotenv(path=tmp_path / "missing.env") is False
