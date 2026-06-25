"""Tests for repo-root .env loading."""

from __future__ import annotations

import os

from context_engine.env_loader import load_repo_dotenv

_SAMPLE_URI_KEY = "SAMPLE_URI"
_SAMPLE_TOKEN_KEY = "SAMPLE_TOKEN"


def test_load_repo_dotenv_parses_spaced_keys(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"{_SAMPLE_URI_KEY} = bolt://localhost:7687\n{_SAMPLE_TOKEN_KEY} = secret\n# comment\nEMPTY=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(_SAMPLE_URI_KEY, raising=False)
    monkeypatch.delenv(_SAMPLE_TOKEN_KEY, raising=False)

    assert load_repo_dotenv(path=env_file) is True
    assert os.environ[_SAMPLE_URI_KEY] == "bolt://localhost:7687"
    assert os.environ[_SAMPLE_TOKEN_KEY] == "secret"


def test_load_repo_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{_SAMPLE_TOKEN_KEY}=from_file\n", encoding="utf-8")
    monkeypatch.setenv(_SAMPLE_TOKEN_KEY, "from_shell")

    load_repo_dotenv(path=env_file)

    assert os.environ[_SAMPLE_TOKEN_KEY] == "from_shell"


def test_load_repo_dotenv_missing_file(tmp_path):
    assert load_repo_dotenv(path=tmp_path / "missing.env") is False
