"""Fast incremental indexing — orchestrator.

The individual phases live in ``phases.py``; axis-payload compilation in
``axis_payloads.py``; shared reporter/diff types in ``pipeline_types.py``.
This module wires them into the two orchestrators (``run_fast_indexing`` and
``_run_fast_changed_files_pipeline``) and keeps the historical import surface.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Collection
from typing import TYPE_CHECKING

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
)

from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.neo4j_client import Neo4jClient
from context_engine.database.provider import get_database_provider
from context_engine.index_profile import (
    AXIS_PYTHON_V1_PROFILE,
    active_index_profile,
    effective_index_workspace_id,
    resolve_index_profile,
)
from context_engine.indexer.fast.collector import collect_files
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
from context_engine.workspace import WorkspaceResolver

if TYPE_CHECKING:
    pass
from context_engine.database.neo4j_env import (  # noqa: F401 — re-exported for QA scripts & fast/__main__
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
)
from context_engine.indexer.fast.axis_payloads import build_symbol_docs_for_extracted
from context_engine.indexer.fast.phases import (
    _adjacency_materialization_phase,
    _affects_phase,
    _apply_graph,
    _attr_access_phase,
    _clear_derived_edges_for_diffs,
    _decorator_phase,
    _degree_phase,
    _degree_seeds_snapshot,
    _embed_phase,
    _ensure_adjacency_materialized,
    _extends_external_phase,
    _external_boundary_phase,
    _flow_pair_phase,
    _hash_phase,
    _hook_phase,
    _http_endpoint_phase,
    _injection_phase,
    _instantiation_phase,
    _integrates_with_phase,
    _metadata_bridge_phase,
    _mro_api_bridge_phase,
    _orphan_prune_phase,
    _parse_phase,
    _property_api_phase,
    _proxy_binding_phase,
    _proxy_call_resolution_phase,
    _proxy_return_call_phase,
    _rebuild_affects_for_uids,
    _reexport_phase,
    _symbol_alias_phase,
    _tombstone_phase,
    _type_reference_phase,
)
from context_engine.indexer.fast.pipeline_types import FileDiff, ProgressReporter, _NullReporter
from context_engine.silence import install as _silence

_silence()

# Parallelism knobs. Default hash pool is high because hashing is I/O-bound;
# parse pool tracks CPU count because tree-sitter parsing is CPU-bound but
# releases the GIL inside the C extension. Axis Python additionally runs a
# GIL-heavy ast pass, so its workers are OS processes (see _parse_phase);
# AXIS_PARSE_PROCESSES=0 forces the old single-thread parse.
_DEFAULT_HASH_WORKERS = max(4, (os.cpu_count() or 4) * 2)
_DEFAULT_PARSE_WORKERS = max(2, os.cpu_count() or 4)


def _axis_parse_workers_default() -> int:
    if os.environ.get("AXIS_PARSE_PROCESSES", "").strip().lower() in {"0", "false", "off", "no"}:
        return 1
    return max(2, min(8, os.cpu_count() or 4))


__all__ = [
    "run_fast_indexing",
    "run_axis_incremental_finalize",
    "build_symbol_docs_for_extracted",
    "NEO4J_PASSWORD",
    "NEO4J_URI",
    "NEO4J_USER",
    "FileDiff",
    "_NullReporter",
    "_apply_graph",
    "_embed_phase",
    "_property_api_phase",
    "_symbol_alias_phase",
    "_type_reference_phase",
]


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


def _stamp_total(stats: dict, t0: float) -> None:
    stats.setdefault("timings_sec", {})["total"] = round(time.perf_counter() - t0, 3)


def _finish_no_indexable_files(
    stats: dict,
    db: Neo4jClient,
    *,
    workspace_id: str,
    project_path: str,
    t0: float,
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
    _stamp_total(stats, t0)
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
    t0: float,
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
    _stamp_total(stats, t0)
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
    t0: float,
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
    _stamp_total(stats, t0)
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
    stats["orphan_symbols_pruned"] = _orphan_prune_phase(db, workspace_id, reporter)
    stats["timings_sec"]["orphan_prune"] = round(time.perf_counter() - t_stage, 3)

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
    stats["flow_pairs_linked"] = _flow_pair_phase(diffs, db, workspace_id, reporter, project_path)
    stats["timings_sec"]["flow_pairs"] = round(time.perf_counter() - t_stage, 3)

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
    reporter = reporter or _NullReporter()

    profile = resolve_index_profile(index_profile) if index_profile else active_index_profile()
    if parse_workers is None:
        parse_workers = (
            _axis_parse_workers_default()
            if profile.name == AXIS_PYTHON_V1_PROFILE
            else _DEFAULT_PARSE_WORKERS
        )
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
                t0=t0,
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
                t0=t0,
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
                t0=t0,
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
