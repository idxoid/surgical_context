"""Fast indexing pipeline.

Orchestrates parallel hashing, parallel parsing, global embedding batch,
and a single AFFECTS rebuild. Mirrors the semantics of
``sidecar.indexer.code.run_indexing`` but restructures the order of work
so that the slowest stages see the largest batches.

Pipeline stages:

1. collect_files — directory-prefiltered walk.
2. hash_phase — parallel sha256 over all collected files; compared against
   Neo4j-stored File.hash to produce ``changed_files``.
3. parse_phase — parallel ``FastExtractor.extract_all`` per changed file,
   using per-thread adapter instances. Each worker also queries the
   existing symbol index for its file so the diff can be computed without
   a second pass.
4. graph_phase — on the main thread, apply every per-file graph mutation
   via the existing ``Neo4jClient`` methods.
5. embed_phase — one global ``upsert_symbol_embeddings`` call with every
   changed symbol across every changed file.
6. affects_phase — one ``rebuild_affects`` call over the union of
   changed UIDs.
7. docs_phase — resolve pending DocAnchor links (unchanged from baseline).
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Protocol

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
)

from sidecar.context.framework_hints import FrameworkHintsIndexer
from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.indexer.fast.collector import collect_files
from sidecar.indexer.fast.extractor import ExtractedFile, FastExtractor, hash_file
from sidecar.indexer.fast.schema import ensure_fast_indexes
from sidecar.indexer.job_log import IndexJobLog
from sidecar.indexer.repository_profile import (
    RepositoryProfileInputs,
    build_empty_repository_profile,
    build_repository_profile,
    summarize_repository_profile,
)
from sidecar.retrieval.manifest import persist_index_manifest
from sidecar.silence import install as _silence
from sidecar.workspace import WorkspaceResolver

_silence()


NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

# Parallelism knobs. Default hash pool is high because hashing is I/O-bound;
# parse pool tracks CPU count because tree-sitter parsing is CPU-bound but
# releases the GIL inside the C extension.
_DEFAULT_HASH_WORKERS = max(4, (os.cpu_count() or 4) * 2)
_DEFAULT_PARSE_WORKERS = max(2, os.cpu_count() or 4)


class ProgressReporter(Protocol):
    """Optional progress sink. The pipeline calls these on phase boundaries
    and after each unit of per-file work. Implementations decide whether to
    render a tqdm bar, log lines, a GUI event, or nothing at all."""

    def stage_start(self, stage: str, total: int) -> None: ...
    def step(self, stage: str, n: int = 1) -> None: ...
    def stage_end(self, stage: str) -> None: ...


class _NullReporter:
    """Default no-op reporter. Keeps the pipeline quiet when no one is watching."""

    def stage_start(self, stage: str, total: int) -> None:
        pass

    def step(self, stage: str, n: int = 1) -> None:
        pass

    def stage_end(self, stage: str) -> None:
        pass


def _symbol_needs_upsert(sym, existing: dict | None) -> bool:
    """Replicates the baseline decision rule verbatim."""
    if existing is None:
        return True
    return bool(
        existing.get("hash") != sym.content_hash
        or int(existing.get("start_line") or 0) != sym.start_line
        or int(existing.get("end_line") or 0) != sym.end_line
    )


@dataclass
class FileDiff:
    """Parse output plus the incremental diff against the stored graph."""

    extracted: ExtractedFile
    current_uids: list[str] = field(default_factory=list)
    changed_uids: list[str] = field(default_factory=list)
    removed_uids: list[str] = field(default_factory=list)
    changed_symbols: list = field(default_factory=list)

    @property
    def edge_refresh_uids(self) -> list[str]:
        return self.changed_uids or self.current_uids


def _hash_phase(files: list[str], workers: int, reporter: ProgressReporter) -> dict[str, str]:
    """Parallel sha256 of every collected file."""
    hashes: dict[str, str] = {}
    reporter.stage_start("hash", total=len(files))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(hash_file, p): p for p in files}
        for fut in as_completed(futures):
            path = futures[fut]
            digest = fut.result()
            if digest:
                hashes[path] = digest
            reporter.step("hash")
    reporter.stage_end("hash")
    return hashes


def _parse_one(
    file_path: str,
    extractor: FastExtractor,
    db: Neo4jClient,
    workspace_id: str,
) -> FileDiff | None:
    """Runs inside a worker thread. Reads file, parses, computes diff."""
    extracted = extractor.extract_all(file_path)
    if extracted is None:
        return None

    # get_symbol_index_for_file is optional on the client; mirror baseline.
    get_idx = getattr(db, "get_symbol_index_for_file", None)
    existing = get_idx(file_path, workspace_id=workspace_id) if callable(get_idx) else {}

    current_uids = [s.uid for s in extracted.symbols]
    changed_symbols = [s for s in extracted.symbols if _symbol_needs_upsert(s, existing.get(s.uid))]
    changed_uids = [s.uid for s in changed_symbols]
    removed_uids = sorted(set(existing) - set(current_uids))

    return FileDiff(
        extracted=extracted,
        current_uids=current_uids,
        changed_uids=changed_uids,
        removed_uids=removed_uids,
        changed_symbols=changed_symbols,
    )


def _parse_phase(
    changed_files: list[str],
    project_path: str,
    file_hashes: dict[str, str],
    db: Neo4jClient,
    workspace_id: str,
    workers: int,
    job_log: IndexJobLog,
    reporter: ProgressReporter,
) -> list[FileDiff]:
    """Parallel extraction + diff computation."""
    extractor = FastExtractor(project_root=project_path)
    results: list[FileDiff] = []

    def _task(path: str) -> FileDiff | None:
        digest = file_hashes.get(path, "")
        with job_log.track_file_job(path, file_hash=digest):
            return _parse_one(path, extractor, db, workspace_id)

    reporter.stage_start("parse", total=len(changed_files))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_task, p): p for p in changed_files}
        for fut in as_completed(futures):
            diff = fut.result()
            if diff is not None:
                results.append(diff)
            reporter.step("parse")
    reporter.stage_end("parse")

    return results


def _apply_graph(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
):
    """Apply all graph mutations in main thread using existing Neo4jClient API."""
    prune_symbols = getattr(db, "prune_symbols_for_file", None)
    clear_edges = getattr(db, "clear_outgoing_symbol_edges", None)
    delete_imports = getattr(db, "delete_imports_for_file", None)

    reporter.stage_start("graph", total=len(diffs) * 2)
    for diff in diffs:
        ex = diff.extracted
        db.upsert_file_structure(
            ex.path, ex.file_hash, diff.changed_symbols, workspace_id=workspace_id
        )

        if callable(prune_symbols):
            prune_symbols(ex.path, keep_uids=diff.current_uids, workspace_id=workspace_id)

        reporter.step("graph")

    # Edges are linked only after every changed file has refreshed its
    # File/Symbol nodes. Otherwise imports/calls to files processed later in
    # this same batch are silently missed after a workspace reset or full reindex.
    for diff in diffs:
        ex = diff.extracted

        if callable(clear_edges):
            clear_edges(diff.edge_refresh_uids, workspace_id=workspace_id)

        if ex.calls and diff.edge_refresh_uids:
            db.link_calls(ex.calls, workspace_id=workspace_id)

        if callable(delete_imports):
            delete_imports(ex.path, workspace_id=workspace_id)
        if ex.imports:
            db.link_imports(ex.imports, workspace_id=workspace_id)

        if ex.inheritance and diff.edge_refresh_uids:
            db.link_inheritance(ex.inheritance, workspace_id=workspace_id)

        reporter.step("graph")
    reporter.stage_end("graph")


def _degree_seeds_snapshot(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
) -> tuple[set[str], set[str]]:
    """Capture (changed_or_refreshed uids, their pre-mutation neighbors) before relink.

    Neighbors must be read before ``_apply_graph`` deletes anything, since a removed
    symbol's neighbor becomes unreachable once detached but still needs its degree
    corrected. Returns (seed_uids, removed_uids) so the recompute can drop the
    now-deleted symbols from the final seed set.
    """
    seed_uids: set[str] = set()
    removed_uids: set[str] = set()
    for diff in diffs:
        seed_uids.update(diff.edge_refresh_uids)
        seed_uids.update(diff.removed_uids)
        removed_uids.update(diff.removed_uids)
    neighbor_fn = getattr(db, "degree_neighbor_uids", None)
    if callable(neighbor_fn) and seed_uids:
        seed_uids.update(neighbor_fn(sorted(seed_uids), workspace_id=workspace_id))
    return seed_uids, removed_uids


def _degree_phase(
    seed_uids: set[str],
    removed_uids: set[str],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    """Recompute materialized Symbol degree over the affected closure.

    Runs after every edge-creating phase (calls, imports, inheritance, MRO API,
    SEMANTIC_HINT, TS HTTP route hints) so all degree-counted edge types are
    present before counting.
    """
    recompute = getattr(db, "recompute_degree_for_closure", None)
    reporter.stage_start("degree", total=1)
    final_seeds = sorted(seed_uids - removed_uids)
    if callable(recompute) and final_seeds:
        recompute(final_seeds, workspace_id=workspace_id)
    reporter.step("degree")
    reporter.stage_end("degree")
    return len(final_seeds)


def _proxy_binding_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    """Create ProxyBinding nodes + PROXY_OF edges for lazy-proxy module vars.

    Runs after `_apply_graph` so the target types (e.g. FlaskProxy) already exist.
    Proxy detection is per-file in the adapter; we gather across all diffs here.
    """
    from sidecar.parser.adapters.python_adapter import PythonAdapter

    link_proxy = getattr(db, "link_proxy_bindings", None)
    reporter.stage_start("proxy_bindings", total=1)
    bindings: list[dict] = []
    if callable(link_proxy):
        adapter = PythonAdapter()
        for diff in diffs:
            ex = diff.extracted
            if not ex.path.endswith((".py", ".pyi")):
                continue
            try:
                bindings.extend(adapter.extract_proxy_bindings(ex.source, ex.path))
            except Exception:
                continue
        if bindings:
            link_proxy(bindings, workspace_id=workspace_id)
    reporter.step("proxy_bindings")
    reporter.stage_end("proxy_bindings")
    return {str(b["proxy_uid"]) for b in bindings if b.get("proxy_uid")}


def _proxy_call_resolution_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    """Forward calls on a proxy var THROUGH PROXY_OF to the real type's method.

    The proxy call-site (`current_app.ensure_sync`) is dropped at normal link time
    (no node matches the proxy-var qualified name), so we re-resolve from the parsed
    call facts here: match `callee_qualified_name` against ProxyBinding qns, then wire
    `caller -[CALLS_DYNAMIC {via_proxy}]-> target.method` (direct or via INHERITED_API).
    """
    resolve = getattr(db, "resolve_proxy_calls", None)
    reporter.stage_start("proxy_calls", total=1)
    created = 0
    if callable(resolve):
        proxy_calls = [
            {
                "caller_uid": c.get("caller_uid"),
                "callee_qualified_name": c.get("callee_qualified_name"),
                "call_site_line": c.get("call_site_line"),
            }
            for diff in diffs
            for c in diff.extracted.calls
            if c.get("callee_qualified_name")
        ]
        if proxy_calls:
            created = resolve(proxy_calls, workspace_id=workspace_id)
    reporter.step("proxy_calls")
    reporter.stage_end("proxy_calls")
    return created


def _decorator_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> int:
    """Create DECORATED_BY (handler→hook) and HANDLES (hook→handler) edges.

    Runs after `_apply_graph` so the decorator symbols (possibly cross-file) exist.
    The decoration is a syntactic fact extracted per-file; gathered across diffs.
    Must run under the same project_root_scope as symbol extraction so the decorated
    symbol's uid matches the stored node (uid derives from the project-relative
    qualified name).
    """
    from sidecar.parser.adapters.python_adapter import PythonAdapter
    from sidecar.parser.uid import project_root_scope

    link_deco = getattr(db, "link_decorators", None)
    reporter.stage_start("decorators", total=1)
    decorators: list[dict] = []
    if callable(link_deco):
        adapter = PythonAdapter()
        with project_root_scope(project_path or None):
            for diff in diffs:
                ex = diff.extracted
                if not ex.path.endswith((".py", ".pyi")):
                    continue
                try:
                    decorators.extend(adapter.extract_decorators(ex.source, ex.path))
                except Exception:
                    continue
        if decorators:
            link_deco(decorators, workspace_id=workspace_id)
    reporter.step("decorators")
    reporter.stage_end("decorators")
    return len(decorators)


def _type_reference_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> int:
    """Create USES_TYPE edges (referrer -> the project class it names).

    A type reference (annotation / isinstance) is a syntactic fact, like a
    decoration. Runs after `_apply_graph` so the referenced class symbols exist,
    and under the same project_root_scope so referrer uids match stored nodes.
    """
    from sidecar.parser.adapters.python_adapter import PythonAdapter
    from sidecar.parser.uid import project_root_scope

    link_types = getattr(db, "link_type_references", None)
    reporter.stage_start("type_refs", total=1)
    references: list[dict] = []
    if callable(link_types):
        adapter = PythonAdapter()
        with project_root_scope(project_path or None):
            for diff in diffs:
                ex = diff.extracted
                if not ex.path.endswith((".py", ".pyi")):
                    continue
                try:
                    references.extend(adapter.extract_type_references(ex.source, ex.path))
                except Exception:
                    continue
        if references:
            link_types(references, workspace_id=workspace_id)
    reporter.step("type_refs")
    reporter.stage_end("type_refs")
    return len(references)


def _reexport_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> int:
    """Create RE_EXPORTS edges (package __init__ file -> the project symbol it surfaces).

    A re-export (``from .submodule import Name`` in an ``__init__``) is a syntactic
    fact, like a type reference. Runs after `_apply_graph` so the surfaced symbols
    exist, and under the same project_root_scope so target uids match stored nodes.
    Gives public surface symbols a ``reexport_in`` signal orthogonal to call/type
    fan-in (whose callers live in user code excluded from clustering).
    """
    from sidecar.parser.adapters.python_adapter import PythonAdapter
    from sidecar.parser.uid import project_root_scope

    link_reexports = getattr(db, "link_reexports", None)
    reporter.stage_start("reexports", total=1)
    reexports: list[dict] = []
    if callable(link_reexports):
        adapter = PythonAdapter()
        with project_root_scope(project_path or None):
            for diff in diffs:
                ex = diff.extracted
                if not ex.path.endswith((".py", ".pyi")):
                    continue
                try:
                    reexports.extend(adapter.extract_reexports(ex.source, ex.path))
                except Exception:
                    continue
        if reexports:
            link_reexports(reexports, workspace_id=workspace_id)
    reporter.step("reexports")
    reporter.stage_end("reexports")
    return len(reexports)


def _instantiation_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> int:
    """Create INSTANTIATES edges (caller -> the project class it constructs).

    A construction (literal ``X(...)`` or ``v(...)`` for a ``type[X]``-typed local)
    is a syntactic fact, like a type reference. Runs after `_apply_graph` so the
    constructed class symbols exist, and under the same project_root_scope so caller
    uids match stored nodes. Feeds the factory_surface role an explicit construction
    signal distinct from a plain caller.
    """
    from sidecar.parser.adapters.python_adapter import PythonAdapter
    from sidecar.parser.uid import project_root_scope

    link_inst = getattr(db, "link_instantiations", None)
    reporter.stage_start("instantiations", total=1)
    instantiations: list[dict] = []
    if callable(link_inst):
        adapter = PythonAdapter()
        with project_root_scope(project_path or None):
            for diff in diffs:
                ex = diff.extracted
                if not ex.path.endswith((".py", ".pyi")):
                    continue
                try:
                    instantiations.extend(
                        adapter.extract_instantiations(ex.source, ex.path)
                    )
                except Exception:
                    continue
        if instantiations:
            link_inst(instantiations, workspace_id=workspace_id)
    reporter.step("instantiations")
    reporter.stage_end("instantiations")
    return len(instantiations)


def _injection_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> int:
    """Create INJECTS edges (owner -> provider wired into a parameter default).

    Static DI binding fact, like USES_TYPE. Runs after `_apply_graph` so providers
    exist, under project_root_scope so owner uids match stored nodes.
    """
    from sidecar.parser.adapters.python_adapter import PythonAdapter
    from sidecar.parser.uid import project_root_scope

    link_inj = getattr(db, "link_injections", None)
    reporter.stage_start("injections", total=1)
    injections: list[dict] = []
    if callable(link_inj):
        adapter = PythonAdapter()
        with project_root_scope(project_path or None):
            for diff in diffs:
                ex = diff.extracted
                if not ex.path.endswith((".py", ".pyi")):
                    continue
                try:
                    injections.extend(adapter.extract_injections(ex.source, ex.path))
                except Exception:
                    continue
        if injections:
            link_inj(injections, workspace_id=workspace_id)
    reporter.step("injections")
    reporter.stage_end("injections")
    return len(injections)


def _mro_api_bridge_phase(
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    from sidecar.indexer.mro_api_bridge import MroApiBridgeIndexer

    reporter.stage_start("mro_api_bridge", total=1)
    created = MroApiBridgeIndexer(db).apply(workspace_id)
    reporter.stage_end("mro_api_bridge")
    return created


def _framework_hints_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    """Apply semantic hint rules to create SEMANTIC_HINT edges."""
    reporter.stage_start("framework_hints", total=len(diffs))
    indexer = FrameworkHintsIndexer(db)
    indexer.apply_rules(diffs, workspace_id)
    reporter.stage_end("framework_hints")
    return len(diffs)


def _ts_http_route_hints_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    project_path: str,
    reporter: ProgressReporter,
) -> int:
    """Link TS HTTP client surfaces to Python FastAPI handlers."""
    from sidecar.indexer.ts_http_route_hints import TsHttpRouteHintsIndexer

    reporter.stage_start("ts_http_route_hints", total=len(diffs))
    created = TsHttpRouteHintsIndexer(db, project_path).apply(diffs, workspace_id)
    reporter.stage_end("ts_http_route_hints")
    return created


def _embed_phase(
    diffs: list[FileDiff],
    lance: LanceDBClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> tuple[int, int]:
    """One global encode+upsert call. Returns (changed_count, removed_count)."""
    symbol_docs: list[dict] = []
    removed_uids: list[str] = []

    for diff in diffs:
        ex = diff.extracted
        source_lines = ex.source.splitlines()
        changed_set = {s.uid for s in diff.changed_symbols}
        for s in ex.symbols:
            if s.uid not in changed_set:
                continue
            symbol_docs.append(
                {
                    "uid": s.uid,
                    "name": s.name,
                    "file_path": s.file_path,
                    "workspace_id": workspace_id,
                    "code": "\n".join(source_lines[s.start_line - 1 : s.end_line]),
                }
            )
        removed_uids.extend(diff.removed_uids)

    # Two indivisible steps: encode+upsert, then delete stale rows.
    # SentenceTransformer.encode doesn't yield per-item progress cheaply,
    # so the reporter only sees the batch boundary.
    reporter.stage_start("embed", total=2 if removed_uids else 1)

    def _emit_embed_progress(message: str) -> None:
        print(f"[embed] {message}")

    if symbol_docs:
        lance.upsert_symbol_embeddings(
            symbol_docs,
            workspace_id=workspace_id,
            progress_callback=_emit_embed_progress,
        )
    reporter.step("embed")

    if removed_uids:
        delete_embeddings = getattr(lance, "delete_symbol_embeddings", None)
        if callable(delete_embeddings):
            delete_embeddings(removed_uids, workspace_id=workspace_id)
        reporter.step("embed")
    reporter.stage_end("embed")

    return len(symbol_docs), len(removed_uids)


def _affects_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    """Single AFFECTS rebuild over the union of every changed UID."""
    union: list[str] = []
    seen: set[str] = set()
    for diff in diffs:
        for uid in diff.changed_uids:
            if uid not in seen:
                seen.add(uid)
                union.append(uid)

    if not union:
        reporter.stage_start("affects", total=0)
        reporter.stage_end("affects")
        return 0

    from sidecar.indexer.affects import AFFECTSIndexer

    reporter.stage_start("affects", total=len(union))
    AFFECTSIndexer(db).rebuild_affects(
        union,
        workspace_id=workspace_id,
        progress_callback=lambda n: reporter.step("affects", n=n),
    )
    reporter.stage_end("affects")
    return len(union)


def _build_profile_from_diffs(
    project_path: str,
    workspace_id: str,
    files: list[str],
    diffs: list[FileDiff],
    stats: dict,
    db: Neo4jClient,
) -> dict:
    """Build the index-time reasoning contract from this indexing pass."""
    counts = _workspace_profile_counts(db, workspace_id, diffs)
    sample_texts = _profile_sample_texts(project_path, files, diffs)

    inputs = RepositoryProfileInputs(
        project_path=project_path,
        workspace_id=workspace_id,
        collected_files=files,
        parsed_files=counts["files"],
        symbols_indexed=counts["symbols"],
        symbols_removed=int(stats.get("symbols_removed") or 0),
        calls_indexed=counts["calls"],
        imports_indexed=counts["imports"],
        inheritance_indexed=counts["inheritance"],
        affects_rebuilt=counts["affects"],
        skip_affects=bool(stats.get("skip_affects")),
        sample_texts=sample_texts,
    )
    return build_repository_profile(inputs)


def _workspace_profile_counts(
    db: Neo4jClient,
    workspace_id: str,
    diffs: list[FileDiff],
) -> dict[str, int]:
    get_counts = getattr(db, "get_workspace_profile_counts", None)
    if callable(get_counts):
        counts = get_counts(workspace_id=workspace_id)
        return {
            "files": int(counts.get("files") or 0),
            "symbols": int(counts.get("symbols") or 0),
            "calls": int(counts.get("calls") or 0),
            "imports": int(counts.get("imports") or 0),
            "inheritance": int(counts.get("inheritance") or 0),
            "affects": int(counts.get("affects") or 0),
        }
    return {
        "files": len(diffs),
        "symbols": sum(len(diff.extracted.symbols) for diff in diffs),
        "calls": sum(len(diff.extracted.calls) for diff in diffs),
        "imports": sum(len(diff.extracted.imports) for diff in diffs),
        "inheritance": sum(len(diff.extracted.inheritance) for diff in diffs),
        "affects": 0,
    }


def _profile_sample_texts(
    project_path: str,
    files: list[str],
    diffs: list[FileDiff],
    *,
    limit: int = 80,
) -> list[str]:
    sample_texts: list[str] = []
    seen: set[str] = set()
    for diff in diffs[:limit]:
        rel = os.path.relpath(diff.extracted.path, project_path)
        sample_texts.append(f"# {rel}\n{diff.extracted.source[:1200]}")
        seen.add(diff.extracted.path)
    for path in files:
        if len(sample_texts) >= limit:
            break
        if path in seen:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                source = fh.read(1200)
        except (OSError, UnicodeDecodeError):
            continue
        rel = os.path.relpath(path, project_path)
        sample_texts.append(f"# {rel}\n{source}")
    return sample_texts


def _use_repository_profile(
    stats: dict,
    profile: dict,
    db: Neo4jClient,
    workspace_id: str,
) -> None:
    """Attach and persist the repository profile for downstream consumers."""
    save_profile = getattr(db, "save_repository_profile", None)
    if callable(save_profile):
        save_profile(profile, workspace_id=workspace_id)
    stats["repository_profile"] = profile
    stats["repository_profile_store"] = "neo4j_workspace"


def run_fast_indexing(
    project_path: str,
    workspace_id: str | None = None,
    *,
    hash_workers: int | None = None,
    parse_workers: int | None = None,
    skip_affects: bool = False,
    reporter: ProgressReporter | None = None,
) -> dict:
    """Drop-in alternative to ``sidecar.indexer.code.run_indexing``.

    Returns a stats dict so callers can compare runs against the baseline.
    Pass ``reporter`` to wire up progress bars; defaults to no-op.
    """
    hash_workers = hash_workers or _DEFAULT_HASH_WORKERS
    parse_workers = parse_workers or _DEFAULT_PARSE_WORKERS
    reporter = reporter or _NullReporter()

    workspace_id = workspace_id or WorkspaceResolver().from_project_path(project_path).id
    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    lance = LanceDBClient()
    job_log = IndexJobLog()

    stats: dict = {
        "project_path": project_path,
        "workspace_id": workspace_id,
        "skip_affects": skip_affects,
        # `performed` / `skipped` mirror the shape that QA/qa_benchmark.py
        # expects from _empty_indexing_summary(). A real run always
        # performs indexing, so performed=True / skipped=False.
        "performed": True,
        "skipped": False,
        "collected": 0,
        "changed": 0,
        "parsed": 0,
        "symbols_encoded": 0,
        "symbols_removed": 0,
        "framework_hints_applied": 0,
        "affects_rebuilt": 0,
        # Doc indexing is done by the caller (qa_benchmark calls
        # sidecar.indexer.docs.index_docs separately). We pre-seed these
        # keys so consumers can always read them without KeyError; the
        # benchmark overwrites them after its doc pass completes.
        "docs_files_indexed": 0,
        "docs_chunks_indexed": 0,
        "docs_timings_sec": {},
        "timings_sec": {},
        "repository_profile": build_empty_repository_profile(
            project_path, workspace_id, reason="not_built"
        ),
        "repository_profile_store": "",
    }

    t0 = time.perf_counter()
    print(f"🚀 Fast indexing: {project_path} ({workspace_id})")
    print(f"   hash_workers={hash_workers} parse_workers={parse_workers}")

    try:
        # Stage 0: ensure node/constraint indexes exist.
        # Without a UNIQUE constraint on Symbol.uid and an index on
        # Symbol.name, MERGE and lookup-by-name queries degrade to full
        # label scans, producing super-linear slowdown on large repos.
        # This call is idempotent; on an already-migrated graph it is
        # a handful of no-op ``IF NOT EXISTS`` statements.
        t_stage = time.perf_counter()
        created = ensure_fast_indexes(db)
        stats["timings_sec"]["schema"] = round(time.perf_counter() - t_stage, 3)
        if created:
            print(f"🧭 Schema: created {len(created)} missing objects: {created}")
            print("   (first run on this DB; subsequent runs skip this step)")

        # Stage 1: collect
        t_stage = time.perf_counter()
        files = collect_files(project_path)
        stats["collected"] = len(files)
        stats["timings_sec"]["collect"] = round(time.perf_counter() - t_stage, 3)
        if not files:
            _use_repository_profile(
                stats,
                build_empty_repository_profile(
                    project_path, workspace_id, reason="no_indexable_files"
                ),
                db,
                workspace_id,
            )
            persist_index_manifest(
                stats=stats,
                db=db,
                workspace_id=workspace_id,
                project_path=project_path,
                outcome="no_indexable_files",
            )
            print(f"❌ No indexable files under {project_path}")
            return stats

        # Stage 2: parallel hash + diff
        t_stage = time.perf_counter()
        current_hashes = _hash_phase(files, hash_workers, reporter)
        stored_hashes = db.get_file_hashes(files, workspace_id=workspace_id)
        changed_files = [
            p for p in files if current_hashes.get(p) and current_hashes[p] != stored_hashes.get(p)
        ]
        stats["changed"] = len(changed_files)
        stats["timings_sec"]["hash"] = round(time.perf_counter() - t_stage, 3)

        if not changed_files:
            get_profile = getattr(db, "get_repository_profile", None)
            existing_profile = (
                get_profile(workspace_id=workspace_id) if callable(get_profile) else None
            )
            if existing_profile:
                stats["repository_profile"] = existing_profile
                stats["repository_profile_store"] = "neo4j_workspace"
            else:
                _use_repository_profile(
                    stats,
                    _build_profile_from_diffs(
                        project_path,
                        workspace_id,
                        files,
                        [],
                        stats,
                        db,
                    ),
                    db,
                    workspace_id,
                )
            print(f"   readiness={summarize_repository_profile(stats['repository_profile'])}")
            print("✅ All files up-to-date, nothing to re-index.")
            persist_index_manifest(
                stats=stats,
                db=db,
                workspace_id=workspace_id,
                project_path=project_path,
                outcome="noop_unchanged",
            )
            return stats
        print(f"🔄 {len(changed_files)}/{len(files)} files changed")

        # Stage 3: parallel parse
        t_stage = time.perf_counter()
        diffs = _parse_phase(
            changed_files,
            project_path,
            current_hashes,
            db,
            workspace_id,
            parse_workers,
            job_log,
            reporter,
        )
        stats["parsed"] = len(diffs)
        stats["timings_sec"]["parse"] = round(time.perf_counter() - t_stage, 3)

        # Stage 4: graph writes. Snapshot the degree-affected closure first, while
        # pre-mutation edges are still present (deleted symbols' neighbors included).
        degree_seeds, degree_removed = _degree_seeds_snapshot(diffs, db, workspace_id)
        t_stage = time.perf_counter()
        _apply_graph(diffs, db, workspace_id, reporter)
        stats["timings_sec"]["graph"] = round(time.perf_counter() - t_stage, 3)

        t_stage = time.perf_counter()
        stats["mro_api_edges"] = _mro_api_bridge_phase(db, workspace_id, reporter)
        stats["timings_sec"]["mro_api_bridge"] = round(time.perf_counter() - t_stage, 3)

        # Stage 4.5: framework hints
        t_stage = time.perf_counter()
        stats["framework_hints_applied"] = _framework_hints_phase(diffs, db, workspace_id, reporter)
        stats["timings_sec"]["framework_hints"] = round(time.perf_counter() - t_stage, 3)

        t_stage = time.perf_counter()
        stats["ts_http_route_hints_applied"] = _ts_http_route_hints_phase(
            diffs,
            db,
            workspace_id,
            project_path,
            reporter,
        )
        stats["timings_sec"]["ts_http_route_hints"] = round(time.perf_counter() - t_stage, 3)

        # Stage 4.6: lazy-proxy resolution. Create ProxyBinding nodes + PROXY_OF
        # edges, then forward proxy-var calls (current_app.x) through to the real
        # type. Must precede degree so the forwarded edges are counted.
        t_stage = time.perf_counter()
        proxy_uids = _proxy_binding_phase(diffs, db, workspace_id, reporter)
        stats["proxy_bindings"] = len(proxy_uids)
        stats["proxy_calls_resolved"] = _proxy_call_resolution_phase(
            diffs, db, workspace_id, reporter
        )
        stats["timings_sec"]["proxy"] = round(time.perf_counter() - t_stage, 3)
        # Proxy nodes/edges are created after the degree snapshot, so fold the new
        # proxy nodes into the seed set; the closure recompute then covers them and
        # their PROXY_OF / via_proxy neighbors.
        degree_seeds |= proxy_uids

        # Stage 4.65: DECORATED_BY + HANDLES edges. Not counted into materialized degree
        # (kept out of _DEGREE_REL_PATTERN to avoid a global degree shift), so it
        # need not precede the degree phase for counting.
        t_stage = time.perf_counter()
        stats["decorators_linked"] = _decorator_phase(
            diffs, db, workspace_id, reporter, project_path
        )
        stats["timings_sec"]["decorators"] = round(time.perf_counter() - t_stage, 3)

        # Stage 4.66: USES_TYPE edges. Like DECORATED_BY, kept out of materialized
        # degree (separate, low-weight, filterable relation), so ordering vs the
        # degree phase is irrelevant.
        t_stage = time.perf_counter()
        stats["type_refs_linked"] = _type_reference_phase(
            diffs, db, workspace_id, reporter, project_path
        )
        stats["timings_sec"]["type_refs"] = round(time.perf_counter() - t_stage, 3)

        # Stage 4.665: RE_EXPORTS edges (package __init__ -> surfaced symbol). Like
        # USES_TYPE, a low-weight derived relation, not in materialized degree.
        t_stage = time.perf_counter()
        stats["reexports_linked"] = _reexport_phase(
            diffs, db, workspace_id, reporter, project_path
        )
        stats["timings_sec"]["reexports"] = round(time.perf_counter() - t_stage, 3)

        # Stage 4.668: INSTANTIATES edges (caller -> constructed class). Like
        # USES_TYPE, a low-weight derived relation, not in materialized degree.
        t_stage = time.perf_counter()
        stats["instantiations_linked"] = _instantiation_phase(
            diffs, db, workspace_id, reporter, project_path
        )
        stats["timings_sec"]["instantiations"] = round(time.perf_counter() - t_stage, 3)

        # Stage 4.67: INJECTS edges (DI bindings). Like USES_TYPE, not in degree.
        t_stage = time.perf_counter()
        stats["injections_linked"] = _injection_phase(
            diffs, db, workspace_id, reporter, project_path
        )
        stats["timings_sec"]["injections"] = round(time.perf_counter() - t_stage, 3)

        # Stage 4.7: recompute materialized degree now that all edge-creating
        # phases (calls, imports, inheritance, MRO API, hints, proxy) have run.
        t_stage = time.perf_counter()
        stats["degree_recomputed"] = _degree_phase(
            degree_seeds, degree_removed, db, workspace_id, reporter
        )
        stats["timings_sec"]["degree"] = round(time.perf_counter() - t_stage, 3)

        # Stage 5: global embedding batch
        t_stage = time.perf_counter()
        encoded, removed = _embed_phase(diffs, lance, workspace_id, reporter)
        stats["symbols_encoded"] = encoded
        stats["symbols_removed"] = removed
        stats["timings_sec"]["embed"] = round(time.perf_counter() - t_stage, 3)

        # Stage 6: single AFFECTS rebuild
        if skip_affects:
            reporter.stage_start("affects", total=0)
            reporter.stage_end("affects")
            stats["affects_rebuilt"] = 0
            stats["timings_sec"]["affects"] = 0.0
            print("⏭️  Skipping AFFECTS rebuild")
        else:
            t_stage = time.perf_counter()
            stats["affects_rebuilt"] = _affects_phase(diffs, db, workspace_id, reporter)
            stats["timings_sec"]["affects"] = round(time.perf_counter() - t_stage, 3)

        # Stage 7: resolve pending DocAnchors (unchanged)
        t_stage = time.perf_counter()
        from sidecar.indexer.anchor import resolve_pending_anchors

        reporter.stage_start("docs", total=1)
        resolve_pending_anchors(
            db,
            lance,
            workspace_id=workspace_id,
            allowed_prefixes=[project_path],
        )
        reporter.step("docs")
        reporter.stage_end("docs")
        stats["timings_sec"]["docs"] = round(time.perf_counter() - t_stage, 3)

        # Stage 7.5: Pass 1 — derive a per-repo role taxonomy from
        # call-graph topology and persist it on Workspace + Symbol nodes.
        # Universal replacement for the hand-curated role heuristics that
        # still exist as fallbacks in mechanism_registry / unified_ranker.
        t_stage = time.perf_counter()
        from sidecar.context.mechanism_registry import (
            ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY,
            preloaded_mechanism_catalog_extensions,
        )
        from sidecar.indexer.role_clustering import (
            build_role_catalog,
            derive_and_persist_role_taxonomy,
        )

        reporter.stage_start("role_clustering", total=1)
        taxonomy = derive_and_persist_role_taxonomy(db, workspace_id)
        role_catalog = build_role_catalog(taxonomy)
        reporter.step("role_clustering")
        reporter.stage_end("role_clustering")
        stats["timings_sec"]["role_clustering"] = round(time.perf_counter() - t_stage, 3)
        stats["role_taxonomy"] = {
            "chosen_k": taxonomy.chosen_k,
            "silhouette": round(taxonomy.silhouette, 4),
            "sample_size": taxonomy.sample_size,
        }
        _preloaded_mech = preloaded_mechanism_catalog_extensions()
        stats["role_catalog"] = {
            "archetypes": len(role_catalog.archetypes),
            "roles": len(role_catalog.role_to_archetypes),
            "preloaded_mechanisms": len(_preloaded_mech[ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY]),
        }

        _use_repository_profile(
            stats,
            _build_profile_from_diffs(
                project_path,
                workspace_id,
                files,
                diffs,
                stats,
                db,
            ),
            db,
            workspace_id,
        )
        persist_index_manifest(
            stats=stats,
            db=db,
            workspace_id=workspace_id,
            project_path=project_path,
            outcome="full_index",
        )

    finally:
        db.close()

    stats["timings_sec"]["total"] = round(time.perf_counter() - t0, 3)
    print(f"✅ Fast indexing complete in {stats['timings_sec']['total']}s")
    print(
        f"   parsed={stats['parsed']} encoded={stats['symbols_encoded']} "
        f"affects={stats['affects_rebuilt']}"
    )
    print(f"   readiness={summarize_repository_profile(stats['repository_profile'])}")
    print(f"   timings={stats['timings_sec']}")
    return stats
