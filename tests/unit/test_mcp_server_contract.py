"""MCP server contract tests.

These stay DB-free: fake the engine/driver boundary and assert the MCP layer
keeps its own output and safety contracts before live Neo4j/LanceDB are involved.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

MCP_ROOT = Path(__file__).resolve().parents[2] / "mcp_server"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from surgical_context_mcp import server  # noqa: E402
from surgical_context_mcp.config import DEFAULT_TOKEN_BUDGET  # noqa: E402
from surgical_context_mcp.engine import AxisEngine, FileEntry, FileOutline  # noqa: E402


class _Result:
    def __init__(
        self,
        *,
        data: list[dict[str, Any]] | None = None,
        single: dict[str, Any] | None = None,
    ) -> None:
        self._data = data or []
        self._single = single

    def data(self) -> list[dict[str, Any]]:
        return self._data

    def single(self) -> dict[str, Any] | None:
        return self._single


class _Session:
    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def run(self, cypher: str, **params):
        self.owner.cypher = cypher
        self.owner.params = params
        self.owner.calls += 1
        return self.owner.result_for(cypher, params)


class _Driver:
    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def session(self) -> _Session:
        return _Session(self.owner)


class _PathDb:
    def __init__(self) -> None:
        self.driver = _Driver(self)
        self.calls = 0
        self.cypher = ""
        self.params: dict[str, Any] = {}

    def get_symbol_uid_by_name(self, name: str, _workspace_id: str) -> str:
        return f"uid-{name}"

    def get_symbol_uid_by_name_in_file(self, name: str, _path: str, _workspace_id: str) -> str:
        return f"uid-{name}"

    def result_for(self, _cypher: str, _params: dict[str, Any]) -> _Result:
        return _Result(single={"names": ["a", "b"], "rels": ["CALLS"]})


class _FileOutlineDb:
    def __init__(self, paths: list[str]) -> None:
        self.driver = _Driver(self)
        self.paths = paths
        self.calls = 0
        self.cypher = ""
        self.params: dict[str, Any] = {}

    def result_for(self, cypher: str, _params: dict[str, Any]) -> _Result:
        if "RETURN DISTINCT f.path AS file_path" in cypher:
            return _Result(data=[{"file_path": path} for path in self.paths])
        return _Result(data=[])


def test_path_query_is_workspace_scoped_to_symbols_and_relationships() -> None:
    engine = AxisEngine()
    fake_db = _PathDb()
    engine._db = fake_db

    result = engine.path("a", "b", "ws+axis_python_v1")

    assert result.found is True
    assert fake_db.params["workspace_id"] == "ws+axis_python_v1"
    assert "all(n IN nodes(p) WHERE n:Symbol)" in fake_db.cypher
    assert "coalesce(rel.workspace_id, $workspace_id) = $workspace_id" in fake_db.cypher
    assert "(:File {workspace_id: $workspace_id})-[:CONTAINS]->(n)" in fake_db.cypher


def test_file_outline_returns_ambiguity_instead_of_mixing_basename_matches() -> None:
    engine = AxisEngine()
    engine._db = _FileOutlineDb(["/repo/a/config.py", "/repo/b/config.py"])

    result = engine.file_outline("config.py", "ws+axis_python_v1")

    assert result.found is False
    assert result.ambiguous is True
    assert result.candidate_files == ["/repo/a/config.py", "/repo/b/config.py"]
    assert engine._db.calls == 1


def test_server_file_outline_marshals_ambiguity_and_bounds_limit(monkeypatch) -> None:
    class FakeEngine:
        seen_limit = 0

        def file_outline(self, _file_path: str, _workspace_id: str, *, limit: int):
            self.seen_limit = limit
            return FileOutline(
                requested_path="config.py",
                workspace_id="ws+axis_python_v1",
                found=False,
                ambiguous=True,
                candidate_files=["/repo/a/config.py", "/repo/b/config.py"],
            )

    fake = FakeEngine()
    monkeypatch.setattr(server, "_engine", fake)

    result = server.file_outline("config.py", limit=-1, workspace="ws")
    payload = result.structuredContent

    assert fake.seen_limit == 1
    assert payload["ok"] is False
    assert payload["found"] is False
    assert payload["ambiguous"] is True
    assert payload["candidate_files"] == ["/repo/a/config.py", "/repo/b/config.py"]


def test_server_list_files_bounds_negative_limit(monkeypatch) -> None:
    class FakeEngine:
        seen_limit = 0

        def list_files(
            self,
            _workspace_id: str,
            *,
            path_prefix: str | None,
            with_counts: bool,
            limit: int,
        ):
            self.seen_limit = limit
            return [FileEntry(path="/repo/app.py", symbols=3 if with_counts else 0)]

    fake = FakeEngine()
    monkeypatch.setattr(server, "_engine", fake)

    result = server.list_files(limit=-1)

    assert fake.seen_limit == 1
    assert result.structuredContent["ok"] is True
    assert result.structuredContent["files"][0]["path"] == "/repo/app.py"


def test_batch_rejects_oversized_op_list_before_dispatch() -> None:
    result = server.batch([{"tool": "list_files"}] * (server.MAX_BATCH_OPS + 1))

    assert result.structuredContent["ok"] is False
    assert "at most" in result.structuredContent["markdown"]


def test_ask_code_lean_detail_omits_symbols_but_keeps_markdown(monkeypatch) -> None:
    class FakeAsk:
        text = "### foo.py :: bar\n```python\npass\n```\n"
        files = ["/repo/foo.py"]
        symbols = [
            {
                "uid": "u1",
                "name": "bar",
                "file_path": "/repo/foo.py",
                "role": "entrypoint",
                "kind": "function",
                "depth": 0,
                "expansion_step": None,
                "relevance_score": 0.9,
                "utility_score": 0.9,
                "has_code": True,
                "start_line": 1,
                "end_line": 2,
            }
        ]
        intent = [("entrypoint", 0.9)]
        candidate_count = 3

    class FakeEngine:
        def available_roles(self):
            return {"entrypoint"}

        def ask(self, *_a, **_k):
            return FakeAsk()

    monkeypatch.setattr(server, "_engine", FakeEngine())
    monkeypatch.delenv("SURGICAL_CONTEXT_MCP_DETAIL", raising=False)

    lean = server.ask_code("how does bar work", detail="lean")
    payload = lean.structuredContent
    assert payload["detail"] == "lean"
    assert payload["symbol_count"] == 1
    assert payload["symbols"] == []
    assert payload["files"] == ["/repo/foo.py"]
    assert "pass" in payload["markdown"]
    assert lean.content[0].text == payload["markdown"]

    full = server.ask_code("how does bar work", detail="full")
    full_payload = full.structuredContent
    assert full_payload["detail"] == "full"
    assert len(full_payload["symbols"]) == 1
    assert full_payload["symbols"][0]["uid"] == "u1"


def test_ask_code_detail_env_defaults_to_full(monkeypatch) -> None:
    class FakeAsk:
        text = "x"
        files = []
        symbols = [
            {
                "uid": "u1",
                "name": "bar",
                "file_path": "/repo/foo.py",
                "role": "",
                "kind": "",
                "depth": 0,
                "expansion_step": None,
                "relevance_score": 0.0,
                "utility_score": 0.0,
                "has_code": False,
                "start_line": None,
                "end_line": None,
            }
        ]
        intent: list = []
        candidate_count = 1

    class FakeEngine:
        def available_roles(self):
            return set()

        def ask(self, *_a, **_k):
            return FakeAsk()

    monkeypatch.setattr(server, "_engine", FakeEngine())
    monkeypatch.setenv("SURGICAL_CONTEXT_MCP_DETAIL", "full")

    payload = server.ask_code("q").structuredContent
    assert payload["detail"] == "full"
    assert len(payload["symbols"]) == 1


def test_investigate_lean_omits_symbols_and_blast(monkeypatch) -> None:
    class FakeInv:
        context_text = "context body"
        files = ["/repo/a.py"]
        symbols = [
            {
                "uid": "u1",
                "name": "a",
                "file_path": "/repo/a.py",
                "role": "",
                "kind": "",
                "depth": 0,
                "expansion_step": None,
                "relevance_score": 0.0,
                "utility_score": 0.0,
                "has_code": True,
                "start_line": 1,
                "end_line": 2,
            }
        ]
        blast = [
            {
                "seed": "a",
                "name": "b",
                "file_path": "/repo/b.py",
                "depth": 1,
                "kind": "function",
            }
        ]
        intent = [("entrypoint", 0.5)]
        candidate_count = 2

    class FakeEngine:
        def investigate(self, *_a, **_k):
            return FakeInv()

    monkeypatch.setattr(server, "_engine", FakeEngine())
    monkeypatch.delenv("SURGICAL_CONTEXT_MCP_DETAIL", raising=False)

    payload = server.investigate("q", detail="lean").structuredContent
    assert payload["detail"] == "lean"
    assert payload["symbol_count"] == 1
    assert payload["blast_count"] == 1
    assert payload["symbols"] == []
    assert payload["blast"] == []
    assert "context body" in payload["markdown"]


def test_token_budget_default_is_shared_between_server_and_engine() -> None:
    assert inspect.signature(server.ask_code).parameters["token_budget"].default == (
        DEFAULT_TOKEN_BUDGET
    )
    assert inspect.signature(AxisEngine.ask).parameters["token_budget"].default == (
        DEFAULT_TOKEN_BUDGET
    )
