"""Disk hydration when Lance payloads are missing symbol code."""

from __future__ import annotations

from pathlib import Path

from context_engine.axis.context_builder import _hydrate_missing_symbol_code


class _FakeDB:
    def __init__(self, *, spans: dict, project_path: str):
        self._spans = spans
        self._project_path = project_path

    def get_symbol_spans_by_uids(self, uids, workspace_id="ws"):
        del workspace_id
        return {uid: self._spans[uid] for uid in uids if uid in self._spans}

    def get_index_manifest(self, workspace_id="ws"):
        del workspace_id
        return {"project_path": self._project_path}


def test_hydrate_missing_symbol_code_reads_from_disk(tmp_path: Path):
    source = tmp_path / "pkg" / "mod.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def helper():\n    return 1\n\n\ndef target():\n    return helper()\n",
        encoding="utf-8",
    )
    uid = "u:target"
    db = _FakeDB(
        spans={
            uid: {
                "name": "target",
                "file_path": "pkg/mod.py",
                "start_line": 5,
                "end_line": 6,
            }
        },
        project_path=str(tmp_path),
    )
    payloads = {uid: {"code": "", "name": "target", "file_path": "pkg/mod.py"}}

    out = _hydrate_missing_symbol_code(db, "ws", {uid}, payloads)

    assert "def target()" in (out[uid]["code"] or "")


def test_hydrate_skips_when_lance_already_has_code(tmp_path: Path):
    uid = "u:target"
    db = _FakeDB(
        spans={
            uid: {
                "name": "target",
                "file_path": "pkg/mod.py",
                "start_line": 1,
                "end_line": 1,
            }
        },
        project_path=str(tmp_path),
    )
    payloads = {uid: {"code": "from lance", "name": "target"}}

    out = _hydrate_missing_symbol_code(db, "ws", {uid}, payloads)

    assert out[uid]["code"] == "from lance"
