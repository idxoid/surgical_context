from sidecar.indexer.ts_http_route_hints import TsHttpRouteHintsIndexer


def test_scan_python_routes_finds_fastapi_handlers(tmp_path):
    main_py = tmp_path / "sidecar" / "main.py"
    main_py.parent.mkdir(parents=True)
    main_py.write_text(
        """
@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    return {}

@app.get("/health")
def health():
    return {"ok": True}
""",
        encoding="utf-8",
    )

    routes = TsHttpRouteHintsIndexer(db=None, project_path=str(tmp_path))._scan_python_routes()

    assert routes["/ask"].handler_name == "ask"
    assert routes["/health"].handler_name == "health"


def test_scan_ts_http_paths_extracts_literal_and_template_suffix():
    source = """
export const SidecarClient = {
  ask() {
    return post('/ask', { symbol: null });
  },
  health() {
    return fetch(`${normalizeBaseUrl(baseUrl)}/health`);
  },
};
"""
    paths = TsHttpRouteHintsIndexer._scan_ts_http_paths(source)
    assert "/ask" in {path for path, _, _ in paths}
    assert "/health" in {path for path, _, _ in paths}


def test_extract_exported_object_api_collapses_nested_methods():
    from sidecar.parser.adapters.typescript_adapter import TypeScriptAdapter

    adapter = TypeScriptAdapter()
    source = """
export const SidecarClient = {
  async ask(symbol: string | undefined, question: string) {
    return post('/ask', { symbol, question });
  },
  health() {
    return fetch(`${getBaseUrl()}/health`);
  },
};
"""
    symbols = adapter.extract_symbols(source, "extension/src/sidecarClient.ts")
    names = {symbol.name for symbol in symbols}
    sidecar = next(symbol for symbol in symbols if symbol.name == "SidecarClient")

    assert names == {"SidecarClient"}
    assert sidecar.kind == "object_api"
    assert sidecar.end_line - sidecar.start_line >= 5

    calls = adapter.extract_calls_from_source(source, "extension/src/sidecarClient.ts")
    sidecar_calls = [call for call in calls if call.get("caller_uid") == sidecar.uid]
    assert {call["callee_name"] for call in sidecar_calls} >= {"post", "fetch"}
