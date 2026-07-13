"""Individual indexing phases run in sequence by the pipeline orchestrator."""

from __future__ import annotations

from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.neo4j_client import Neo4jClient
from context_engine.index_profile import (
    AXIS_PYTHON_V1_PROFILE,
)
from context_engine.indexer.external_boundary import (
    build_project_boundary,
    package_manifest_external_roots,
)
from context_engine.indexer.external_facts import (
    collect_external_call_links,
    collect_external_import_links,
    collect_external_symbol_import_links,
    external_call_link_rows,
    external_import_link_rows,
    external_symbol_import_rows,
)
from context_engine.indexer.fast.extractor import FastExtractor, hash_file
from context_engine.indexer.job_log import IndexJobLog

if TYPE_CHECKING:
    from context_engine.axis.container_kind import GraphContextProbe

from context_engine.indexer.fast.axis_payloads import build_symbol_docs_for_extracted
from context_engine.indexer.fast.pipeline_types import (
    FileDiff,
    ProgressReporter,
    _collect_adapter_facts_from_diffs,
    _collect_decorator_facts,
    _parse_link_phase_result,
    _symbol_needs_upsert,
)


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


# ---------------------------------------------------------------------------
# Process-pool parsing.
#
# ``extract_all`` is side-effect-free (thread-local adapters, no DB access),
# so parse workers can be OS processes: the CPython AST/axis pass holds the
# GIL, which is why thread workers never scaled. Everything stateful stays in
# the parent — the job log (sqlite), Neo4j, Lance and the reporter are never
# touched from a worker, so there is no lock/retry surface to collide on.
# Workers are ``spawn``ed (not forked) so they inherit no parent DB handles.
# ---------------------------------------------------------------------------

# Below this many changed files the pool spin-up (spawned interpreters +
# module imports) costs more than it saves; incremental updates and small
# fixtures stay on the in-thread path.
_PROCESS_POOL_MIN_FILES = 32
_PROCESS_CHUNK_FILES = 8

_PROC_EXTRACTOR: FastExtractor | None = None


def _parse_pool_init(project_root: str, workspace_id: str, include_axis_facts: bool) -> None:
    global _PROC_EXTRACTOR
    _PROC_EXTRACTOR = FastExtractor(
        project_root=project_root,
        workspace_id=workspace_id,
        include_axis_facts=include_axis_facts,
    )


def _parse_chunk_in_process(
    chunk: list[tuple[str, dict[str, dict]]],
) -> list[tuple[str, FileDiff | None, str]]:
    """Parse one chunk of files inside a worker process.

    Returns ``(path, diff, error)`` per file; per-file failures are carried
    back as strings so the parent can record them in the job log without one
    bad file poisoning the chunk.
    """
    out: list[tuple[str, FileDiff | None, str]] = []
    for path, existing in chunk:
        try:
            out.append((path, _parse_one(path, _PROC_EXTRACTOR, existing), ""))
        except Exception as exc:
            out.append((path, None, f"{type(exc).__name__}: {exc}"))
    return out


def _parse_phase_process_pool(
    changed_files: list[str],
    project_path: str,
    file_hashes: dict[str, str],
    existing_by_path: dict[str, dict],
    workspace_id: str,
    workers: int,
    job_log: IndexJobLog,
    reporter: ProgressReporter,
    *,
    include_axis_facts: bool,
) -> list[FileDiff]:
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    chunks = [
        [
            (path, existing_by_path.get(path, {}))
            for path in changed_files[start : start + _PROCESS_CHUNK_FILES]
        ]
        for start in range(0, len(changed_files), _PROCESS_CHUNK_FILES)
    ]
    results: list[FileDiff] = []
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_parse_pool_init,
        initargs=(project_path, workspace_id, include_axis_facts),
    ) as pool:
        for chunk_result in pool.map(_parse_chunk_in_process, chunks):
            for path, diff, error in chunk_result:
                job_id = job_log.start_file_job(path, file_hash=file_hashes.get(path, ""))
                if error:
                    job_log.mark_failed(job_id, RuntimeError(error))
                else:
                    job_log.mark_completed(job_id)
                if diff is not None:
                    results.append(diff)
                reporter.step("parse")
    return results


def _parse_one(
    file_path: str,
    extractor: FastExtractor,
    existing: dict[str, dict],
) -> FileDiff | None:
    """Runs inside a worker thread. Reads file, parses, computes diff vs preload."""
    extracted = extractor.extract_all(file_path)
    if extracted is None:
        return None

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
    """Parallel extraction + diff computation.

    Neo4j symbol indexes are preloaded on the main thread so workers stay
    CPU/I/O bound. With ``workers > 1`` and enough changed files the CPU work
    moves to a spawn-based process pool (the CPython AST/axis pass holds the
    GIL, so thread workers never scaled); the job log and reporter are always
    driven from the parent. Results are path-sorted so downstream graph
    writes see a deterministic file order either way.
    """
    extractor = FastExtractor(
        project_root=project_path,
        workspace_id=workspace_id,
        include_axis_facts=include_axis_facts,
    )
    get_many = getattr(db, "get_symbol_index_for_files", None)
    if callable(get_many):
        existing_by_path = get_many(changed_files, workspace_id=workspace_id)
    else:
        get_one = getattr(db, "get_symbol_index_for_file", None)
        existing_by_path = {
            path: (get_one(path, workspace_id=workspace_id) if callable(get_one) else {})
            for path in changed_files
        }

    # Processes only where threads are GIL-bound: the axis profile's CPython
    # AST pass. Plain tree-sitter profiles keep the thread pool (the C parser
    # releases the GIL, and their test doubles rely on in-process adapters).
    reporter.stage_start("parse", total=len(changed_files))
    if include_axis_facts and workers > 1 and len(changed_files) >= _PROCESS_POOL_MIN_FILES:
        results = _parse_phase_process_pool(
            changed_files,
            project_path,
            file_hashes,
            existing_by_path,
            workspace_id,
            workers,
            job_log,
            reporter,
            include_axis_facts=include_axis_facts,
        )
    else:
        results = []

        def _task(path: str) -> FileDiff | None:
            digest = file_hashes.get(path, "")
            with job_log.track_file_job(path, file_hash=digest):
                return _parse_one(path, extractor, existing_by_path.get(path, {}))

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_task, p): p for p in changed_files}
            for fut in as_completed(futures):
                diff = fut.result()
                if diff is not None:
                    results.append(diff)
                reporter.step("parse")
    reporter.stage_end("parse")

    results.sort(key=lambda diff: diff.extracted.path)
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
    """Materialize ``ExternalPkg`` nodes and ``*_EXTERNAL`` edges (C1).

    Row collection is per-file CPU; the graph work is batched into one
    ``link_external_boundary`` call. The per-file variant paid ~5 round-trips
    per file AND re-ran the two workspace-wide orphan sweeps inside
    ``link_external_boundary`` once per file — on django that was the whole
    phase cost.
    """
    link_boundary = getattr(db, "link_external_boundary", None)
    if not callable(link_boundary):
        return 0, 0
    boundary = build_project_boundary(project_path, file_paths=tuple(indexed_files))
    project_external_roots = package_manifest_external_roots(project_path)
    reporter.stage_start("external_boundary", total=len(diffs))

    delete_many = getattr(db, "delete_external_imports_for_files", None)
    delete_one = getattr(db, "delete_external_imports_for_file", None)
    if callable(delete_many):
        delete_many([diff.extracted.path for diff in diffs], workspace_id=workspace_id)
    elif callable(delete_one):
        for diff in diffs:
            delete_one(diff.extracted.path, workspace_id=workspace_id)

    all_call_rows: list[dict] = []
    all_import_rows: list[dict] = []
    all_symbol_rows: list[dict] = []
    for diff in diffs:
        ex = diff.extracted
        call_links = collect_external_call_links(
            ex.calls,
            boundary=boundary,
            project_external_roots=project_external_roots,
        )
        import_links = collect_external_import_links(
            ex.source,
            ex.path,
            boundary=boundary,
            project_external_roots=project_external_roots,
        )
        symbol_import_links = collect_external_symbol_import_links(
            ex.source,
            ex.path,
            boundary=boundary,
            project_external_roots=project_external_roots,
        )
        all_call_rows.extend(external_call_link_rows(call_links, workspace_id))
        all_import_rows.extend(external_import_link_rows(import_links, workspace_id))
        all_symbol_rows.extend(external_symbol_import_rows(symbol_import_links, workspace_id))
        reporter.step("external_boundary")

    calls_created, imports_created = link_boundary(
        all_call_rows,
        all_import_rows,
        workspace_id=workspace_id,
        symbol_import_links=all_symbol_rows,
    )
    reporter.stage_end("external_boundary")
    return int(calls_created or 0), int(imports_created or 0)


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


def _orphan_prune_phase(
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    """Remove file-less orphan Symbols and heal neighbor degrees."""
    prune_orphans = getattr(db, "prune_orphan_symbols", None)
    reporter.stage_start("orphan_prune", total=1)
    pruned = prune_orphans(workspace_id=workspace_id) if callable(prune_orphans) else 0
    reporter.step("orphan_prune")
    reporter.stage_end("orphan_prune")
    if pruned:
        print(f"Pruned {pruned} orphan symbol nodes")
    return int(pruned or 0)


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
    from context_engine.parser.uid import project_root_scope

    link_proxy = getattr(db, "link_proxy_bindings", None)
    reporter.stage_start("proxy_bindings", total=1)
    bindings: list[dict] = []
    if callable(link_proxy):
        with project_root_scope(project_path or None, workspace_id):
            bindings = _collect_adapter_facts_from_diffs(diffs, "extract_proxy_bindings")
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
    from context_engine.parser.uid import project_root_scope

    resolve = getattr(db, "resolve_proxy_return_calls", None)
    reporter.stage_start("proxy_return_calls", total=1)
    created = 0
    if callable(resolve):
        with project_root_scope(project_path or None, workspace_id):
            py_diffs = [d for d in diffs if d.extracted.path.endswith((".py", ".pyi"))]
            candidates = _collect_adapter_facts_from_diffs(
                py_diffs, "extract_self_method_proxy_calls"
            )
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
    from context_engine.parser.uid import project_root_scope

    link_hooks = getattr(db, "link_hooks", None)
    reporter.stage_start("hooks", total=1)
    hooks: list[dict] = []
    if callable(link_hooks):
        with project_root_scope(project_path or None, workspace_id):
            hooks = _collect_adapter_facts_from_diffs(diffs, "extract_hooks")
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
    from context_engine.parser.uid import project_root_scope

    link_bridges = getattr(db, "link_metadata_bridges", None)
    reporter.stage_start("metadata_bridges", total=1)
    facts: list[dict] = []
    if callable(link_bridges):
        with project_root_scope(project_path or None, workspace_id):
            facts = _collect_adapter_facts_from_diffs(diffs, "extract_metadata_bridges")
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
    from context_engine.parser.uid import project_root_scope

    link_endpoints = getattr(db, "link_http_endpoints", None)
    reporter.stage_start("http_endpoints", total=1)
    facts: list[dict] = []
    if callable(link_endpoints):
        with project_root_scope(project_path or None, workspace_id):
            facts = _collect_adapter_facts_from_diffs(diffs, "extract_http_endpoints")
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
    from context_engine.parser.uid import project_root_scope

    link_attr = getattr(db, "link_attr_accesses", None)
    reporter.stage_start("attr_accesses", total=1)
    accesses: list[dict] = []
    if callable(link_attr):
        with project_root_scope(project_path or None, workspace_id):
            accesses = _collect_adapter_facts_from_diffs(diffs, "extract_attr_accesses")
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
    from context_engine.parser.uid import project_root_scope

    link_types = getattr(db, "link_type_references", None)
    reporter.stage_start("type_refs", total=1)
    references: list[dict] = []
    if callable(link_types):
        with project_root_scope(project_path or None, workspace_id):
            references = _collect_adapter_facts_from_diffs(diffs, "extract_type_references")
        if references:
            link_types(references, workspace_id=workspace_id)
    reporter.step("type_refs")
    reporter.stage_end("type_refs")
    return len(references)


def _flow_pair_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
    project_path: str = "",
) -> int:
    """Create FLOWS_INTO edges (call A's result feeds call B's arguments).

    A co-invocation dataflow pair (``x = A(...); B(x)`` / ``B(A(...))``) is a
    syntactic fact, like a type reference. Runs after `_apply_graph` so both
    callee symbols exist, and under the same project_root_scope so uids match
    stored nodes. Pairs are recorded per caller site, so a reindexed file's
    stale pairs are cleared by its current + removed symbol uids first.
    """
    from context_engine.parser.uid import project_root_scope

    link_pairs = getattr(db, "link_flow_pairs", None)
    delete_pairs = getattr(db, "delete_flow_pairs_for_callers", None)
    reporter.stage_start("flow_pairs", total=1)
    pairs: list[dict] = []
    linked = 0
    if callable(link_pairs):
        stale_caller_uids: list[str] = []
        with project_root_scope(project_path or None, workspace_id):
            for diff in diffs:
                stale_caller_uids.extend(diff.current_uids)
                stale_caller_uids.extend(diff.removed_uids)
            pairs = _collect_adapter_facts_from_diffs(diffs, "extract_flow_pairs")
        if callable(delete_pairs) and stale_caller_uids:
            delete_pairs(stale_caller_uids, workspace_id=workspace_id)
        if pairs:
            linked = link_pairs(pairs, workspace_id=workspace_id)
    reporter.step("flow_pairs")
    reporter.stage_end("flow_pairs")
    return int(linked or 0)


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
    from context_engine.parser.uid import project_root_scope

    link_reexports = getattr(db, "link_reexports", None)
    reporter.stage_start("reexports", total=1)
    reexports: list[dict] = []
    if callable(link_reexports):
        with project_root_scope(project_path or None, workspace_id):
            reexports = _collect_adapter_facts_from_diffs(diffs, "extract_reexports")
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
    from context_engine.parser.uid import project_root_scope

    link_inst = getattr(db, "link_instantiations", None)
    reporter.stage_start("instantiations", total=1)
    instantiations: list[dict] = []
    if callable(link_inst):
        with project_root_scope(project_path or None, workspace_id):
            instantiations = _collect_adapter_facts_from_diffs(diffs, "extract_instantiations")
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
    from context_engine.parser.uid import project_root_scope

    link_inj = getattr(db, "link_injections", None)
    reporter.stage_start("injections", total=1)
    injections: list[dict] = []
    if callable(link_inj):
        with project_root_scope(project_path or None, workspace_id):
            injections = _collect_adapter_facts_from_diffs(diffs, "extract_injections")
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
