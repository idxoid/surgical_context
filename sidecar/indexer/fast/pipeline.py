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

    reporter.stage_start("graph", total=len(diffs))
    for diff in diffs:
        ex = diff.extracted
        db.upsert_file_structure(
            ex.path, ex.file_hash, diff.changed_symbols, workspace_id=workspace_id
        )

        if callable(prune_symbols):
            prune_symbols(ex.path, keep_uids=diff.current_uids, workspace_id=workspace_id)

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


def _framework_hints_phase(
    diffs: list[FileDiff],
    db: Neo4jClient,
    workspace_id: str,
    reporter: ProgressReporter,
) -> int:
    """Apply framework-specific rules to create SEMANTIC_HINT edges."""
    reporter.stage_start("framework_hints", total=len(diffs))
    indexer = FrameworkHintsIndexer(db)
    indexer.apply_rules(diffs, workspace_id)
    reporter.stage_end("framework_hints")
    return len(diffs)


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

        # Stage 4: graph writes
        t_stage = time.perf_counter()
        _apply_graph(diffs, db, workspace_id, reporter)
        stats["timings_sec"]["graph"] = round(time.perf_counter() - t_stage, 3)

        # Stage 4.5: framework hints
        t_stage = time.perf_counter()
        stats["framework_hints_applied"] = _framework_hints_phase(diffs, db, workspace_id, reporter)
        stats["timings_sec"]["framework_hints"] = round(time.perf_counter() - t_stage, 3)

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
