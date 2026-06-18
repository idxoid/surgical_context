"""Index manifest: reproducibility snapshot written after indexing, readable by workspace id."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, cast

from sidecar.database.embedding_registry import get_model_metadata
from sidecar.index_profile import active_index_profile, resolve_index_profile
from sidecar.indexer.repository_profile import summarize_repository_profile
from sidecar.parser.registry import REGISTRY

INDEX_MANIFEST_SCHEMA_VERSION = 1

# Written under the indexed project root (same convention as extension workspace metadata).
MANIFEST_REL_PATH = Path(".surgical_context") / "index_manifest.json"

_EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")


def _index_profile_from_stats(stats: dict[str, Any]):
    profile_name = stats.get("index_profile") if isinstance(stats, dict) else None
    if isinstance(profile_name, str) and profile_name.strip():
        return resolve_index_profile(profile_name)
    return active_index_profile()


def manifest_file_path(project_path: str) -> Path:
    return Path(project_path).resolve() / MANIFEST_REL_PATH


def _git_snapshot(project_path: str) -> dict[str, str | None]:
    """Best-effort git branch + SHA for the indexed tree."""
    root = str(Path(project_path).resolve())
    out: dict[str, str | None] = {"commit": None, "branch": None}
    try:
        cp = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            out["commit"] = cp.stdout.strip()
        br = subprocess.run(
            ["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if br.returncode == 0 and br.stdout.strip():
            out["branch"] = br.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _graph_version(db: Any, workspace_id: str) -> int | None:
    gv = getattr(db, "get_workspace_graph_version", None)
    if callable(gv):
        try:
            return cast(int | None, gv(workspace_id=workspace_id))
        except TypeError:
            return cast(int | None, gv(workspace_id))
    return None


def _stats_fingerprint(stats: dict[str, Any]) -> str:
    """Stable hash of index-run counters so full reindexes get distinct ids when work differs."""
    payload = {
        "collected": stats.get("collected"),
        "changed": stats.get("changed"),
        "parsed": stats.get("parsed"),
        "symbols_encoded": stats.get("symbols_encoded"),
        "symbols_removed": stats.get("symbols_removed"),
        "affects_rebuilt": stats.get("affects_rebuilt"),
        "framework_hints_applied": stats.get("framework_hints_applied"),
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def compute_manifest_id(
    *,
    workspace_id: str,
    project_path: str,
    stats: dict[str, Any],
    graph_version: int | None,
    outcome: str,
    git: dict[str, str | None] | None = None,
) -> str:
    """Reproducible id: same workspace, graph generation, git head, embed model, outcome, and (for full_index) work fingerprint."""
    commit = (git or _git_snapshot(project_path)).get("commit") or ""
    gv = "" if graph_version is None else str(int(graph_version))
    index_profile = _index_profile_from_stats(stats)
    if outcome in ("noop_unchanged", "no_indexable_files"):
        work_fp = ""
    else:
        work_fp = _stats_fingerprint(stats)
    parts = "|".join(
        [
            workspace_id,
            index_profile.name,
            str(index_profile.schema_version),
            gv,
            commit,
            _EMBED_MODEL,
            outcome,
            work_fp,
        ],
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:32]


def build_index_manifest(
    *,
    workspace_id: str,
    project_path: str,
    stats: dict[str, Any],
    graph_version: int | None,
    outcome: str,
    indexing_pipeline: str = "fast_indexing",
) -> dict[str, Any]:
    """Assemble the canonical manifest dict (JSON-serializable)."""
    profile = (
        stats.get("repository_profile") if isinstance(stats.get("repository_profile"), dict) else {}
    )
    rp_store = stats.get("repository_profile_store") or ""
    emb = get_model_metadata(_EMBED_MODEL)
    index_profile = _index_profile_from_stats(stats)
    taxonomy = stats.get("role_taxonomy") if isinstance(stats.get("role_taxonomy"), dict) else {}
    role_catalog = stats.get("role_catalog") if isinstance(stats.get("role_catalog"), dict) else {}
    git = _git_snapshot(project_path)

    manifest_id = compute_manifest_id(
        workspace_id=workspace_id,
        project_path=project_path,
        stats=stats,
        graph_version=graph_version,
        outcome=outcome,
        git=git,
    )
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    manifest: dict[str, Any] = {
        "manifest_schema_version": INDEX_MANIFEST_SCHEMA_VERSION,
        "manifest_id": manifest_id,
        "created_at": created_at,
        "workspace_id": workspace_id,
        "project_path": os.path.abspath(project_path),
        "project_name": os.path.basename(os.path.abspath(project_path).rstrip(os.sep)),
        "indexing_outcome": outcome,
        "indexing_pipeline": indexing_pipeline,
        **index_profile.manifest_fields(),
        "git": git,
        "parser_languages": REGISTRY.supported_languages(),
        "embedding_model_id": _EMBED_MODEL,
        "embedding_model_name": emb.name if emb else _EMBED_MODEL,
        "embedding_model_version": emb.version if emb else "unknown",
        "embedding_dimensions": emb.dimensions if emb else None,
        "graph_store": "neo4j",
        "graph_version": graph_version,
        "graph_schema_note": "workspace_scoped_nodes_edges",
        "repository_profile_store": rp_store,
        "repository_profile_readiness": summarize_repository_profile(profile)
        if isinstance(profile, dict)
        else None,
        "indexed_files_collected": stats.get("collected"),
        "indexed_files_changed": stats.get("changed"),
        "parsed_files": stats.get("parsed"),
        "symbols_encoded": stats.get("symbols_encoded"),
        "symbols_removed": stats.get("symbols_removed"),
        "affects_rebuilt": stats.get("affects_rebuilt"),
        "framework_hints_applied": stats.get("framework_hints_applied"),
        "docs_files_indexed": stats.get("docs_files_indexed"),
        "docs_chunks_indexed": stats.get("docs_chunks_indexed"),
        "role_taxonomy": taxonomy or None,
        "role_catalog_counts": role_catalog or None,
        "timings_sec": stats.get("timings_sec")
        if isinstance(stats.get("timings_sec"), dict)
        else {},
    }
    return manifest


def write_manifest_to_disk(project_path: str, manifest: dict[str, Any]) -> Path:
    """Write ``.surgical_context/index_manifest.json`` atomically."""
    path = manifest_file_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(manifest, indent=2, sort_keys=True)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return path


def read_manifest_from_disk(project_path: str) -> dict[str, Any] | None:
    path = manifest_file_path(project_path)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def register_workspace_project_root(
    *,
    db: Any,
    workspace_id: str,
    project_path: str,
    file_count: int = 0,
) -> dict[str, Any] | None:
    """Persist ``project_path`` on the workspace before queued indexing finishes.

    Path sandboxing (/overlay, /index/file, /ask file fallback) reads
    ``project_path`` from the index manifest; queued ``POST /index`` must
    register the root immediately, not only after the batch worker completes.
    """
    stats: dict[str, Any] = {"collected": file_count, "changed": 0}
    return persist_index_manifest(
        stats=stats,
        db=db,
        workspace_id=workspace_id,
        project_path=project_path,
        outcome="queued",
        indexing_pipeline="queued_batch",
    )


def persist_index_manifest(
    *,
    stats: dict[str, Any],
    db: Any,
    workspace_id: str,
    project_path: str,
    outcome: str,
    indexing_pipeline: str = "fast_indexing",
) -> dict[str, Any] | None:
    """Build manifest, best-effort disk + Neo4j. Failures are recorded in ``stats``; does not raise."""
    warnings: list[dict[str, str]] = []
    gv: int | None
    try:
        gv = _graph_version(db, workspace_id)
    except Exception as exc:  # noqa: BLE001
        stats["index_manifest_error"] = repr(exc)
        gv = None
    try:
        manifest = build_index_manifest(
            workspace_id=workspace_id,
            project_path=project_path,
            stats=stats,
            graph_version=gv,
            outcome=outcome,
            indexing_pipeline=indexing_pipeline,
        )
    except Exception as exc:  # noqa: BLE001
        stats["index_manifest_error"] = repr(exc)
        return None

    disk_path = str(manifest_file_path(project_path))
    try:
        write_manifest_to_disk(project_path, manifest)
    except Exception as exc:  # noqa: BLE001
        warnings.append({"stage": "disk", "error": repr(exc)})
        disk_path = ""

    save = getattr(db, "save_index_manifest", None)
    if callable(save):
        try:
            save(manifest, workspace_id=workspace_id)
        except TypeError:
            try:
                save(manifest, workspace_id)
            except Exception as exc:  # noqa: BLE001
                warnings.append({"stage": "neo4j", "error": repr(exc)})
        except Exception as exc:  # noqa: BLE001
            warnings.append({"stage": "neo4j", "error": repr(exc)})

    if warnings:
        stats["index_manifest_persist_warnings"] = warnings
    stats["index_manifest"] = manifest
    stats["index_manifest_path"] = disk_path

    from sidecar.workspace_paths import prune_graph_paths_outside_root

    removed = prune_graph_paths_outside_root(
        db,
        workspace_id=workspace_id,
        project_root=Path(project_path).expanduser().resolve(),
    )
    if removed:
        stats["pruned_outside_root_paths"] = removed

    return manifest
