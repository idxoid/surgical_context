"""Fast indexing pipeline.

Orchestrates parallel hashing, parallel parsing, global embedding batch,
and a single AFFECTS rebuild. Mirrors the semantics of
``context_engine.indexer.code.run_indexing`` but restructures the order of work
so that the slowest stages see the largest batches.

Pipeline stages:

1. collect_files — directory-prefiltered walk.
2. hash_phase — parallel sha256 over all collected files; compared against
   Neo4j-stored File.hash to produce ``changed_files``.
3. parse_phase — parallel ``FastExtractor.extract_all`` per changed file,
   using per-thread adapter instances. On ``axis_python_v1`` profiles, axis
   facts are extracted here (same tree-sitter parse) and cached on
   ``ExtractedFile.axis_facts`` for reuse in embed_phase.
4. graph_phase — on the main thread, apply every per-file graph mutation
   via the existing ``Neo4jClient`` methods.
5. embed_phase — one global ``upsert_symbol_embeddings`` call with every
   changed symbol across every changed file.
6. affects_phase — one ``rebuild_affects`` call over the union of
   changed UIDs.
7. docs_phase — resolve pending DocAnchor links (unchanged from baseline).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
)

from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.neo4j_client import Neo4jClient
from context_engine.database.neo4j_env import (  # noqa: F401 — re-exported for QA scripts & fast/__main__
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
)
from context_engine.database.provider import get_database_provider
from context_engine.index_profile import (
    AXIS_PYTHON_V1_PROFILE,
    active_index_profile,
    effective_index_workspace_id,
    resolve_index_profile,
)
from context_engine.indexer.external_boundary import (
    build_project_boundary,
    package_manifest_external_roots,
)
from context_engine.indexer.external_facts import apply_external_boundary_for_file
from context_engine.indexer.fast.collector import collect_files
from context_engine.indexer.fast.extractor import ExtractedFile, FastExtractor, hash_file
from context_engine.indexer.fast.schema import ensure_fast_indexes
from context_engine.indexer.file_tier import classify_file_tier, is_pure_reexport_source
from context_engine.indexer.job_log import IndexJobLog
from context_engine.indexer.repository_profile import (
    RepositoryProfileInputs,
    build_empty_repository_profile,
    build_repository_profile,
    summarize_repository_profile,
)
from context_engine.retrieval.manifest import persist_index_manifest
from context_engine.silence import install as _silence
from context_engine.workspace import WorkspaceResolver

if TYPE_CHECKING:
    from context_engine.axis.container_kind import GraphContextProbe
    from context_engine.axis.schema import AxisProfile

_silence()

# Parallelism knobs. Default hash pool is high because hashing is I/O-bound;
# parse pool tracks CPU count because tree-sitter parsing is CPU-bound but
# releases the GIL inside the C extension.
_DEFAULT_HASH_WORKERS = max(4, (os.cpu_count() or 4) * 2)
_DEFAULT_PARSE_WORKERS = max(2, os.cpu_count() or 4)


def _parse_link_phase_result(result, fallback_count: int) -> tuple[int, set[str]]:
    if isinstance(result, tuple):
        return int(result[0] or 0), {str(uid) for uid in (result[1] or set()) if uid}
    if isinstance(result, set):
        return fallback_count, {str(uid) for uid in result if uid}
    if isinstance(result, int):
        return result, set()
    return fallback_count, set()


def _collect_adapter_facts_from_diffs(
    diffs: list[FileDiff],
    extract_attr: str,
) -> list:
    from context_engine.parser.registry import REGISTRY

    facts: list = []
    for diff in diffs:
        ex = diff.extracted
        try:
            language = REGISTRY.detect_language(ex.path)
            adapter = REGISTRY.get_adapter(language)
        except Exception:
            continue
        extract_fn = getattr(adapter, extract_attr, None)
        if not callable(extract_fn):
            continue
        try:
            facts.extend(extract_fn(ex.source, ex.path))
        except Exception:
            continue
    return facts


def _collect_decorator_facts(
    diffs: list[FileDiff],
    py_adapter,
    ts_adapter,
) -> tuple[list[dict], list[dict]]:
    decorators: list[dict] = []
    compositions: list[dict] = []
    for diff in diffs:
        ex = diff.extracted
        if ex.path.endswith((".py", ".pyi")):
            adapter = py_adapter
        elif ex.path.endswith((".ts", ".tsx")):
            adapter = ts_adapter
        else:
            continue
        try:
            decorators.extend(adapter.extract_decorators(ex.source, ex.path))
        except Exception:
            continue
        extract_compose = getattr(adapter, "extract_decorator_compositions", None)
        if not callable(extract_compose):
            continue
        try:
            compositions.extend(extract_compose(ex.source, ex.path))
        except Exception:
            continue
    return decorators, compositions


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
        pass  # No-op default: pipeline runs without a progress UI attached.

    def step(self, stage: str, n: int = 1) -> None:
        pass  # No-op default: callers may pass a real reporter for tqdm/logging.

    def stage_end(self, stage: str) -> None:
        pass  # No-op default: stage boundaries are ignored unless overridden.


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


def _clear_derived_edges_for_diffs(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> None:
    """Drop stale per-file derived edges before relinking (parity with ``index_file``)."""
    per_file_deleters: tuple[tuple[str, str], ...] = (
        ("proxy_bindings", "delete_proxy_bindings_for_file"),
        ("decorators", "delete_decorators_for_file"),
        ("decorator_compositions", "delete_decorator_compositions_for_file"),
        ("type_refs", "delete_type_references_for_file"),
        ("injections", "delete_injections_for_file"),
        ("attr_accesses", "delete_attr_accesses_for_file"),
        ("reexports", "delete_reexports_for_file"),
        ("instantiations", "delete_instantiations_for_file"),
        ("hooks", "delete_hooks_for_file"),
        ("metadata_bridges", "delete_metadata_bridges_for_file"),
        ("http_endpoints", "delete_http_endpoints_for_file"),
    )
    reporter.stage_start("clear_derived_edges", total=len(diffs) * len(per_file_deleters))
    for diff in diffs:
        path = diff.extracted.path
        for _, method_name in per_file_deleters:
            delete_fn = getattr(db, method_name, None)
            if callable(delete_fn):
                delete_fn(path, workspace_id=workspace_id)
            reporter.step("clear_derived_edges")
    reporter.stage_end("clear_derived_edges")


def _rebuild_affects_for_uids(
    uids: Collection[str],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    if not uids:
        reporter.stage_start("affects", total=0)
        reporter.stage_end("affects")
        return 0
    from context_engine.indexer.affects import AFFECTSIndexer

    reporter.stage_start("affects", total=len(uids))
    AFFECTSIndexer(db).rebuild_affects(list(uids), workspace_id=workspace_id)
    reporter.stage_end("affects")
    return len(uids)


def _tombstone_phase(
    db: Neo4jClient,
    lance: LanceDBClient,
    *,
    workspace_id: str,
    project_path: str,
    active_paths: list[str],
    reporter: ProgressReporter,
) -> tuple[list[str], list[str]]:
    from context_engine.workspace_paths import (
        prune_graph_paths_outside_root,
        tombstone_stale_indexed_files,
    )

    project_root = Path(project_path).resolve()
    prune_graph_paths_outside_root(db, workspace_id=workspace_id, project_root=project_root)
    reporter.stage_start("tombstone", total=1)
    removed_paths, removed_uids = tombstone_stale_indexed_files(
        db,
        lance,
        workspace_id=workspace_id,
        project_root=project_root,
        active_paths=active_paths,
    )
    reporter.step("tombstone")
    reporter.stage_end("tombstone")
    return removed_paths, removed_uids


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
    *,
    include_axis_facts: bool = False,
) -> list[FileDiff]:
    """Parallel extraction + diff computation."""
    extractor = FastExtractor(
        project_root=project_path,
        workspace_id=workspace_id,
        include_axis_facts=include_axis_facts,
    )
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
    #
    # Batch-link across files: every linker is a pure UNWIND on the call/import/
    # inheritance list, so per-file invocation cost ~N×RTT — collapses to one
    # round-trip per relation type for the whole diff. clear_outgoing_symbol_edges
    # already accepts a uid list. delete_imports stays per-file (one query MATCHes
    # by file path, no list-form available; small fraction of the budget).
    if callable(clear_edges):
        all_refresh_uids = [u for diff in diffs for u in diff.edge_refresh_uids]
        if all_refresh_uids:
            clear_edges(all_refresh_uids, workspace_id=workspace_id)

    all_calls = [c for diff in diffs for c in diff.extracted.calls if diff.edge_refresh_uids]
    if all_calls:
        db.link_calls(all_calls, workspace_id=workspace_id)

    if callable(delete_imports):
        for diff in diffs:
            delete_imports(diff.extracted.path, workspace_id=workspace_id)

    all_imports = [imp for diff in diffs for imp in diff.extracted.imports]
    if all_imports:
        db.link_imports(all_imports, workspace_id=workspace_id)

    all_inheritance = [
        edge for diff in diffs for edge in diff.extracted.inheritance if diff.edge_refresh_uids
    ]
    if all_inheritance:
        db.link_inheritance(all_inheritance, workspace_id=workspace_id)

    for _ in diffs:
        reporter.step("graph")
    reporter.stage_end("graph")


def _integrates_with_phase(
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    """Workspace pass: ``(:File)-[:INTEGRATES_WITH]->(:File)`` for files sharing
    >=2 non-plumbing external imports.

    Depends on ``IMPORTS_EXTERNAL`` (built by ``_external_boundary_phase``).
    """
    method = getattr(db, "materialize_file_integrates_with", None)
    if not callable(method):
        return 0
    reporter.stage_start("integrates_with", total=1)
    created = method(workspace_id=workspace_id)
    reporter.step("integrates_with")
    reporter.stage_end("integrates_with")
    return int(created or 0)


def _external_boundary_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    project_path: str,
    indexed_files: list[str],
    reporter: ProgressReporter,
) -> tuple[int, int]:
    """Materialize ``ExternalPkg`` nodes and ``*_EXTERNAL`` edges (C1)."""
    link_boundary = getattr(db, "link_external_boundary", None)
    if not callable(link_boundary):
        return 0, 0
    boundary = build_project_boundary(project_path, file_paths=tuple(indexed_files))
    project_external_roots = package_manifest_external_roots(project_path)
    calls_created = 0
    imports_created = 0
    reporter.stage_start("external_boundary", total=len(diffs))
    for diff in diffs:
        ex = diff.extracted
        created_calls, created_imports = apply_external_boundary_for_file(
            db,
            file_path=ex.path,
            source_code=ex.source,
            calls=ex.calls,
            boundary=boundary,
            workspace_id=workspace_id,
            project_external_roots=project_external_roots,
        )
        calls_created += created_calls
        imports_created += created_imports
        reporter.step("external_boundary")
    reporter.stage_end("external_boundary")
    return calls_created, imports_created


def _extends_external_phase(
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    """Connect class Symbols to ExternalSymbol nodes they structurally inherit.

    Workspace-wide pass: must run AFTER both ``link_inheritance`` (which writes
    ``parsed_base_names`` on each subclass) and ``_external_boundary_phase``
    (which materializes IMPORTS_EXTERNAL_SYMBOL with ``local_alias``). The join
    on those two pieces is the structural proof that this class inherits from
    the external symbol — no name-pattern matching at the consumer site.
    """
    method = getattr(db, "materialize_extends_external", None)
    if not callable(method):
        return 0
    reporter.stage_start("extends_external", total=1)
    created = method(workspace_id=workspace_id)
    reporter.step("extends_external")
    reporter.stage_end("extends_external")
    return int(created or 0)


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

    Runs after every edge-creating phase (calls, imports, inheritance, MRO API)
    so all degree-counted edge types are present before counting.
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
    project_path: str | None = None,
) -> set[str]:
    """Create ProxyBinding nodes + PROXY_OF edges for lazy-proxy module vars.

    Runs after `_apply_graph` so the target types (e.g. FlaskProxy) already exist.
    Proxy detection is per-file in the adapter; we gather across all diffs here.

    Must run under the same ``project_root_scope`` as symbol extraction: the
    proxy node's qualified_name / uid derive from ``module_name_from_path``,
    which falls back to ``cwd`` (yielding a filesystem-path-prefixed module like
    ``QA.repos.celery.celery._state``) when no project root is set. Without the
    scope the ProxyBinding's qn diverges from the canonical module qn of the
    same variable, orphaning the ``PROXY_OF`` anchor from the imports /
    re-exports that resolve to the variable node.
    """
    from context_engine.parser.registry import REGISTRY
    from context_engine.parser.uid import project_root_scope

    link_proxy = getattr(db, "link_proxy_bindings", None)
    reporter.stage_start("proxy_bindings", total=1)
    bindings: list[dict] = []
    if callable(link_proxy):
        with project_root_scope(project_path or None, workspace_id):
            for diff in diffs:
                ex = diff.extracted
                try:
                    language = REGISTRY.detect_language(ex.path)
                    adapter = REGISTRY.get_adapter(language)
                except Exception:
                    continue
                extract_proxy = getattr(adapter, "extract_proxy_bindings", None)
                if not callable(extract_proxy):
                    continue
                try:
                    bindings.extend(extract_proxy(ex.source, ex.path))
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


def _proxy_return_call_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str | None = None,
) -> int:
    """Relink ``L = self.M(); L.x()`` calls through a method-returned proxy.

    Sibling of :func:`_proxy_call_resolution_phase`. Where that forwards a direct
    ``proxyvar.x()`` call, this closes the case where the proxy is reached via a
    method return (``app = self._get_app(); app.send_task(...)``). The per-file
    adapter pass emits ``{caller_uid, callee_name, returns_global_qn}`` points-to
    candidates; the linker follows ``returns_global_qn`` → ``PROXY_OF`` → ``C``
    and wires ``caller -[CALLS_DYNAMIC{via_proxy_return}]-> C.callee_name``. Runs
    after proxy bindings exist (so ``PROXY_OF`` anchors are present) and before
    the AFFECTS rebuild (so the new call edge enters the impact closure). Under
    ``project_root_scope`` so the caller uid matches the stored node.
    """
    from context_engine.parser.adapters.python_adapter import PythonAdapter
    from context_engine.parser.uid import project_root_scope

    resolve = getattr(db, "resolve_proxy_return_calls", None)
    reporter.stage_start("proxy_return_calls", total=1)
    created = 0
    if callable(resolve):
        adapter = PythonAdapter()
        candidates: list[dict] = []
        with project_root_scope(project_path or None, workspace_id):
            for diff in diffs:
                ex = diff.extracted
                if not ex.path.endswith((".py", ".pyi")):
                    continue
                try:
                    candidates.extend(adapter.extract_self_method_proxy_calls(ex.source, ex.path))
                except Exception:
                    continue
        if candidates:
            created = resolve(candidates, workspace_id=workspace_id)
    reporter.step("proxy_return_calls")
    reporter.stage_end("proxy_return_calls")
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
    from context_engine.parser.adapters.python_adapter import PythonAdapter
    from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter
    from context_engine.parser.uid import project_root_scope

    link_deco = getattr(db, "link_decorators", None)
    link_compose = getattr(db, "link_decorator_compositions", None)
    reporter.stage_start("decorators", total=1)
    decorators: list[dict] = []
    compositions: list[dict] = []
    if callable(link_deco):
        py_adapter = PythonAdapter()
        ts_adapter = TypeScriptAdapter()
        with project_root_scope(project_path or None, workspace_id):
            decorators, compositions = _collect_decorator_facts(diffs, py_adapter, ts_adapter)
        if decorators:
            link_deco(decorators, workspace_id=workspace_id)
        if compositions and callable(link_compose):
            link_compose(compositions, workspace_id=workspace_id)
    reporter.step("decorators")
    reporter.stage_end("decorators")
    return len(decorators)


def _hook_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> int:
    """Create the EVENT channel + HOOK wrapper edges from hook facts.

    Named-hook/event transparency: a registration (``listen``/``listens_for``,
    ``@receiver``, ``.connect``) or a dispatch (``.dispatch.<name>(...)``,
    ``.send``) binds its site to (a) the EVENT topic it sub/pub-s and (b) the
    HOOK api wrapper it goes through. Same syntactic-fact basis as decorators /
    type references, and the same ``project_root_scope`` requirement so site
    uids match stored nodes. See ``Neo4jClient.link_hooks`` for the two layers.
    """
    from context_engine.parser.registry import REGISTRY
    from context_engine.parser.uid import project_root_scope

    link_hooks = getattr(db, "link_hooks", None)
    reporter.stage_start("hooks", total=1)
    hooks: list[dict] = []
    if callable(link_hooks):
        with project_root_scope(project_path or None, workspace_id):
            for diff in diffs:
                ex = diff.extracted
                try:
                    language = REGISTRY.detect_language(ex.path)
                    adapter = REGISTRY.get_adapter(language)
                except Exception:
                    continue
                extract_hooks = getattr(adapter, "extract_hooks", None)
                if not callable(extract_hooks):
                    continue
                try:
                    hooks.extend(extract_hooks(ex.source, ex.path))
                except Exception:
                    continue
            if hooks:
                link_hooks(hooks, workspace_id=workspace_id)
    reporter.step("hooks")
    reporter.stage_end("hooks")
    return len(hooks)


def _metadata_bridge_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> int:
    """Create METADATA_BRIDGE edges from reflect-metadata producer/consumer facts.

    The TS/JS analog of the hook/event archetype: a decorator's
    ``Reflect.defineMetadata(KEY, …)`` and its scanner's
    ``Reflect.getMetadata(KEY, …)`` are linked only by the shared KEY constant.
    Runs after ``_apply_graph`` (site symbols must exist) under
    ``project_root_scope`` so site uids match stored nodes, like hooks.
    """
    from context_engine.parser.registry import REGISTRY
    from context_engine.parser.uid import project_root_scope

    link_bridges = getattr(db, "link_metadata_bridges", None)
    reporter.stage_start("metadata_bridges", total=1)
    facts: list[dict] = []
    if callable(link_bridges):
        with project_root_scope(project_path or None, workspace_id):
            for diff in diffs:
                ex = diff.extracted
                try:
                    language = REGISTRY.detect_language(ex.path)
                    adapter = REGISTRY.get_adapter(language)
                except Exception:
                    continue
                extract_bridges = getattr(adapter, "extract_metadata_bridges", None)
                if not callable(extract_bridges):
                    continue
                try:
                    facts.extend(extract_bridges(ex.source, ex.path))
                except Exception:
                    continue
            if facts:
                link_bridges(facts, workspace_id=workspace_id)
    reporter.step("metadata_bridges")
    reporter.stage_end("metadata_bridges")
    return len(facts)


def _http_endpoint_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> int:
    """Create CALLS_ENDPOINT / IMPLEMENTS_ENDPOINT edges via shared ApiEndpoint nodes."""
    from context_engine.parser.registry import REGISTRY
    from context_engine.parser.uid import project_root_scope

    link_endpoints = getattr(db, "link_http_endpoints", None)
    reporter.stage_start("http_endpoints", total=1)
    facts: list[dict] = []
    if callable(link_endpoints):
        with project_root_scope(project_path or None, workspace_id):
            for diff in diffs:
                ex = diff.extracted
                try:
                    language = REGISTRY.detect_language(ex.path)
                    adapter = REGISTRY.get_adapter(language)
                except Exception:
                    continue
                extract_endpoints = getattr(adapter, "extract_http_endpoints", None)
                if not callable(extract_endpoints):
                    continue
                try:
                    facts.extend(extract_endpoints(ex.source, ex.path))
                except Exception:
                    continue
            if facts:
                link_endpoints(facts, workspace_id=workspace_id)
    reporter.step("http_endpoints")
    reporter.stage_end("http_endpoints")
    return len(facts)


def _attr_access_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> int:
    """Create READS_ATTR / WRITES_ATTR edges from accessor functions to
    attribute symbols.

    Runs after `_apply_graph` so the attribute symbols (class members,
    module-level vars) exist. Same ``project_root_scope`` requirement as
    decorators / type references — uids must match stored nodes.
    """
    from context_engine.parser.registry import REGISTRY
    from context_engine.parser.uid import project_root_scope

    link_attr = getattr(db, "link_attr_accesses", None)
    reporter.stage_start("attr_accesses", total=1)
    accesses: list[dict] = []
    if callable(link_attr):
        with project_root_scope(project_path or None, workspace_id):
            for diff in diffs:
                ex = diff.extracted
                try:
                    language = REGISTRY.detect_language(ex.path)
                    adapter = REGISTRY.get_adapter(language)
                except Exception:
                    continue
                extract_attr = getattr(adapter, "extract_attr_accesses", None)
                if not callable(extract_attr):
                    continue
                try:
                    accesses.extend(extract_attr(ex.source, ex.path))
                except Exception:
                    continue
        if accesses:
            link_attr(accesses, workspace_id=workspace_id)
    reporter.step("attr_accesses")
    reporter.stage_end("attr_accesses")
    return len(accesses)


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
    from context_engine.parser.registry import REGISTRY
    from context_engine.parser.uid import project_root_scope

    link_types = getattr(db, "link_type_references", None)
    reporter.stage_start("type_refs", total=1)
    references: list[dict] = []
    if callable(link_types):
        with project_root_scope(project_path or None, workspace_id):
            for diff in diffs:
                ex = diff.extracted
                try:
                    language = REGISTRY.detect_language(ex.path)
                    adapter = REGISTRY.get_adapter(language)
                except Exception:
                    continue
                extract_refs = getattr(adapter, "extract_type_references", None)
                if not callable(extract_refs):
                    continue
                try:
                    references.extend(extract_refs(ex.source, ex.path))
                except Exception:
                    continue
        if references:
            link_types(references, workspace_id=workspace_id)
    reporter.step("type_refs")
    reporter.stage_end("type_refs")
    return len(references)


def _symbol_alias_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> tuple[int, set[str]]:
    """Create REFERENCES edges for static symbol aliases.

    CommonJS export/require aliases are syntactic topology: one project symbol is
    surfaced as another. Runs after `_apply_graph` so both endpoints exist, and
    before degree recompute because REFERENCES participates in degree.
    """
    from context_engine.parser.uid import project_root_scope

    link_aliases = getattr(db, "link_symbol_references", None)
    reporter.stage_start("symbol_aliases", total=1)
    aliases: list[dict] = []
    linked = 0
    touched: set[str] = set()
    if callable(link_aliases):
        with project_root_scope(project_path or None, workspace_id):
            aliases = _collect_adapter_facts_from_diffs(diffs, "extract_symbol_aliases")
        if aliases:
            linked, touched = _parse_link_phase_result(
                link_aliases(aliases, workspace_id=workspace_id),
                len(aliases),
            )
    reporter.step("symbol_aliases")
    reporter.stage_end("symbol_aliases")
    return linked, touched


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
    from context_engine.parser.registry import REGISTRY
    from context_engine.parser.uid import project_root_scope

    link_reexports = getattr(db, "link_reexports", None)
    reporter.stage_start("reexports", total=1)
    reexports: list[dict] = []
    if callable(link_reexports):
        with project_root_scope(project_path or None, workspace_id):
            for diff in diffs:
                ex = diff.extracted
                try:
                    language = REGISTRY.detect_language(ex.path)
                    adapter = REGISTRY.get_adapter(language)
                except Exception:
                    continue
                extract_re = getattr(adapter, "extract_reexports", None)
                if not callable(extract_re):
                    continue
                try:
                    reexports.extend(extract_re(ex.source, ex.path))
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
    from context_engine.parser.registry import REGISTRY
    from context_engine.parser.uid import project_root_scope

    link_inst = getattr(db, "link_instantiations", None)
    reporter.stage_start("instantiations", total=1)
    instantiations: list[dict] = []
    if callable(link_inst):
        with project_root_scope(project_path or None, workspace_id):
            for diff in diffs:
                ex = diff.extracted
                try:
                    language = REGISTRY.detect_language(ex.path)
                    adapter = REGISTRY.get_adapter(language)
                except Exception:
                    continue
                extract_inst = getattr(adapter, "extract_instantiations", None)
                if not callable(extract_inst):
                    continue
                try:
                    instantiations.extend(extract_inst(ex.source, ex.path))
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
    from context_engine.parser.registry import REGISTRY
    from context_engine.parser.uid import project_root_scope

    link_inj = getattr(db, "link_injections", None)
    reporter.stage_start("injections", total=1)
    injections: list[dict] = []
    if callable(link_inj):
        with project_root_scope(project_path or None, workspace_id):
            for diff in diffs:
                ex = diff.extracted
                try:
                    language = REGISTRY.detect_language(ex.path)
                    adapter = REGISTRY.get_adapter(language)
                except Exception:
                    continue
                extract_inj = getattr(adapter, "extract_injections", None)
                if not callable(extract_inj):
                    continue
                try:
                    injections.extend(extract_inj(ex.source, ex.path))
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
    from context_engine.indexer.mro_api_bridge import MroApiBridgeIndexer

    reporter.stage_start("mro_api_bridge", total=1)
    created = MroApiBridgeIndexer(db).apply(workspace_id)
    reporter.stage_end("mro_api_bridge")
    return created


def _property_api_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> tuple[int, set[str]]:
    """Create HAS_API edges from property-owner API assignments."""
    from context_engine.parser.uid import project_root_scope

    link_api = getattr(db, "link_symbol_api_edges", None)
    reporter.stage_start("property_api", total=1)
    edges = []
    linked = 0
    touched: set[str] = set()
    if callable(link_api):
        with project_root_scope(project_path or None, workspace_id):
            edges = _collect_adapter_facts_from_diffs(diffs, "extract_property_api_edges")
        if edges:
            linked, touched = _parse_link_phase_result(
                link_api(edges, workspace_id=workspace_id),
                len(edges),
            )
    reporter.step("property_api")
    reporter.stage_end("property_api")
    return linked, touched


def build_symbol_docs_for_extracted(
    ex: ExtractedFile,
    *,
    changed_uids: set[str],
    workspace_id: str,
    project_path: str = "",
    graph_probe: GraphContextProbe | None = None,
    include_axis_facts: bool = False,
) -> list[dict]:
    """Build Lance symbol rows for changed symbols in one extracted file."""
    source_lines = ex.source.splitlines()
    # File tier is a per-file structural property: path topology +
    # whether the module body is a pure re-export. Computed once per
    # file and stamped on every symbol row so the axis ranker can
    # demote noise tiers in seed retrieval (see file_tier.py).
    # Classify on the path RELATIVE to the indexed project root — the
    # absolute path carries infra segments (e.g. ``QA/repos/...``) that
    # would otherwise be mistaken for tier markers.
    tier_path = os.path.relpath(ex.path, project_path) if project_path else ex.path
    file_tier_value = classify_file_tier(
        tier_path, pure_reexport=is_pure_reexport_source(ex.source)
    )
    axis_payloads = (
        _axis_payloads_for_extracted_file(
            ex,
            project_path=project_path,
            graph_probe=graph_probe,
        )
        if include_axis_facts and changed_uids
        else {}
    )
    symbol_docs: list[dict] = []
    for sym in ex.symbols:
        if sym.uid not in changed_uids:
            continue
        code = "\n".join(source_lines[sym.start_line - 1 : sym.end_line])
        if not include_axis_facts:
            symbol_docs.append(
                {
                    "uid": sym.uid,
                    "name": sym.name,
                    "file_path": sym.file_path,
                    "workspace_id": workspace_id,
                    "code": code,
                }
            )
            continue
        row = {
            "uid": sym.uid,
            "name": sym.name,
            "symbol_kind": sym.kind,
            "qualified_name": sym.qualified_name or "",
            "file_path": sym.file_path,
            "workspace_id": workspace_id,
            "code": code,
            "file_tier": file_tier_value,
        }
        row.update(axis_payloads.get(sym.uid) or axis_payloads.get(sym.qualified_name) or {})
        symbol_docs.append(row)
    return symbol_docs


def run_axis_incremental_finalize(
    db: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str,
    *,
    seed_uids: set[str] | None = None,
    project_path: str = "",
) -> dict[str, int]:
    """Run workspace-level axis propagation and adjacency after incremental indexing."""
    if getattr(lance, "index_profile_name", "") != AXIS_PYTHON_V1_PROFILE:
        return {}

    from context_engine.indexer.fast.error_dispatch_propagation import propagate_error_dispatch
    from context_engine.indexer.fast.proxy_object_propagation import propagate_proxy_object
    from context_engine.indexer.fast.registry_class_inheritance import (
        propagate_error_model_via_inheritance,
        propagate_registry_class_via_inheritance,
    )

    stats: dict[str, int] = {}
    stats["registry_class_propagated"] = propagate_registry_class_via_inheritance(
        db,
        lance,
        workspace_id,
        project_path=project_path,
    )
    stats["error_model_propagated"] = propagate_error_model_via_inheritance(
        db,
        lance,
        workspace_id,
        project_path=project_path,
    )
    stats["error_dispatch_propagated"] = propagate_error_dispatch(db, lance, workspace_id)
    stats["proxy_object_propagated"] = propagate_proxy_object(db, lance, workspace_id)
    stats["axis_adjacency_materialized"] = _adjacency_materialization_phase(
        db,
        lance,
        workspace_id,
        _NullReporter(),
        seed_uids={uid for uid in (seed_uids or set()) if uid},
    )
    return stats


def _embed_phase(
    diffs: list[FileDiff],
    lance: LanceDBClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
    graph_probe: GraphContextProbe | None = None,
) -> tuple[int, int]:
    """One global encode+upsert call. Returns (changed_count, removed_count)."""
    symbol_docs: list[dict] = []
    removed_uids: list[str] = []
    include_axis_facts = getattr(lance, "index_profile_name", "") == AXIS_PYTHON_V1_PROFILE

    for diff in diffs:
        changed_set = {s.uid for s in diff.changed_symbols}
        symbol_docs.extend(
            build_symbol_docs_for_extracted(
                diff.extracted,
                changed_uids=changed_set,
                workspace_id=workspace_id,
                project_path=project_path,
                graph_probe=graph_probe,
                include_axis_facts=include_axis_facts,
            )
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


class _PeerAwarePeerProbe:
    """Wrap a ``GraphContextProbe`` with a per-file peer-kind lookup.

    Delegates every probe method to the wrapped base probe (or to a
    ``NullGraphProbe``-style return when no base probe is present) and only
    overrides ``peer_container_kinds_for`` to consult the supplied
    ``peer_kinds_by_qn`` map. The map carries the container kinds of
    every non-class profile in the same file, keyed by qualified_name.
    """

    def __init__(
        self,
        base: GraphContextProbe | None,
        peer_kinds_by_qn: dict[str, set[str]],
    ) -> None:
        self._base = base
        self._peer_kinds_by_qn = peer_kinds_by_qn

    def peer_container_kinds_for(self, qualified_name_prefix: str) -> set[str]:
        collected: set[str] = set()
        for qn, kinds in self._peer_kinds_by_qn.items():
            if qn.startswith(qualified_name_prefix):
                collected |= kinds
        return collected

    def outgoing_kind_edges(self, symbol_uid, kinds):
        if self._base is None:
            return 0
        return self._base.outgoing_kind_edges(symbol_uid, kinds)

    def library_marker_kinds(self, symbol_uid):
        if self._base is None:
            return set()
        return self._base.library_marker_kinds(symbol_uid)

    def caller_package_dispersion(self, symbol_uid):
        if self._base is None:
            return 0.0
        return self._base.caller_package_dispersion(symbol_uid)

    def is_cfg_driver(self, symbol_uid):
        if self._base is None:
            return False
        return self._base.is_cfg_driver(symbol_uid)

    def is_event_signal(self, symbol_uid):
        if self._base is None:
            return False
        return self._base.is_event_signal(symbol_uid)

    def outgoing_handles_count(self, symbol_uid):
        if self._base is None:
            return 0
        return self._base.outgoing_handles_count(symbol_uid)

    def outgoing_injects_count(self, symbol_uid):
        if self._base is None:
            return 0
        return self._base.outgoing_injects_count(symbol_uid)

    def metadata_bridge_keys(self, symbol_uid):
        if self._base is None:
            return ()
        fn = getattr(self._base, "metadata_bridge_keys", None)
        if not callable(fn):
            return ()
        return fn(symbol_uid)


def _load_axis_extraction_for_file(
    ex: ExtractedFile,
    *,
    project_path: str = "",
):
    from context_engine.axis.schema import AxisExtraction
    from context_engine.parser.registry import REGISTRY

    if ex.axis_facts is not None:
        facts = ex.axis_facts
    else:
        try:
            adapter = REGISTRY.get_adapter(REGISTRY.detect_language(ex.path))
        except ValueError:
            facts = []
        else:
            facts = adapter.extract_axis_facts(
                ex.source,
                ex.path,
                symbols=ex.symbols,
                project_root=project_path or None,
            )
    return AxisExtraction(file_path=ex.path, facts=facts)


def _merge_profile_fact(target, fact, target_uid):
    from context_engine.axis.schema import AxisFact

    if fact.symbol_uid == target_uid:
        target.add_fact(fact)
        return
    target.add_fact(
        AxisFact(
            symbol_uid=target_uid,
            qualified_name=fact.qualified_name,
            symbol_kind=fact.symbol_kind,
            axis=fact.axis,
            bit=fact.bit,
            line=fact.line,
            evidence=fact.evidence,
            ast_kind=fact.ast_kind,
            payload=dict(fact.payload),
        )
    )


def _variable_stub_profiles(
    ex: ExtractedFile,
    profiles_by_uid: dict[str, AxisProfile],
    *,
    graph_probe: GraphContextProbe | None,
) -> None:
    from context_engine.axis.schema import AxisFact, AxisProfile

    for sym in ex.symbols:
        if sym.kind != "variable" or sym.uid in profiles_by_uid:
            continue
        stub = AxisProfile(
            symbol_uid=sym.uid,
            qualified_name=sym.qualified_name,
            symbol_kind="variable",
        )
        handles_count = (
            graph_probe.outgoing_handles_count(sym.uid) if graph_probe is not None else 0
        )
        if handles_count > 0:
            stub.add_fact(
                AxisFact(
                    symbol_uid=sym.uid,
                    qualified_name=sym.qualified_name,
                    symbol_kind="variable",
                    axis="dfg",
                    bit="registered_callable",
                    line=sym.start_line,
                    evidence=f"<handles:{handles_count}>",
                    ast_kind="GraphProbe",
                    payload={"count": handles_count},
                )
            )
        profiles_by_uid[sym.uid] = stub


def _merge_extraction_profiles(
    extraction,
    ex: ExtractedFile,
    *,
    graph_probe: GraphContextProbe | None,
) -> dict[str, AxisProfile]:
    from context_engine.axis.schema import AxisProfile

    parser_uid_by_qn = {s.qualified_name: s.uid for s in ex.symbols if s.qualified_name}
    profiles_by_uid: dict[str, AxisProfile] = {}
    for profile in extraction.profiles.values():
        target_uid = parser_uid_by_qn.get(profile.qualified_name, profile.symbol_uid)
        target = profiles_by_uid.get(target_uid)
        if target is None:
            target = AxisProfile(
                symbol_uid=target_uid,
                qualified_name=profile.qualified_name,
                symbol_kind=profile.symbol_kind,
            )
            profiles_by_uid[target_uid] = target
        for fact in profile.facts:
            _merge_profile_fact(target, fact, target_uid)
    _variable_stub_profiles(ex, profiles_by_uid, graph_probe=graph_probe)
    return profiles_by_uid


def _add_injection_probe_facts(
    profiles_by_uid: dict[str, AxisProfile],
    graph_probe: GraphContextProbe | None,
) -> None:
    from context_engine.axis.schema import AxisFact

    if graph_probe is None:
        return
    for uid, profile in profiles_by_uid.items():
        if profile.symbol_kind not in {"function", "method"}:
            continue
        injects_count = graph_probe.outgoing_injects_count(uid)
        if injects_count <= 0:
            continue
        profile.add_fact(
            AxisFact(
                symbol_uid=uid,
                qualified_name=profile.qualified_name,
                symbol_kind=profile.symbol_kind,
                axis="dfg",
                bit="injected_dependency",
                line=0,
                evidence=f"<injects:{injects_count}>",
                ast_kind="GraphProbe",
                payload={"count": injects_count},
            )
        )


def _classify_profiles_by_uid(
    profiles_by_uid: dict[str, AxisProfile],
    base_probe: GraphContextProbe | None,
) -> dict[str, list]:
    from context_engine.axis.container_kind import ContainerKindClassifier, GraphContextProbe

    classifier = (
        ContainerKindClassifier(probe=base_probe)
        if base_probe is not None
        else ContainerKindClassifier()
    )
    container_kinds_by_uid: dict[str, list] = {}
    for uid, profile in profiles_by_uid.items():
        if profile.symbol_kind == "class":
            continue
        container_kinds_by_uid[uid] = classifier.classify(profile)

    peer_kinds_by_qn: dict[str, set[str]] = {}
    for uid, matches in container_kinds_by_uid.items():
        prof = profiles_by_uid[uid]
        if not matches:
            continue
        peer_kinds_by_qn.setdefault(prof.qualified_name, set()).update(
            match.kind for match in matches
        )

    class_probe = cast(GraphContextProbe, _PeerAwarePeerProbe(base_probe, peer_kinds_by_qn))
    class_classifier = ContainerKindClassifier(probe=class_probe)
    for uid, profile in profiles_by_uid.items():
        if profile.symbol_kind != "class":
            continue
        container_kinds_by_uid[uid] = class_classifier.classify(profile)
    return container_kinds_by_uid


def _compile_axis_payloads(
    profiles_by_uid: dict[str, AxisProfile],
    container_kinds_by_uid: dict[str, list],
    contract_compiler,
) -> dict[str, dict]:
    payloads: dict[str, dict] = {}
    for profile in profiles_by_uid.values():
        container_kinds = container_kinds_by_uid.get(profile.symbol_uid, [])
        contracts = contract_compiler.compile(profile, container_kinds)
        payload = {
            "ast_kind_bits": sorted({fact.ast_kind for fact in profile.facts}),
            "cfg_bits": sorted(profile.cfg_bits),
            "dfg_bits": sorted(profile.dfg_bits),
            "struct_bits": sorted(profile.struct_bits),
            "container_kinds": sorted({match.kind for match in container_kinds}),
            "axis_evidence_json": json.dumps(
                [fact.to_dict() for fact in profile.facts],
                sort_keys=True,
            ),
            "axis_container_kinds_json": json.dumps(
                [match.to_dict() for match in container_kinds],
                sort_keys=True,
            ),
            "axis_contracts_json": json.dumps(
                [match.to_dict() for match in contracts],
                sort_keys=True,
            ),
        }
        payloads[profile.symbol_uid] = payload
        payloads[profile.qualified_name] = payload
    return payloads


def _axis_payloads_for_extracted_file(
    ex: ExtractedFile,
    *,
    project_path: str = "",
    graph_probe: GraphContextProbe | None = None,
) -> dict[str, dict]:
    """Return per-symbol axis payloads keyed by uid and qualified name.

    UID generation should line up with parser symbols, but this first isolated
    index keeps a qualified-name fallback so signature-normalization drift does
    not silently drop physical AST facts.
    """
    from context_engine.axis.contract_compiler import AxisContractCompiler

    extraction = _load_axis_extraction_for_file(ex, project_path=project_path)
    base_probe = graph_probe if graph_probe is not None else None
    contract_compiler = AxisContractCompiler()
    profiles_by_uid = _merge_extraction_profiles(
        extraction,
        ex,
        graph_probe=graph_probe,
    )
    _add_injection_probe_facts(profiles_by_uid, graph_probe)
    container_kinds_by_uid = _classify_profiles_by_uid(profiles_by_uid, base_probe)
    return _compile_axis_payloads(profiles_by_uid, container_kinds_by_uid, contract_compiler)


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

    from context_engine.indexer.affects import AFFECTSIndexer

    reporter.stage_start("affects", total=len(union))
    AFFECTSIndexer(db).rebuild_affects(
        union,
        workspace_id=workspace_id,
        progress_callback=lambda n: reporter.step("affects", n=n),
    )
    reporter.stage_end("affects")
    return len(union)


def _adjacency_materialization_phase(
    db: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str,
    reporter: ProgressReporter,
    seed_uids: set[str] | None = None,
) -> int:
    """Snapshot workspace graph-walk adjacency into LanceDB."""
    from context_engine.indexer.fast.adjacency_materialization import (
        materialize_axis_adjacency,
        materialize_axis_adjacency_subset,
    )

    reporter.stage_start("axis_adjacency", total=1)
    seeds = {uid for uid in (seed_uids or set()) if uid}
    count_rows = getattr(lance, "count_axis_adjacency_workspace", None)
    existing = int(count_rows(workspace_id)) if callable(count_rows) and seeds else 0
    if existing > 0 and seeds:
        rows = materialize_axis_adjacency_subset(db, lance, workspace_id, seeds)
    else:
        rows = materialize_axis_adjacency(db, lance, workspace_id)
    reporter.step("axis_adjacency")
    reporter.stage_end("axis_adjacency")
    return rows


def _ensure_adjacency_materialized(
    db: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    count_rows = getattr(lance, "count_axis_adjacency_workspace", None)
    load_external = getattr(lance, "load_axis_adjacency_external", None)
    if callable(count_rows):
        try:
            if int(count_rows(workspace_id)) > 0:
                if callable(load_external) and load_external(workspace_id) is not None:
                    return 0
        except Exception:
            return 0
    return _adjacency_materialization_phase(db, lance, workspace_id, reporter)


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


def _fast_indexing_initial_stats(
    project_path: str,
    base_workspace_id: str,
    workspace_id: str,
    profile,
    *,
    skip_affects: bool,
) -> dict:
    return {
        "project_path": project_path,
        "base_workspace_id": base_workspace_id,
        "workspace_id": workspace_id,
        "index_profile": profile.name,
        "index_profile_schema_version": profile.schema_version,
        "index_profile_language_scope": profile.language_scope,
        "lancedb_docs_table": profile.docs_table,
        "lancedb_symbols_table": profile.symbols_table,
        "skip_affects": skip_affects,
        "performed": True,
        "skipped": False,
        "collected": 0,
        "tombstoned": 0,
        "changed": 0,
        "parsed": 0,
        "symbols_encoded": 0,
        "symbols_removed": 0,
        "affects_rebuilt": 0,
        "axis_adjacency_materialized": 0,
        "docs_files_indexed": 0,
        "docs_chunks_indexed": 0,
        "docs_timings_sec": {},
        "timings_sec": {},
        "repository_profile": build_empty_repository_profile(
            project_path, workspace_id, reason="not_built"
        ),
        "repository_profile_store": "",
    }


def _finish_no_indexable_files(
    stats: dict,
    db: Neo4jClient,
    *,
    workspace_id: str,
    project_path: str,
) -> dict:
    _use_repository_profile(
        stats,
        build_empty_repository_profile(project_path, workspace_id, reason="no_indexable_files"),
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


def _finish_tombstone_only(
    stats: dict,
    db: Neo4jClient,
    *,
    workspace_id: str,
    project_path: str,
    tombstoned_paths: list[str],
    tombstone_uids: Collection[str],
    skip_affects: bool,
    reporter: ProgressReporter,
) -> dict:
    if tombstone_uids and not skip_affects:
        t_stage = time.perf_counter()
        stats["affects_rebuilt"] = _rebuild_affects_for_uids(
            tombstone_uids, db, workspace_id, reporter
        )
        stats["timings_sec"]["affects"] = round(time.perf_counter() - t_stage, 3)
    persist_index_manifest(
        stats=stats,
        db=db,
        workspace_id=workspace_id,
        project_path=project_path,
        outcome="tombstone_only",
    )
    print(f"🪦 Tombstoned {len(tombstoned_paths)} stale indexed file(s).")
    return stats


def _finish_unchanged_files(
    stats: dict,
    db: Neo4jClient,
    lance: LanceDBClient,
    *,
    workspace_id: str,
    project_path: str,
    files: list[str],
    tombstoned_paths: list[str],
    tombstone_uids: Collection[str],
    skip_affects: bool,
    reporter: ProgressReporter,
) -> dict:
    if tombstone_uids and not skip_affects:
        t_stage = time.perf_counter()
        stats["affects_rebuilt"] = _rebuild_affects_for_uids(
            tombstone_uids, db, workspace_id, reporter
        )
        stats["timings_sec"]["affects"] = round(time.perf_counter() - t_stage, 3)
    get_profile = getattr(db, "get_repository_profile", None)
    existing_profile = get_profile(workspace_id=workspace_id) if callable(get_profile) else None
    if existing_profile:
        stats["repository_profile"] = existing_profile
        stats["repository_profile_store"] = "neo4j_workspace"
    else:
        _use_repository_profile(
            stats,
            _build_profile_from_diffs(project_path, workspace_id, files, [], stats, db),
            db,
            workspace_id,
        )
    print(f"   readiness={summarize_repository_profile(stats['repository_profile'])}")
    t_stage = time.perf_counter()
    stats["axis_adjacency_materialized"] = _ensure_adjacency_materialized(
        db,
        lance,
        workspace_id,
        reporter,
    )
    if stats["axis_adjacency_materialized"]:
        print(f"🕸️  Axis adjacency: materialized {stats['axis_adjacency_materialized']} rows")
    stats["timings_sec"]["axis_adjacency"] = round(time.perf_counter() - t_stage, 3)
    if tombstoned_paths:
        print(f"🪦 Tombstoned {len(tombstoned_paths)} stale indexed file(s).")
    print("✅ All files up-to-date, nothing to re-index.")
    persist_index_manifest(
        stats=stats,
        db=db,
        workspace_id=workspace_id,
        project_path=project_path,
        outcome="noop_unchanged" if not tombstoned_paths else "tombstone_noop",
    )
    return stats


def _run_axis_python_propagation_stage(
    db: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str,
    project_path: str,
    stats: dict,
) -> None:
    from context_engine.indexer.fast.error_dispatch_propagation import propagate_error_dispatch
    from context_engine.indexer.fast.proxy_object_propagation import propagate_proxy_object
    from context_engine.indexer.fast.registry_class_inheritance import (
        propagate_error_model_via_inheritance,
        propagate_registry_class_via_inheritance,
    )

    stats["registry_class_propagated"] = propagate_registry_class_via_inheritance(
        db,
        lance,
        workspace_id,
        project_path=project_path,
    )
    stats["error_model_propagated"] = propagate_error_model_via_inheritance(
        db,
        lance,
        workspace_id,
        project_path=project_path,
    )
    stats["error_dispatch_propagated"] = propagate_error_dispatch(db, lance, workspace_id)
    stats["proxy_object_propagated"] = propagate_proxy_object(db, lance, workspace_id)


def _run_fast_changed_files_pipeline(
    *,
    stats: dict,
    changed_files: list[str],
    files: list[str],
    project_path: str,
    current_hashes: dict[str, str],
    db: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str,
    parse_workers: int,
    job_log: IndexJobLog,
    reporter: ProgressReporter,
    profile,
    skip_affects: bool,
) -> None:
    print(f"🔄 {len(changed_files)}/{len(files)} files changed")

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
        include_axis_facts=profile.name == AXIS_PYTHON_V1_PROFILE,
    )
    stats["parsed"] = len(diffs)
    stats["timings_sec"]["parse"] = round(time.perf_counter() - t_stage, 3)

    degree_seeds, degree_removed = _degree_seeds_snapshot(diffs, db, workspace_id)
    t_stage = time.perf_counter()
    _apply_graph(diffs, db, workspace_id, reporter)
    stats["timings_sec"]["graph"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    ext_calls, ext_imports = _external_boundary_phase(
        diffs,
        db,
        workspace_id,
        project_path,
        files,
        reporter,
    )
    stats["external_calls_linked"] = ext_calls
    stats["external_imports_linked"] = ext_imports
    stats["timings_sec"]["external_boundary"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    stats["extends_external_edges"] = _extends_external_phase(db, workspace_id, reporter)
    stats["timings_sec"]["extends_external"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    stats["integrates_with_edges"] = _integrates_with_phase(db, workspace_id, reporter)
    stats["timings_sec"]["integrates_with"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    stats["mro_api_edges"] = _mro_api_bridge_phase(db, workspace_id, reporter)
    stats["timings_sec"]["mro_api_bridge"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    property_api_count, property_api_uids = _property_api_phase(
        diffs,
        db,
        workspace_id,
        reporter,
        project_path,
    )
    stats["property_api_edges"] = property_api_count
    stats["timings_sec"]["property_api"] = round(time.perf_counter() - t_stage, 3)
    degree_seeds |= property_api_uids

    # Clear stale per-file derived edges BEFORE the create-only phases below
    # (parity with index_file's delete-then-link). The proxy phase must come
    # after this clear: ``_clear_derived_edges_for_diffs`` DETACH-DELETEs
    # proxy_binding nodes, so running it after _proxy_binding_phase would wipe
    # the bindings it just created and leave the final graph without them.
    t_stage = time.perf_counter()
    _clear_derived_edges_for_diffs(diffs, db, workspace_id, reporter)
    stats["timings_sec"]["clear_derived_edges"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    proxy_uids = _proxy_binding_phase(diffs, db, workspace_id, reporter, project_path)
    stats["proxy_bindings"] = len(proxy_uids)
    stats["proxy_calls_resolved"] = _proxy_call_resolution_phase(diffs, db, workspace_id, reporter)
    stats["proxy_return_calls_resolved"] = _proxy_return_call_phase(
        diffs, db, workspace_id, reporter, project_path
    )
    stats["timings_sec"]["proxy"] = round(time.perf_counter() - t_stage, 3)
    degree_seeds |= proxy_uids

    t_stage = time.perf_counter()
    stats["attr_accesses_linked"] = _attr_access_phase(
        diffs, db, workspace_id, reporter, project_path
    )
    stats["timings_sec"]["attr_accesses"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    stats["type_refs_linked"] = _type_reference_phase(
        diffs, db, workspace_id, reporter, project_path
    )
    stats["timings_sec"]["type_refs"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    alias_count, alias_uids = _symbol_alias_phase(diffs, db, workspace_id, reporter, project_path)
    stats["symbol_aliases_linked"] = alias_count
    stats["timings_sec"]["symbol_aliases"] = round(time.perf_counter() - t_stage, 3)
    degree_seeds |= alias_uids

    t_stage = time.perf_counter()
    stats["reexports_linked"] = _reexport_phase(diffs, db, workspace_id, reporter, project_path)
    stats["timings_sec"]["reexports"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    stats["instantiations_linked"] = _instantiation_phase(
        diffs, db, workspace_id, reporter, project_path
    )
    stats["timings_sec"]["instantiations"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    stats["decorators_linked"] = _decorator_phase(diffs, db, workspace_id, reporter, project_path)
    stats["timings_sec"]["decorators"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    stats["hooks_linked"] = _hook_phase(diffs, db, workspace_id, reporter, project_path)
    stats["timings_sec"]["hooks"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    stats["metadata_bridges_linked"] = _metadata_bridge_phase(
        diffs, db, workspace_id, reporter, project_path
    )
    stats["timings_sec"]["metadata_bridges"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    stats["http_endpoints_linked"] = _http_endpoint_phase(
        diffs, db, workspace_id, reporter, project_path
    )
    stats["timings_sec"]["http_endpoints"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    stats["injections_linked"] = _injection_phase(diffs, db, workspace_id, reporter, project_path)
    stats["timings_sec"]["injections"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    stats["degree_recomputed"] = _degree_phase(
        degree_seeds, degree_removed, db, workspace_id, reporter
    )
    stats["timings_sec"]["degree"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    graph_probe = None
    if profile.name == AXIS_PYTHON_V1_PROFILE:
        from context_engine.axis.graph_probe import Neo4jGraphContextProbe

        graph_probe = Neo4jGraphContextProbe(db, workspace_id)
    encoded, removed = _embed_phase(
        diffs,
        lance,
        workspace_id,
        reporter,
        project_path,
        graph_probe=graph_probe,
    )
    stats["symbols_encoded"] = encoded
    stats["symbols_removed"] = removed
    stats["timings_sec"]["embed"] = round(time.perf_counter() - t_stage, 3)

    if profile.name == AXIS_PYTHON_V1_PROFILE:
        t_stage = time.perf_counter()
        _run_axis_python_propagation_stage(
            db,
            lance,
            workspace_id,
            project_path,
            stats,
        )
        stats["timings_sec"]["registry_class_inheritance"] = round(time.perf_counter() - t_stage, 3)

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

    t_stage = time.perf_counter()
    stats["axis_adjacency_materialized"] = _adjacency_materialization_phase(
        db,
        lance,
        workspace_id,
        reporter,
        seed_uids=degree_seeds - degree_removed,
    )
    stats["timings_sec"]["axis_adjacency"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    from context_engine.indexer.anchor import ingest_symbol_docstrings, resolve_pending_anchors
    from context_engine.indexer.file_tier import classify_file_tier, is_pure_reexport_source

    reporter.stage_start("docs", total=1)
    all_symbols = []
    removed_uids: list[str] = []
    file_tier_by_path: dict[str, str] = {}
    for diff in diffs:
        ex = diff.extracted
        tier_path = os.path.relpath(ex.path, project_path) if project_path else ex.path
        tier_value = classify_file_tier(
            tier_path,
            pure_reexport=is_pure_reexport_source(ex.source),
        )
        file_tier_by_path[tier_path] = tier_value
        file_tier_by_path[ex.path] = tier_value
        all_symbols.extend(ex.symbols)
        removed_uids.extend(diff.removed_uids)
    doc_stats = ingest_symbol_docstrings(
        db,
        lance,
        all_symbols,
        workspace_id=workspace_id,
        allowed_prefixes=[project_path],
        removed_owner_uids=removed_uids,
        file_tier_by_path=file_tier_by_path,
    )
    stats["docstring_anchors"] = doc_stats
    resolve_pending_anchors(
        db,
        lance,
        workspace_id=workspace_id,
        allowed_prefixes=[project_path],
    )
    reporter.step("docs")
    reporter.stage_end("docs")
    stats["timings_sec"]["docs"] = round(time.perf_counter() - t_stage, 3)

    t_stage = time.perf_counter()
    from context_engine.indexer.role_clustering import derive_and_persist_role_taxonomy

    reporter.stage_start("role_clustering", total=1)
    summary = derive_and_persist_role_taxonomy(db, workspace_id)
    reporter.step("role_clustering")
    reporter.stage_end("role_clustering")
    stats["timings_sec"]["role_clustering"] = round(time.perf_counter() - t_stage, 3)
    stats["role_taxonomy"] = {
        "method": summary.method,
        "sample_size": summary.sample_size,
        "filtered_sample_size": summary.filtered_sample_size,
        "present_role_count": len(summary.present_roles),
    }
    stats["role_catalog"] = {
        "present_roles": len(summary.present_roles),
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


def run_fast_indexing(
    project_path: str,
    workspace_id: str | None = None,
    *,
    index_profile: str | None = None,
    hash_workers: int | None = None,
    parse_workers: int | None = None,
    skip_affects: bool = False,
    reporter: ProgressReporter | None = None,
    user_id: str = "anonymous",
) -> dict:
    """Drop-in alternative to ``context_engine.indexer.code.run_indexing``.

    Returns a stats dict so callers can compare runs against the baseline.
    Pass ``reporter`` to wire up progress bars; defaults to no-op.
    """
    hash_workers = hash_workers or _DEFAULT_HASH_WORKERS
    parse_workers = parse_workers or _DEFAULT_PARSE_WORKERS
    reporter = reporter or _NullReporter()

    profile = resolve_index_profile(index_profile) if index_profile else active_index_profile()
    base_workspace_id = workspace_id or WorkspaceResolver().from_project_path(project_path).id
    workspace_id = effective_index_workspace_id(base_workspace_id, profile=profile)
    # Request-scoped view over the process-wide driver (audit-tagged), not a
    # second raw driver. The finally's db.close() is a no-op on this view.
    db = get_database_provider().client_for(user_id)
    lance = LanceDBClient(index_profile=profile)
    job_log = IndexJobLog()

    stats = _fast_indexing_initial_stats(
        project_path,
        base_workspace_id,
        workspace_id,
        profile,
        skip_affects=skip_affects,
    )

    t0 = time.perf_counter()
    print(f"🚀 Fast indexing: {project_path} ({workspace_id})")
    if profile.name != "legacy":
        print(f"   profile={profile.name} base_workspace={base_workspace_id}")
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

        # Stage 1b: tombstone indexed files no longer in the committed collect set
        t_stage = time.perf_counter()
        tombstoned_paths, tombstone_uids = _tombstone_phase(
            db,
            lance,
            workspace_id=workspace_id,
            project_path=project_path,
            active_paths=files,
            reporter=reporter,
        )
        stats["tombstoned"] = len(tombstoned_paths)
        stats["timings_sec"]["tombstone"] = round(time.perf_counter() - t_stage, 3)

        if not files and not tombstoned_paths:
            return _finish_no_indexable_files(
                stats,
                db,
                workspace_id=workspace_id,
                project_path=project_path,
            )

        if not files and tombstoned_paths:
            return _finish_tombstone_only(
                stats,
                db,
                workspace_id=workspace_id,
                project_path=project_path,
                tombstoned_paths=tombstoned_paths,
                tombstone_uids=tombstone_uids,
                skip_affects=skip_affects,
                reporter=reporter,
            )

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
            return _finish_unchanged_files(
                stats,
                db,
                lance,
                workspace_id=workspace_id,
                project_path=project_path,
                files=files,
                tombstoned_paths=tombstoned_paths,
                tombstone_uids=tombstone_uids,
                skip_affects=skip_affects,
                reporter=reporter,
            )
        _run_fast_changed_files_pipeline(
            stats=stats,
            changed_files=changed_files,
            files=files,
            project_path=project_path,
            current_hashes=current_hashes,
            db=db,
            lance=lance,
            workspace_id=workspace_id,
            parse_workers=parse_workers,
            job_log=job_log,
            reporter=reporter,
            profile=profile,
            skip_affects=skip_affects,
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
