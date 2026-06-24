"""Tests for HTTP endpoint extraction and graph bridging."""

from __future__ import annotations

from pathlib import Path

from context_engine.indexer.http_endpoint import (
    combine_controller_path,
    endpoint_fingerprint,
    normalize_http_path,
)
from context_engine.parser.adapters.javascript_adapter import JavaScriptAdapter
from context_engine.parser.adapters.python_adapter import PythonAdapter
from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter


class TestHttpEndpointNormalization:
    def test_normalize_and_fingerprint(self):
        assert normalize_http_path("ask") == "/ask"
        assert combine_controller_path("api/cats", ":id") == "/api/cats/:id"
        assert endpoint_fingerprint("post", "/ask") == "POST:/ask"


class TestTypeScriptHttpEndpointExtraction:
    def test_context_engine_client_posts_ask(self):
        adapter = TypeScriptAdapter()
        source = Path("extension/src/context_engineClient.ts").read_text()
        path = "extension/src/context_engineClient.ts"
        from context_engine.parser.uid import (
            project_root_scope,
        )

        with project_root_scope("./", "ws"):
            symbols = adapter.extract_symbols(source, path)
            rows = adapter.extract_http_endpoints(source, path)
            ask_uid = next(symbol.uid for symbol in symbols if symbol.name == "ask")
        calls = [r for r in rows if r["role"] == "call" and r["path"] == "/ask"]
        assert len(calls) == 1
        assert calls[0]["method"] == "POST"
        assert calls[0]["site_uid"] == ask_uid

    def test_express_app_get_registers_handler(self):
        adapter = TypeScriptAdapter()
        source = """
function handler(req: any, res: any) {}

export function register(app: any) {
  app.get('/health', handler);
}
"""
        rows = adapter.extract_http_endpoints(source, "src/app.ts")
        assert any(
            r["role"] == "implement"
            and r["method"] == "GET"
            and r["path"] == "/health"
            and r["via"] == "app.get"
            for r in rows
        )

    def test_nestjs_controller_and_get(self):
        adapter = TypeScriptAdapter()
        source = """
@Controller('cats')
export class CatsController {
  @Get(':id')
  findOne() {}
}
"""
        rows = adapter.extract_http_endpoints(source, "src/cats.controller.ts")
        assert any(
            r["role"] == "implement" and r["method"] == "GET" and r["path"] == "/cats/:id"
            for r in rows
        )

    def test_fetch_template_literal_path(self):
        adapter = TypeScriptAdapter()
        source = """
export async function ping() {
  await fetch(`${getBaseUrl()}/ask/stream`, { method: 'POST' });
}
"""
        rows = adapter.extract_http_endpoints(source, "src/client.ts")
        assert any(r["method"] == "GET" and r["path"] == "/ask/stream" for r in rows)


class TestJavaScriptHttpEndpointExtraction:
    def test_express_app_get_registers_handler(self):
        adapter = JavaScriptAdapter()
        source = """
function handler(req, res) {}

export function register(app) {
  app.get('/health', handler);
}
"""
        symbols = adapter.extract_symbols(source, "src/app.js")
        rows = adapter.extract_http_endpoints(source, "src/app.js")
        handler_uid = next(symbol.uid for symbol in symbols if symbol.name == "handler")
        route = next(r for r in rows if r["role"] == "implement")
        assert route["method"] == "GET"
        assert route["path"] == "/health"
        assert route["via"] == "app.get"
        assert route["site_uid"] == handler_uid


class TestPythonHttpEndpointExtraction:
    def test_fastapi_post_route(self):
        adapter = PythonAdapter()
        source = """
from fastapi import FastAPI

app = FastAPI()

@app.post("/ask")
async def ask():
    return {}
"""
        rows = adapter.extract_http_endpoints(source, "context_engine/main.py")
        assert any(
            r["role"] == "implement" and r["method"] == "POST" and r["path"] == "/ask" for r in rows
        )

    def test_api_route_with_methods_kwarg(self):
        adapter = PythonAdapter()
        source = """
from fastapi import FastAPI

app = FastAPI()

@app.api_route("/items", methods=["GET", "POST"])
async def items():
    return {}
"""
        rows = adapter.extract_http_endpoints(source, "main.py")
        methods = {r["method"] for r in rows if r["path"] == "/items"}
        assert methods == {"GET", "POST"}


class TestNeo4jHttpEndpointLinker:
    def test_link_http_endpoints_connects_client_and_handler(self):
        from context_engine.database.neo4j_client import Neo4jClient

        class _FakeResult:
            def consume(self):
                return None

        class _FakeTx:
            def __init__(self):
                self.queries: list[tuple[str, dict]] = []

            def run(self, query, **params):
                self.queries.append((query, params))
                return _FakeResult()

        class _FakeSession:
            def __init__(self):
                self.tx = _FakeTx()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute_write(self, fn, facts, workspace_id):
                fn(self.tx, facts, workspace_id)

            def run(self, *args, **kwargs):
                return _FakeResult()

        class _FakeDriver:
            def __init__(self):
                self.last_session: _FakeSession | None = None

            def session(self):
                self.last_session = _FakeSession()
                return self.last_session

        driver = _FakeDriver()
        db = Neo4jClient.__new__(Neo4jClient)
        db.driver = driver
        facts = [
            {
                "site_uid": "client",
                "method": "POST",
                "path": "/ask",
                "role": "call",
                "via": "post",
            },
            {
                "site_uid": "handler",
                "method": "POST",
                "path": "/ask",
                "role": "implement",
                "via": "@app.post",
            },
        ]
        db.link_http_endpoints(facts, workspace_id="ws")
        assert driver.last_session is not None
        query_text = "\n".join(q for q, _ in driver.last_session.tx.queries)
        assert "CALLS_ENDPOINT" in query_text
        assert "IMPLEMENTS_ENDPOINT" in query_text
        assert "POST:/ask" in str(driver.last_session.tx.queries)
