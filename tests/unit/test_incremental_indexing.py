"""Unit tests for incremental indexing with hash-based skip logic."""

import json
from unittest.mock import MagicMock, patch

from context_engine.database.neo4j_client import Neo4jClient, _import_row
from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE
from context_engine.indexer.code import index_file
from context_engine.indexer.fast.extractor import ExtractedFile
from context_engine.indexer.fast.pipeline import (
    FileDiff,
    _apply_graph,
    _embed_phase,
    _NullReporter,
    _property_api_phase,
    _symbol_alias_phase,
    _type_reference_phase,
)
from context_engine.parser.protocol import ImportEdge, SymbolMetadata


def _symbol(uid: str, content_hash: str, start_line: int = 1, end_line: int = 2):
    return SymbolMetadata(
        uid=uid,
        name=uid,
        kind="function",
        start_line=start_line,
        end_line=end_line,
        content_hash=content_hash,
        file_path="/test.py",
        qualified_name=uid,
        signature=f"def {uid}()",
        signature_hash=f"sig-{uid}",
        signature_status="resolved",
        language="python",
    )


class TestIncrementalIndexing:
    """Test hash-based file skipping and incremental re-indexing."""

    def test_get_file_hashes_returns_empty_for_empty_input(self):
        """Test that empty file list returns empty dict."""
        db = Neo4jClient("bolt://localhost:7687", "neo4j", "password")
        result = db.get_file_hashes([])
        assert result == {}

    def test_get_file_hashes_with_mock_session(self):
        """Test get_file_hashes logic with mocked session."""
        db = Neo4jClient("bolt://localhost:7687", "neo4j", "password")
        mock_session = MagicMock()

        # Create mock records that support dictionary-style access
        mock_records = [
            {"path": "/file1.py", "hash": "hash1"},
            {"path": "/file2.py", "hash": "hash2"},
        ]

        with patch.object(db.driver, "session") as mock_ctx:
            mock_ctx.return_value.__enter__.return_value = mock_session
            mock_session.run.return_value = mock_records
            result = db.get_file_hashes(["/file1.py", "/file2.py"])
            assert result == {"/file1.py": "hash1", "/file2.py": "hash2"}

    def test_repository_profile_round_trips_through_workspace_metadata(self):
        db = Neo4jClient("bolt://localhost:7687", "neo4j", "password")
        mock_session = MagicMock()
        profile = {
            "schema_version": 1,
            "workspace_id": "local/repo@main",
            "indexability": "medium",
        }

        with patch.object(db.driver, "session") as mock_ctx:
            mock_ctx.return_value.__enter__.return_value = mock_session
            db.save_repository_profile(profile, workspace_id="local/repo@main")
            call_args = mock_session.run.call_args
            assert "repository_profile_json" in call_args.args[0]
            assert json.loads(call_args.kwargs["profile_json"]) == profile

        mock_session = MagicMock()
        mock_session.run.return_value.single.return_value = {"profile_json": json.dumps(profile)}
        with patch.object(db.driver, "session") as mock_ctx:
            mock_ctx.return_value.__enter__.return_value = mock_session
            assert db.get_repository_profile("local/repo@main") == profile

    def test_delete_symbols_for_file_with_mock_session(self):
        """Test that delete_symbols_for_file sends correct Cypher."""
        db = Neo4jClient("bolt://localhost:7687", "neo4j", "password")
        mock_session = MagicMock()

        with patch.object(db.driver, "session") as mock_ctx:
            mock_ctx.return_value.__enter__.return_value = mock_session
            db.delete_symbols_for_file("/test.py")
            mock_session.run.assert_called_once()
            call_args = mock_session.run.call_args
            assert "DETACH DELETE s" in call_args[0][0]
            assert call_args[1] == {
                "path": "/test.py",
                "workspace_id": "local/surgical_context@main",
            }

    def test_upsert_nodes_batches_symbols_with_unwind(self):
        tx = MagicMock()

        Neo4jClient._upsert_nodes(
            tx,
            "/test.py",
            "file-hash",
            [_symbol("one", "hash-1"), _symbol("two", "hash-2")],
            "acme/repo@main",
        )

        assert tx.run.call_count == 2
        query = tx.run.call_args.args[0]
        params = tx.run.call_args.kwargs
        assert "UNWIND $symbols AS symbol" in query
        assert params["workspace_id"] == "acme/repo@main"
        assert len(params["symbols"]) == 2

    def test_create_import_relations_batches_resolved_rows_with_unwind(self):
        # _create_import_relations now expects rows pre-resolved by link_imports
        # (Python-side suffix lookup against the workspace File.path set) and
        # matches by exact target_path — the old `ENDS WITH any($suffixes)` scan
        # was O(imports × files) and dominated graph time on real-repo reindex.
        tx = MagicMock()

        Neo4jClient._create_import_relations(
            tx,
            [
                {
                    "source_file": "/repo/a.py",
                    "target_path": "/repo/pkg/module_a.py",
                    "import_type": "direct",
                },
                {
                    "source_file": "/repo/a.py",
                    "target_path": "/repo/pkg/module_b.py",
                    "import_type": "direct",
                },
            ],
            "acme/repo@main",
        )

        tx.run.assert_called_once()
        query = tx.run.call_args.args[0]
        params = tx.run.call_args.kwargs
        assert "UNWIND $imports AS imp" in query
        assert "(target:File {path: imp.target_path" in query.replace("\n", " ")
        assert "ENDS WITH" not in query
        assert params["workspace_id"] == "acme/repo@main"
        assert len(params["imports"]) == 2

    def test_import_rows_include_cross_language_module_suffixes(self):
        row = _import_row(ImportEdge("/repo/lib/express.js", "./response", "relative"))

        assert row["source_file"] == "/repo/lib/express.js"
        assert row["import_type"] == "relative"
        assert "/repo/lib/response.py" in row["path_suffixes"]
        assert "/repo/lib/response.js" in row["path_suffixes"]
        assert "/repo/lib/response.ts" in row["path_suffixes"]
        assert "/repo/lib/response/index.tsx" in row["path_suffixes"]

    def test_package_import_rows_remain_module_suffix_based(self):
        row = _import_row(ImportEdge("/repo/pkg/a.py", "pkg.module", "direct"))

        assert "/pkg/module.py" in row["path_suffixes"]
        assert "/pkg/module.js" in row["path_suffixes"]

    def test_scoped_package_import_rows_include_monorepo_package_src_suffixes(self):
        row = _import_row(
            ImportEdge(
                "/repo/packages/runtime-dom/src/index.ts", "@vue/runtime-core", "from_package"
            )
        )

        assert "/@vue/runtime-core/index.ts" in row["path_suffixes"]
        assert "/packages/runtime-core/src/index.ts" in row["path_suffixes"]

    def test_scoped_package_subpath_import_rows_include_package_src_suffixes(self):
        row = _import_row(
            ImportEdge(
                "/repo/packages/compiler-dom/src/index.ts",
                "@vue/compiler-core/runtimeHelpers",
                "from_package",
            )
        )

        assert "/packages/compiler-core/src/runtimeHelpers.ts" in row["path_suffixes"]

    def test_fast_graph_phase_upserts_all_nodes_before_linking_edges(self):
        calls = []

        class FakeDb:
            def upsert_file_structure(self, file_path, file_hash, symbols, workspace_id):
                calls.append(("upsert", file_path))

            def prune_symbols_for_file(self, file_path, keep_uids, workspace_id):
                calls.append(("prune", file_path))

            def clear_outgoing_symbol_edges(self, symbol_uids, workspace_id):
                calls.append(("clear", tuple(symbol_uids)))

            def link_calls(self, linked_calls, workspace_id):
                calls.append(("calls", linked_calls[0]["callee_name"]))

            def delete_imports_for_file(self, file_path, workspace_id):
                calls.append(("delete_imports", file_path))

            def link_imports(self, imports, workspace_id):
                calls.append(("imports", imports[0].target_module_name))

            def link_inheritance(self, inheritance_edges, workspace_id):
                calls.append(("inheritance", len(inheritance_edges)))

        first = FileDiff(
            extracted=ExtractedFile(
                "/repo/a.js",
                "",
                "hash-a",
                [_symbol("a", "ha")],
                [{"caller_uid": "a", "callee_name": "b"}],
                [ImportEdge("/repo/a.js", "./b", "relative")],
                [],
            ),
            current_uids=["a"],
            changed_uids=["a"],
            changed_symbols=[_symbol("a", "ha")],
        )
        second = FileDiff(
            extracted=ExtractedFile(
                "/repo/b.js",
                "",
                "hash-b",
                [_symbol("b", "hb")],
                [],
                [],
                [],
            ),
            current_uids=["b"],
            changed_uids=["b"],
            changed_symbols=[_symbol("b", "hb")],
        )

        _apply_graph([first, second], FakeDb(), "acme/repo@main", _NullReporter())

        edge_start = min(i for i, call in enumerate(calls) if call[0] == "clear")
        upsert_end = max(i for i, call in enumerate(calls) if call[0] == "upsert")
        assert upsert_end < edge_start

    def test_type_reference_phase_uses_language_adapter_for_typescript(self):
        linked = []
        source = """
export interface ConfigureStoreOptions<S = unknown> {
  reducer: Reducer<S>
}

export type EnhancedStore<S = unknown> = Store<S>

export function configureStore<S>(
  options: ConfigureStoreOptions<S>,
): EnhancedStore<S> {
  return createStore(options.reducer)
}
"""

        class FakeDb:
            def link_type_references(self, references, workspace_id):
                linked.extend(references)

        diff = FileDiff(
            extracted=ExtractedFile(
                "/repo/configureStore.ts",
                source,
                "hash",
                [],
                [],
                [],
                [],
            ),
        )

        count = _type_reference_phase(
            [diff],
            FakeDb(),
            "acme/repo@main",
            _NullReporter(),
            project_path="/repo",
        )

        assert count > 0
        assert {ref["type_name"] for ref in linked} >= {
            "ConfigureStoreOptions",
            "EnhancedStore",
        }

    def test_symbol_alias_phase_uses_javascript_adapter_for_commonjs_exports(self, tmp_path):
        linked = []
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "response.js").write_text("var res = {};\nmodule.exports = res;\n")
        source = """
var res = require('./response');
exports.response = res;
"""

        class FakeDb:
            def link_symbol_references(self, references, workspace_id):
                linked.extend(references)
                touched = {str(ref["source_uid"]) for ref in references}
                return len(references), touched

        diff = FileDiff(
            extracted=ExtractedFile(
                str(lib / "express.js"),
                source,
                "hash",
                [],
                [],
                [],
                [],
            ),
        )

        count, touched = _symbol_alias_phase(
            [diff],
            FakeDb(),
            "acme/repo@main",
            _NullReporter(),
            project_path=str(tmp_path),
        )

        assert count == len(linked)
        assert touched
        assert any(
            ref["source_name"] == "response"
            and ref["target_name"] == "res"
            and ref["target_qualified_name"] == "lib.response.res"
            for ref in linked
        )

    def test_property_api_phase_links_javascript_property_methods(self, tmp_path):
        linked = []
        source = """
var res = Object.create(proto);
res.status = function status(code) {
  return this;
};
"""

        class FakeDb:
            def link_symbol_api_edges(self, edges, workspace_id):
                linked.extend(edges)
                touched = {edge.class_uid for edge in edges} | {edge.method_uid for edge in edges}
                return len(edges), touched

        diff = FileDiff(
            extracted=ExtractedFile(
                str(tmp_path / "response.js"),
                source,
                "hash",
                [],
                [],
                [],
                [],
            ),
        )

        count, touched = _property_api_phase(
            [diff],
            FakeDb(),
            "acme/repo@main",
            _NullReporter(),
            project_path=str(tmp_path),
        )

        assert count == 1
        assert touched
        assert linked[0].edge_type == "HAS_API"

    def test_fast_embed_phase_adds_axis_payload_for_axis_python_profile(self, tmp_path):
        source = """
@app.task(name="jobs.run")
def run(x: int):
    return {"x": x}
"""
        path = tmp_path / "pkg" / "tasks.py"
        path.parent.mkdir()
        path.write_text(source, encoding="utf-8")
        symbol = SymbolMetadata(
            uid="run-uid-from-parser",
            name="run",
            kind="function",
            start_line=2,
            end_line=4,
            content_hash="hash",
            file_path=str(path),
            qualified_name="pkg.tasks.run",
            signature="run(int)->_",
            signature_hash="sig",
            signature_status="resolved",
            language="python",
        )
        diff = FileDiff(
            extracted=ExtractedFile(str(path), source, "hash", [symbol], [], [], []),
            current_uids=[symbol.uid],
            changed_uids=[symbol.uid],
            changed_symbols=[symbol],
        )

        class FakeLance:
            index_profile_name = AXIS_PYTHON_V1_PROFILE

            def __init__(self):
                self.rows = []

            def upsert_symbol_embeddings(self, symbols, *, workspace_id, progress_callback=None):
                self.rows = symbols

        lance = FakeLance()

        encoded, removed = _embed_phase(
            [diff],
            lance,
            "local/repo@main+axis_python_v1",
            _NullReporter(),
            project_path=str(tmp_path),
        )

        assert encoded == 1
        assert removed == 0
        assert {"callable_body", "decorator_application", "return_exit"} <= set(
            lance.rows[0]["cfg_bits"]
        )
        assert {"parameter_input", "collection_assembly", "return_output"} <= set(
            lance.rows[0]["dfg_bits"]
        )
        assert {
            "function_def",
            "parameter_decl",
            "annotation",
            "decorator_attachment",
            "literal_shape",
        } <= set(lance.rows[0]["struct_bits"])
        assert "axis_evidence_json" in lance.rows[0]
        assert json.loads(lance.rows[0]["axis_container_kinds_json"]) == []
        assert json.loads(lance.rows[0]["axis_contracts_json"]) == []

    def test_fast_embed_phase_reuses_axis_facts_from_parse(self, tmp_path):
        source = """
@app.task(name="jobs.run")
def run(x: int):
    return {"x": x}
"""
        path = tmp_path / "pkg" / "tasks.py"
        path.parent.mkdir()
        path.write_text(source, encoding="utf-8")
        from context_engine.indexer.fast.extractor import FastExtractor

        extracted = FastExtractor(
            project_root=str(tmp_path),
            include_axis_facts=True,
        ).extract_all(str(path))
        assert extracted is not None
        assert extracted.axis_facts is not None
        run_symbol = next(s for s in extracted.symbols if s.name == "run")
        diff = FileDiff(
            extracted=extracted,
            current_uids=[run_symbol.uid],
            changed_uids=[run_symbol.uid],
            changed_symbols=[run_symbol],
        )

        class FakeLance:
            index_profile_name = AXIS_PYTHON_V1_PROFILE

            def upsert_symbol_embeddings(self, symbols, *, workspace_id, progress_callback=None):
                self.rows = symbols

        lance = FakeLance()

        encoded, removed = _embed_phase(
            [diff],
            lance,
            "local/repo@main+axis_python_v1",
            _NullReporter(),
            project_path=str(tmp_path),
        )

        assert encoded == 1
        assert removed == 0
        assert {"callable_body", "decorator_application", "return_exit"} <= set(
            lance.rows[0]["cfg_bits"]
        )

    def test_fast_embed_phase_adds_axis_container_kind_payload(self, tmp_path):
        source = """
class Settings:
    host: str = "localhost"
    port: int = 5432
"""
        path = tmp_path / "settings.py"
        path.write_text(source, encoding="utf-8")
        symbol = SymbolMetadata(
            uid="settings-class-uid-from-parser",
            name="Settings",
            kind="class",
            start_line=2,
            end_line=4,
            content_hash="hash",
            file_path=str(path),
            qualified_name="settings.Settings",
            signature="class Settings",
            signature_hash="sig",
            signature_status="resolved",
            language="python",
        )
        diff = FileDiff(
            extracted=ExtractedFile(str(path), source, "hash", [symbol], [], [], []),
            current_uids=[symbol.uid],
            changed_uids=[symbol.uid],
            changed_symbols=[symbol],
        )

        class FakeLance:
            index_profile_name = AXIS_PYTHON_V1_PROFILE

            def __init__(self):
                self.rows = []

            def upsert_symbol_embeddings(self, symbols, *, workspace_id, progress_callback=None):
                self.rows = symbols

        lance = FakeLance()

        _embed_phase(
            [diff],
            lance,
            "local/repo@main+axis_python_v1",
            _NullReporter(),
            project_path=str(tmp_path),
        )

        matches = json.loads(lance.rows[0]["axis_container_kinds_json"])
        contracts = json.loads(lance.rows[0]["axis_contracts_json"])

        assert {match["kind"] for match in matches} == {"config_carrier", "data_model"}
        assert all(match["evidence_bits"] for match in matches)
        assert {contract["contract"] for contract in contracts} == {
            "configuration_carrier",
            "data_shape_declaration",
        }

    def test_fast_embed_phase_adds_typescript_metadata_bridge_payload(self, tmp_path):
        source = """
export class GuardsContextCreator {
  create() {
    return this.createContext();
  }
}
"""
        path = tmp_path / "guards-context-creator.ts"
        path.write_text(source, encoding="utf-8")
        symbol = SymbolMetadata(
            uid="create-uid",
            name="create",
            kind="function",
            start_line=3,
            end_line=5,
            content_hash="hash",
            file_path=str(path),
            qualified_name="guards-context-creator.GuardsContextCreator.create",
            signature="create()->_",
            signature_hash="sig",
            signature_status="resolved",
            language="typescript",
        )
        diff = FileDiff(
            extracted=ExtractedFile(str(path), source, "hash", [symbol], [], [], []),
            current_uids=[symbol.uid],
            changed_uids=[symbol.uid],
            changed_symbols=[symbol],
        )

        class FakeLance:
            index_profile_name = AXIS_PYTHON_V1_PROFILE

            def __init__(self):
                self.rows = []

            def upsert_symbol_embeddings(self, symbols, *, workspace_id, progress_callback=None):
                self.rows = symbols

        class BridgeProbe:
            def metadata_bridge_keys(self, symbol_uid):
                return ("packages.common.constants.GUARDS_METADATA",)

            def outgoing_kind_edges(self, symbol_uid, kinds):
                return 0

            def library_marker_kinds(self, symbol_uid):
                return set()

            def caller_package_dispersion(self, symbol_uid):
                return 0.0

            def is_cfg_driver(self, symbol_uid):
                return False

            def outgoing_handles_count(self, symbol_uid):
                return 0

            def outgoing_injects_count(self, symbol_uid):
                return 0

            def peer_container_kinds_for(self, qualified_name_prefix):
                return set()

            def is_event_signal(self, symbol_uid):
                return False

        lance = FakeLance()

        _embed_phase(
            [diff],
            lance,
            "local/repo@main+axis_python_v1",
            _NullReporter(),
            project_path=str(tmp_path),
            graph_probe=BridgeProbe(),
        )

        row = lance.rows[0]
        assert {"callable_body"} <= set(row["cfg_bits"])
        assert {"callable_value"} <= set(row["dfg_bits"])
        assert {"function_def"} <= set(row["struct_bits"])
        matches = json.loads(row["axis_container_kinds_json"])
        contracts = json.loads(row["axis_contracts_json"])
        assert {match["kind"] for match in matches} == {"metadata_carrier"}
        assert {contract["contract"] for contract in contracts} == {"metadata_key_roundtrip"}
        assert row["container_kinds"] == ["metadata_carrier"]

    def test_fast_embed_phase_uses_graph_probe_for_marker_only_container_kind(self, tmp_path):
        source = "def run():\n    return 1\n"
        path = tmp_path / "routes.py"
        path.write_text(source, encoding="utf-8")
        symbol = SymbolMetadata(
            uid="run-uid-from-parser",
            name="run",
            kind="function",
            start_line=1,
            end_line=2,
            content_hash="hash",
            file_path=str(path),
            qualified_name="routes.run",
            signature="run()->_",
            signature_hash="sig",
            signature_status="resolved",
            language="python",
        )
        diff = FileDiff(
            extracted=ExtractedFile(str(path), source, "hash", [symbol], [], [], []),
            current_uids=[symbol.uid],
            changed_uids=[symbol.uid],
            changed_symbols=[symbol],
        )

        class FakeLance:
            index_profile_name = AXIS_PYTHON_V1_PROFILE

            def __init__(self):
                self.rows = []

            def upsert_symbol_embeddings(self, symbols, *, workspace_id, progress_callback=None):
                self.rows = symbols

        class MarkerProbe:
            def outgoing_kind_edges(self, symbol_uid, kinds):
                return 0

            def library_marker_kinds(self, symbol_uid):
                return {"web_route_register"}

            def caller_package_dispersion(self, symbol_uid):
                return 0.0

            def is_cfg_driver(self, symbol_uid):
                return False

            def outgoing_handles_count(self, symbol_uid):
                return 0

            def outgoing_injects_count(self, symbol_uid):
                return 0

            def peer_container_kinds_for(self, qualified_name_prefix):
                return set()

            def is_event_signal(self, symbol_uid):
                return False

        lance = FakeLance()

        _embed_phase(
            [diff],
            lance,
            "local/repo@main+axis_python_v1",
            _NullReporter(),
            project_path=str(tmp_path),
            graph_probe=MarkerProbe(),
        )

        matches = json.loads(lance.rows[0]["axis_container_kinds_json"])

        assert [match["kind"] for match in matches] == ["web_route_register"]
        assert matches[0]["evidence_probes"] == ["library_marker:web_route_register"]
        assert json.loads(lance.rows[0]["axis_contracts_json"]) == []
        assert list(lance.rows[0]["container_kinds"]) == ["web_route_register"]

    def test_fast_embed_phase_leaves_legacy_symbol_rows_without_axis_payload(self, tmp_path):
        source = "def run():\n    return 1\n"
        path = tmp_path / "tasks.py"
        path.write_text(source, encoding="utf-8")
        symbol = SymbolMetadata(
            uid="run",
            name="run",
            kind="function",
            start_line=1,
            end_line=2,
            content_hash="hash",
            file_path=str(path),
            qualified_name="tasks.run",
            signature="run()->_",
            signature_hash="sig",
            signature_status="resolved",
            language="python",
        )
        diff = FileDiff(
            extracted=ExtractedFile(str(path), source, "hash", [symbol], [], [], []),
            current_uids=[symbol.uid],
            changed_uids=[symbol.uid],
            changed_symbols=[symbol],
        )

        class FakeLance:
            index_profile_name = "legacy"

            def __init__(self):
                self.rows = []

            def upsert_symbol_embeddings(self, symbols, *, workspace_id, progress_callback=None):
                self.rows = symbols

        lance = FakeLance()

        _embed_phase([diff], lance, "local/repo@main", _NullReporter(), project_path=str(tmp_path))

        assert "cfg_bits" not in lance.rows[0]
        assert "axis_evidence_json" not in lance.rows[0]
        assert "axis_container_kinds_json" not in lance.rows[0]
        assert "axis_contracts_json" not in lance.rows[0]

    def test_hash_skip_gate_with_unchanged_file(self):
        """Test that unchanged files are correctly identified."""
        stored_hashes = {"/file1.py": "abc123", "/file2.py": "def456"}
        current_hashes = {"/file1.py": "abc123", "/file2.py": "def456"}

        changed_files = [p for p in current_hashes if current_hashes[p] != stored_hashes.get(p)]
        assert changed_files == []

    def test_hash_skip_gate_with_changed_file(self):
        """Test that changed files are correctly identified."""
        stored_hashes = {"/file1.py": "abc123", "/file2.py": "def456"}
        current_hashes = {"/file1.py": "xyz789", "/file2.py": "def456"}

        changed_files = [p for p in current_hashes if current_hashes[p] != stored_hashes.get(p)]
        assert changed_files == ["/file1.py"]

    def test_hash_skip_gate_with_new_file(self):
        """Test that new files (not in stored hashes) are included in changed."""
        stored_hashes = {"/file1.py": "abc123"}
        current_hashes = {"/file1.py": "abc123", "/file2.py": "new_hash"}

        changed_files = [p for p in current_hashes if current_hashes[p] != stored_hashes.get(p)]
        assert changed_files == ["/file2.py"]

    def test_hash_skip_gate_with_mixed_scenario(self):
        """Test skip gate with unchanged, changed, and new files."""
        stored_hashes = {
            "/file1.py": "hash1",
            "/file2.py": "hash2",
            "/file3.py": "hash3",
        }
        current_hashes = {
            "/file1.py": "hash1",  # unchanged
            "/file2.py": "hash2_new",  # changed
            "/file4.py": "hash4",  # new (not in stored)
        }

        changed_files = [p for p in current_hashes if current_hashes[p] != stored_hashes.get(p)]
        assert set(changed_files) == {"/file2.py", "/file4.py"}

    def test_sha256_hash_is_deterministic(self):
        """Test that sha256 hash is deterministic for same content."""
        import hashlib

        content = b"def test(): pass"
        hash1 = hashlib.sha256(content).hexdigest()
        hash2 = hashlib.sha256(content).hexdigest()
        assert hash1 == hash2
        # Should be a 64-character hex string (256 bits / 4 bits per hex char)
        assert len(hash1) == 64

    def test_sha256_hash_differs_for_different_content(self):
        """Test that different content produces different hashes."""
        import hashlib

        hash1 = hashlib.sha256(b"content1").hexdigest()
        hash2 = hashlib.sha256(b"content2").hexdigest()
        assert hash1 != hash2

    def test_index_file_upserts_only_changed_symbols(self, tmp_path, monkeypatch):
        """Changed/new symbols are upserted; unchanged symbols are preserved."""

        class FakeDb:
            def __init__(self):
                self.upserted = []
                self.pruned_keep = []
                self.cleared_edges = []
                self.linked_calls = []
                self.deleted_imports = []
                self.deleted_http_endpoints = []
                self.linked_http_endpoints = []

            def get_symbol_index_for_file(self, file_path, workspace_id):
                return {
                    "unchanged": {"hash": "same", "start_line": 1, "end_line": 2},
                    "changed": {"hash": "old", "start_line": 3, "end_line": 4},
                    "removed": {"hash": "gone", "start_line": 5, "end_line": 6},
                }

            def upsert_file_structure(self, file_path, file_hash, symbols, workspace_id):
                self.upserted = [s.uid for s in symbols]

            def prune_symbols_for_file(self, file_path, keep_uids, workspace_id):
                self.pruned_keep = keep_uids

            def clear_outgoing_symbol_edges(self, symbol_uids, workspace_id):
                self.cleared_edges = symbol_uids

            def link_calls(self, calls, workspace_id):
                self.linked_calls = calls

            def delete_imports_for_file(self, file_path, workspace_id):
                self.deleted_imports.append(file_path)

            def delete_http_endpoints_for_file(self, file_path, workspace_id):
                self.deleted_http_endpoints.append(file_path)

            def link_http_endpoints(self, facts, workspace_id):
                self.linked_http_endpoints = facts

            def link_imports(self, imports, workspace_id):
                raise AssertionError("No imports expected")

            def link_inheritance(self, inheritance_edges, workspace_id):
                raise AssertionError("No inheritance expected")

        class FakeLance:
            def __init__(self):
                self.upserted = []
                self.deleted = []

            def upsert_symbol_embeddings(self, symbols):
                self.upserted = [s["uid"] for s in symbols]

            def delete_symbol_embeddings(self, uids):
                self.deleted = uids

        class FakeExtractor:
            def extract(self, file_path):
                return [
                    _symbol("unchanged", "same", 1, 2),
                    _symbol("changed", "new", 3, 4),
                    _symbol("new", "brand-new", 5, 6),
                ]

            def extract_calls(self, file_path):
                return [{"caller_uid": "changed", "callee_name": "helper"}]

            def extract_imports(self, file_path):
                return []

            def extract_http_endpoints(self, file_path):
                return [
                    {
                        "site_uid": "changed",
                        "method": "POST",
                        "path": "/ask",
                        "role": "call",
                    }
                ]

            def extract_inheritance(self, file_path):
                return []

        rebuilt = []

        class FakeAffectsIndexer:
            def __init__(self, db):
                self.db = db

            def rebuild_affects(self, uids, workspace_id):
                rebuilt.extend(uids)

        monkeypatch.setattr("context_engine.indexer.affects.AFFECTSIndexer", FakeAffectsIndexer)
        source_file = tmp_path / "test.py"
        source_file.write_text(
            "def unchanged():\n    pass\ndef changed():\n    return 1\ndef new():\n    return 2\n",
            encoding="utf-8",
        )
        db = FakeDb()
        lance = FakeLance()

        index_file(str(source_file), db, lance, FakeExtractor(), workspace_id="acme/repo@main")

        assert db.upserted == ["changed", "new"]
        assert db.pruned_keep == ["unchanged", "changed", "new"]
        assert db.cleared_edges == ["changed", "new"]
        assert db.linked_calls == [{"caller_uid": "changed", "callee_name": "helper"}]
        assert db.deleted_imports == [str(source_file)]
        assert db.deleted_http_endpoints == [str(source_file)]
        assert db.linked_http_endpoints == [
            {
                "site_uid": "changed",
                "method": "POST",
                "path": "/ask",
                "role": "call",
            }
        ]
        assert lance.upserted == ["changed", "new"]
        assert lance.deleted == ["removed"]
        assert rebuilt == ["changed", "new"]

    def test_index_file_skips_embeddings_when_symbols_unchanged(self, tmp_path, monkeypatch):
        """Unchanged symbols skip row/vector writes but refresh file-scoped edges."""

        class FakeDb:
            def __init__(self):
                self.upserted = None
                self.cleared_edges = None

            def get_symbol_index_for_file(self, file_path, workspace_id):
                return {"unchanged": {"hash": "same", "start_line": 1, "end_line": 2}}

            def upsert_file_structure(self, file_path, file_hash, symbols, workspace_id):
                self.upserted = [s.uid for s in symbols]

            def prune_symbols_for_file(self, file_path, keep_uids, workspace_id):
                self.keep_uids = keep_uids

            def clear_outgoing_symbol_edges(self, symbol_uids, workspace_id):
                self.cleared_edges = symbol_uids

            def link_calls(self, calls, workspace_id):
                self.linked_calls = calls

            def delete_imports_for_file(self, file_path, workspace_id):
                self.deleted_imports = True

        class FakeLance:
            def upsert_symbol_embeddings(self, symbols):
                self.upserted = symbols

            def delete_symbol_embeddings(self, uids):
                self.deleted = uids

        class FakeExtractor:
            def extract(self, file_path):
                return [_symbol("unchanged", "same", 1, 2)]

            def extract_calls(self, file_path):
                return [{"caller_uid": "unchanged", "callee_name": "helper"}]

            def extract_imports(self, file_path):
                return []

            def extract_inheritance(self, file_path):
                return []

        monkeypatch.setattr(
            "context_engine.indexer.affects.AFFECTSIndexer",
            lambda db: (_ for _ in ()).throw(AssertionError("AFFECTS should not rebuild")),
        )
        source_file = tmp_path / "test.py"
        source_file.write_text("def unchanged():\n    pass\n", encoding="utf-8")
        db = FakeDb()
        lance = FakeLance()

        index_file(str(source_file), db, lance, FakeExtractor(), workspace_id="acme/repo@main")

        assert db.upserted == []
        assert db.keep_uids == ["unchanged"]
        assert db.cleared_edges == ["unchanged"]
        assert db.linked_calls == [{"caller_uid": "unchanged", "callee_name": "helper"}]
        assert db.deleted_imports is True
        assert not getattr(lance, "upserted", None)
        assert not getattr(lance, "deleted", None)

    def test_index_file_emits_axis_payload_for_axis_python_profile(self, tmp_path, monkeypatch):
        source = """
class Settings:
    host: str = "localhost"
    port: int = 5432
"""
        path = tmp_path / "settings.py"
        path.write_text(source, encoding="utf-8")

        class FakeDb:
            def get_symbol_index_for_file(self, file_path, workspace_id):
                return {}

            def degree_neighbor_uids(self, seed_uids, workspace_id):
                return []

            def upsert_file_structure(self, file_path, file_hash, symbols, workspace_id):
                self.upserted = [s.uid for s in symbols]

            def prune_symbols_for_file(self, file_path, keep_uids, workspace_id):
                pass

            def clear_outgoing_symbol_edges(self, symbol_uids, workspace_id):
                pass

            def link_calls(self, calls, workspace_id):
                pass

            def delete_imports_for_file(self, file_path, workspace_id):
                pass

            def link_imports(self, imports, workspace_id):
                pass

            def link_inheritance(self, inheritance_edges, workspace_id):
                pass

            def delete_proxy_bindings_for_file(self, file_path, workspace_id):
                pass

            def delete_decorators_for_file(self, file_path, workspace_id):
                pass

            def delete_type_references_for_file(self, file_path, workspace_id):
                pass

            def delete_injections_for_file(self, file_path, workspace_id):
                pass

            def recompute_degree_for_closure(self, seed_uids, workspace_id):
                pass

        class FakeLance:
            index_profile_name = AXIS_PYTHON_V1_PROFILE

            def __init__(self):
                self.rows = []

            def upsert_symbol_embeddings(self, symbols, *, workspace_id=None, **kwargs):
                self.rows = symbols

        finalize_calls = []

        monkeypatch.setattr(
            "context_engine.indexer.affects.AFFECTSIndexer",
            lambda db: MagicMock(rebuild_affects=lambda uids, workspace_id: None),
        )
        monkeypatch.setattr(
            "context_engine.indexer.external_facts.apply_external_boundary_for_file",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "context_engine.indexer.fast.pipeline.run_axis_incremental_finalize",
            lambda db, lance, workspace_id, **kwargs: (
                finalize_calls.append({"workspace_id": workspace_id, **kwargs}) or {}
            ),
        )

        from context_engine.parser.extractor import SymbolExtractor

        extractor = SymbolExtractor()
        extractor.project_root = str(tmp_path)
        lance = FakeLance()
        index_file(
            str(path),
            FakeDb(),
            lance,
            extractor,
            workspace_id="local/repo@main+axis_python_v1",
        )

        settings_row = next(row for row in lance.rows if row.get("symbol_kind") == "class")
        assert settings_row["qualified_name"]
        assert {"class_def", "annotation"} <= set(settings_row["struct_bits"])
        matches = json.loads(settings_row["axis_container_kinds_json"])
        contracts = json.loads(settings_row["axis_contracts_json"])
        assert {match["kind"] for match in matches} == {"config_carrier", "data_model"}
        assert {contract["contract"] for contract in contracts} == {
            "configuration_carrier",
            "data_shape_declaration",
        }
        assert finalize_calls
        assert finalize_calls[0]["workspace_id"] == "local/repo@main+axis_python_v1"
