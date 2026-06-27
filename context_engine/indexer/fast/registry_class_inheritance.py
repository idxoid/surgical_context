"""Cross-file ``registry_class`` propagation via inheritance.

The per-file L2 ``registry_class`` predicate fires when a class's
*same-file* peer methods carry a registry-shape container kind. It
cannot see the inheritance chain — a class whose registration methods
live on an ancestor in a different file (e.g. ``flask.blueprints.Blueprint``
inheriting from ``flask.sansio.blueprints.Blueprint``) stays unclassified
even when the structural evidence is one hop away.

This workspace-level post-pass closes that gap. After axis classification
runs per file, it walks each class's structural ancestry — both the
``DEPENDS_ON`` edges the indexer already materializes from concrete
inheritance, *and* parsed-base-name aliases resolved by re-reading the
file's import table — and if any ancestor carries the ``registry_class``
container kind, propagates that kind onto the descendant by updating its
Lance row.

Two channels are deliberately combined:

  - ``DEPENDS_ON``: the parser-derived graph edge for inheritance whose
    base resolved to a local Symbol by name at link time.
  - ``parsed_base_names`` + per-file ``import_bindings``: cases where the
    base is imported under a local alias (``from .sansio.blueprints
    import Blueprint as SansioBlueprint``), which the link-by-name pass
    cannot resolve.

The pass does not author any new kinds — it just propagates an existing
``registry_class`` classification down the inheritance chain. The class
still has to have a *real* registry ancestor; ``registry_class`` itself
is still axis-only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from context_engine.database.neo4j_client import Neo4jClient
from context_engine.parser.adapters.python_adapter import PythonAdapter
from context_engine.parser.uid import current_project_root, project_root_scope

_PYTHON_ADAPTER = PythonAdapter()


def _workspace_package(db: Neo4jClient, workspace_id: str) -> str | None:
    """Top-level package = directory holding the shortest ``__init__.py``."""
    with db.driver.session() as session:
        rec = session.run(
            """
            MATCH (f:File {workspace_id: $ws})
            WHERE f.path ENDS WITH '/__init__.py'
            RETURN f.path AS path ORDER BY size(f.path) ASC LIMIT 1
            """,
            ws=workspace_id,
        ).single()
    if rec is None:
        return None
    return Path(rec["path"]).parent.name or None


def _strip_package_prefix(qualified_name: str, package: str | None) -> str:
    """Convert an upstream FQN into the workspace-local form.

    The python adapter's ``_extract_import_bindings`` resolves relative
    imports (``from .sansio.blueprints import Blueprint as SansioBlueprint``)
    against the file's package, producing the workspace-local QN directly
    (``sansio.blueprints.Blueprint``) — no prefix to strip. Absolute
    imports may carry the package as a prefix (``flask.sansio.blueprints.Blueprint``);
    those we trim. Either way the result is the QN form the workspace
    stores on Symbol nodes.
    """
    if not qualified_name:
        return qualified_name
    if package:
        prefix = f"{package}."
        if qualified_name.startswith(prefix):
            return qualified_name[len(prefix) :]
    return qualified_name


def _query_class_inheritance_context(
    db: Neo4jClient,
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Return one row per class: uid, qn, file_path, parsed_base_names,
    DEPENDS_ON ancestor uids, and ancestor uids reached through any chain
    of DEPENDS_ON edges up to 6 hops.
    """
    with db.driver.session() as session:
        return cast(
            list[dict[str, Any]],
            session.run(
                """
            MATCH (f:File {workspace_id: $ws})-[:CONTAINS]->(c:Symbol {kind: 'class'})
            OPTIONAL MATCH (c)-[:DEPENDS_ON*1..6 {workspace_id: $ws}]->(a:Symbol)
            WHERE a.kind = 'class'
            RETURN c.uid AS class_uid,
                   c.qualified_name AS class_qn,
                   f.path AS file_path,
                   c.parsed_base_names AS parsed_base_names,
                   collect(DISTINCT a.uid) AS ancestor_uids
            """,
                ws=workspace_id,
            ).data(),
        )


def _query_classes_by_local_qn(
    db: Neo4jClient,
    workspace_id: str,
    local_qns: set[str],
) -> dict[str, str]:
    """Resolve a set of in-workspace class qualified-names to their uids."""
    if not local_qns:
        return {}
    with db.driver.session() as session:
        rows = session.run(
            """
            MATCH (f:File {workspace_id: $ws})-[:CONTAINS]->(c:Symbol {kind: 'class'})
            WHERE c.qualified_name IN $qns
            RETURN c.qualified_name AS qn, c.uid AS uid
            """,
            ws=workspace_id,
            qns=list(local_qns),
        ).data()
    return {str(r["qn"]): str(r["uid"]) for r in rows}


def _read_lance_kinds(lance, workspace_id: str) -> dict[str, dict[str, Any]]:
    """Return ``{uid: {container_kinds: [...], axis_container_kinds_json: '...'}}``
    for every Symbol row in the workspace, so the propagator can read
    current kinds and emit an updated row.
    """
    scan = getattr(lance, "scan_symbols_workspace", None)
    if callable(scan):
        rows = scan(
            workspace_id,
            columns=["uid", "container_kinds", "axis_container_kinds_json"],
        )
    else:
        table = lance.symbols_table(workspace_id)  # type: ignore[attr-defined]
        rows = (
            table.to_lance()
            .to_table(columns=["uid", "container_kinds", "axis_container_kinds_json"])
            .to_pylist()
        )
    return {
        r["uid"]: {
            "container_kinds": list(r.get("container_kinds") or []),
            "axis_container_kinds_json": r.get("axis_container_kinds_json") or "[]",
        }
        for r in rows
        if r.get("uid")
    }


def _alias_ancestor_uids_for_class(
    file_path: str,
    parsed_base_names: list[str],
    depends_on_names: set[str],
    package: str | None,
    cached_bindings: dict[str, dict[str, str]],
    candidate_uids_by_local_qn: dict[str, str],
) -> list[str]:
    """Resolve any parsed_base_name that DEPENDS_ON didn't match (typically
    because the base is bound under a local import alias) to an in-workspace
    class uid. Returns the uids of those ancestor classes.
    """
    if not parsed_base_names or not package:
        return []
    bindings = _file_import_bindings(file_path, cached_bindings)
    out: list[str] = []
    for base in parsed_base_names:
        if base in depends_on_names:
            continue
        upstream_qn = bindings.get(base)
        if not upstream_qn:
            continue
        local_qn = _strip_package_prefix(upstream_qn, package)
        if local_qn == upstream_qn:
            # Couldn't strip — base lives in a foreign package, nothing
            # to propagate from at the in-workspace layer.
            continue
        uid = candidate_uids_by_local_qn.get(local_qn)
        if uid:
            out.append(uid)
    return out


# Python builtin exception hierarchy. Membership here is a *language-
# level* contract, not a domain-name heuristic: ``raise`` requires a
# ``BaseException`` subclass, so a class is an exception type iff it
# (transitively) inherits one of these. This is the same kind of
# structural anchor as "inherits an external framework marker" — the
# name set is fixed by the language, not by what a project happens to
# call things. Warning subclasses are intentionally excluded; they are
# not error-surface answers.
_EXCEPTION_BASES: frozenset[str] = frozenset(
    {
        "BaseException",
        "Exception",
        "ArithmeticError",
        "AssertionError",
        "AttributeError",
        "BufferError",
        "EOFError",
        "ImportError",
        "ModuleNotFoundError",
        "LookupError",
        "IndexError",
        "KeyError",
        "MemoryError",
        "NameError",
        "UnboundLocalError",
        "OSError",
        "IOError",
        "EnvironmentError",
        "BlockingIOError",
        "ChildProcessError",
        "ConnectionError",
        "BrokenPipeError",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "FileExistsError",
        "FileNotFoundError",
        "InterruptedError",
        "IsADirectoryError",
        "NotADirectoryError",
        "PermissionError",
        "ProcessLookupError",
        "TimeoutError",
        "OverflowError",
        "RecursionError",
        "ReferenceError",
        "RuntimeError",
        "NotImplementedError",
        "StopIteration",
        "StopAsyncIteration",
        "SyntaxError",
        "IndentationError",
        "TabError",
        "SystemError",
        "SystemExit",
        "TypeError",
        "ValueError",
        "UnicodeError",
        "UnicodeDecodeError",
        "UnicodeEncodeError",
        "UnicodeTranslateError",
        "ZeroDivisionError",
        "FloatingPointError",
        "KeyboardInterrupt",
        "GeneratorExit",
    }
)


def resolve_error_model_uids(
    parsed_bases_by_uid: dict[str, list[str]],
    ancestors_by_uid: dict[str, set[str]],
) -> set[str]:
    """Pure core of the error-model classification.

    A uid is an ``error_model`` when its own parsed bases name a builtin
    exception (``_EXCEPTION_BASES``) — a *direct anchor* — or when its
    ancestor set (transitive ``DEPENDS_ON`` ∪ alias-resolved) reaches an
    anchor. Iterated to a fixpoint so alias-only ancestor edges that add
    a single hop still converge.

    Kept IO-free so the language-level inheritance logic can be unit
    tested without a Neo4j / Lance harness.
    """
    anchor_uids: set[str] = {
        uid for uid, bases in parsed_bases_by_uid.items() if _EXCEPTION_BASES & set(bases)
    }
    error_model_uids: set[str] = set(anchor_uids)
    changed = True
    while changed:
        changed = False
        for uid, ancestors in ancestors_by_uid.items():
            if uid in error_model_uids:
                continue
            if ancestors & error_model_uids or ancestors & anchor_uids:
                error_model_uids.add(uid)
                changed = True
    return error_model_uids


def _class_short_names(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {r["class_uid"]: (r["class_qn"] or "").split(".")[-1] for r in rows}


def _depends_on_names_by_class(
    rows: list[dict[str, Any]],
    name_by_uid: dict[str, str],
) -> dict[str, set[str]]:
    depends_on_names: dict[str, set[str]] = {}
    for row in rows:
        names = {name_by_uid.get(anc, "") for anc in (row.get("ancestor_uids") or [])}
        depends_on_names[row["class_uid"]] = names
    return depends_on_names


def _file_import_bindings(
    file_path: str,
    cached_bindings: dict[str, dict[str, str]],
) -> dict[str, str]:
    bindings = cached_bindings.get(file_path)
    if bindings is not None:
        return bindings
    try:
        with open(file_path, encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        cached_bindings[file_path] = {}
        return {}
    bindings = _PYTHON_ADAPTER._extract_import_bindings(source, file_path)  # noqa: SLF001
    cached_bindings[file_path] = bindings
    return bindings


def _local_qns_for_unresolved_bases(
    base_names: list[str],
    depends_on_names: set[str],
    bindings: dict[str, str],
    package: str | None,
    needed_local_qns: set[str],
) -> list[str]:
    local_qns: list[str] = []
    for base in base_names:
        if base in depends_on_names:
            continue
        upstream_qn = bindings.get(base)
        if not upstream_qn:
            continue
        local_qn = _strip_package_prefix(upstream_qn, package)
        if not local_qn:
            continue
        local_qns.append(local_qn)
        needed_local_qns.add(local_qn)
    return local_qns


def _alias_local_qns_by_class(
    rows: list[dict[str, Any]],
    parsed_bases_by_uid: dict[str, list[str]],
    depends_on_names_by_class: dict[str, set[str]],
    package: str | None,
    project_path: str,
    workspace_id: str,
) -> tuple[dict[str, list[str]], set[str]]:
    needed_local_qns: set[str] = set()
    alias_targets: dict[str, list[str]] = {}
    cached_bindings: dict[str, dict[str, str]] = {}
    with project_root_scope(project_path, workspace_id):
        for row in rows:
            class_uid = row["class_uid"]
            base_names = parsed_bases_by_uid.get(class_uid, [])
            if not base_names:
                continue
            file_path = str(row.get("file_path") or "")
            bindings = _file_import_bindings(file_path, cached_bindings)
            depends_on_names = depends_on_names_by_class.get(class_uid, set())
            local_qns = _local_qns_for_unresolved_bases(
                base_names,
                depends_on_names,
                bindings,
                package,
                needed_local_qns,
            )
            if local_qns:
                alias_targets[class_uid] = local_qns
    return alias_targets, needed_local_qns


def _ancestors_by_uid(
    rows: list[dict[str, Any]],
    alias_targets: dict[str, list[str]],
    candidate_uids_by_local_qn: dict[str, str],
) -> dict[str, set[str]]:
    ancestors_by_uid: dict[str, set[str]] = {}
    for row in rows:
        uid = row["class_uid"]
        ancestors = set(row.get("ancestor_uids") or [])
        for local_qn in alias_targets.get(uid, []):
            anc_uid = candidate_uids_by_local_qn.get(local_qn)
            if anc_uid:
                ancestors.add(anc_uid)
        ancestors_by_uid[uid] = ancestors
    return ancestors_by_uid


@dataclass(frozen=True)
class _ClassInheritancePassContext:
    rows: list[dict[str, Any]]
    lance_kinds: dict[str, dict[str, Any]]
    parsed_bases_by_uid: dict[str, list[str]]
    name_by_uid: dict[str, str]
    alias_targets: dict[str, list[str]]
    candidate_uids_by_local_qn: dict[str, str]


def _prepare_class_inheritance_pass(
    db: Neo4jClient,
    lance,
    workspace_id: str,
    *,
    project_path: str | None = None,
) -> _ClassInheritancePassContext | None:
    package = _workspace_package(db, workspace_id)
    rows = _query_class_inheritance_context(db, workspace_id)
    if not rows:
        return None

    lance_kinds = _read_lance_kinds(lance, workspace_id)
    if not project_path:
        project_path = current_project_root()

    parsed_bases_by_uid = {r["class_uid"]: list(r.get("parsed_base_names") or []) for r in rows}
    name_by_uid = _class_short_names(rows)
    depends_on_names_by_class = _depends_on_names_by_class(rows, name_by_uid)
    alias_targets, needed_local_qns = _alias_local_qns_by_class(
        rows,
        parsed_bases_by_uid,
        depends_on_names_by_class,
        package,
        project_path,
        workspace_id,
    )
    candidate_uids_by_local_qn = _query_classes_by_local_qn(
        db,
        workspace_id,
        needed_local_qns,
    )
    return _ClassInheritancePassContext(
        rows=rows,
        lance_kinds=lance_kinds,
        parsed_bases_by_uid=parsed_bases_by_uid,
        name_by_uid=name_by_uid,
        alias_targets=alias_targets,
        candidate_uids_by_local_qn=candidate_uids_by_local_qn,
    )


def _build_error_model_lance_updates(
    error_model_uids: set[str],
    lance_kinds: dict[str, dict[str, Any]],
    name_by_uid: dict[str, str],
) -> dict[str, dict[str, Any]]:
    update_map: dict[str, dict[str, Any]] = {}
    for uid in error_model_uids:
        self_data = lance_kinds.get(uid)
        if not self_data or "error_model" in self_data["container_kinds"]:
            continue
        new_container_kinds = sorted(set(self_data["container_kinds"]) | {"error_model"})
        try:
            existing_matches = json.loads(self_data["axis_container_kinds_json"] or "[]")
        except json.JSONDecodeError:
            existing_matches = []
        existing_matches.append(
            {
                "kind": "error_model",
                "symbol_uid": uid,
                "qualified_name": name_by_uid.get(uid, ""),
                "evidence_bits": [["struct", "class_def"]],
                "evidence_probes": ["inherits_builtin_exception"],
                "payload": {},
            }
        )
        update_map[uid] = {
            "container_kinds": new_container_kinds,
            "axis_container_kinds_json": json.dumps(existing_matches, sort_keys=True),
        }
    return update_map


def _persist_lance_container_kind_updates(
    lance,
    workspace_id: str,
    update_map: dict[str, dict[str, Any]],
) -> int:
    if not update_map:
        return 0
    from context_engine.database.lance_workspace_tables import workspace_partitioned_enabled

    table = lance.symbols_table(workspace_id)  # type: ignore[attr-defined]
    existing_rows = [
        row
        for row in table.to_lance().to_table().to_pylist()
        if row.get("uid") in update_map
        and (workspace_partitioned_enabled() or row.get("workspace_id") == workspace_id)
    ]
    if not existing_rows:
        return 0
    for row in existing_rows:
        payload = update_map[row["uid"]]
        row["container_kinds"] = list(payload["container_kinds"])
        row["axis_container_kinds_json"] = payload["axis_container_kinds_json"]
    import pyarrow as pa

    arrow = pa.Table.from_pylist(existing_rows, schema=table.schema)
    uid_in = ", ".join("'" + uid.replace("'", "''") + "'" for uid in update_map)
    if workspace_partitioned_enabled():
        table.delete(f"uid IN ({uid_in})")
    else:
        quoted_ws = workspace_id.replace("'", "''")
        table.delete(f"workspace_id = '{quoted_ws}' AND uid IN ({uid_in})")
    table.add(arrow)
    return len(existing_rows)


def propagate_error_model_via_inheritance(
    db: Neo4jClient,
    lance,
    workspace_id: str,
    *,
    project_path: str | None = None,
) -> int:
    """Tag exception-type classes with the ``error_model`` container kind.

    A class is an ``error_model`` when its inheritance chain reaches a
    builtin exception base (``_EXCEPTION_BASES``). Two channels resolve
    the chain, mirroring the registry-class propagator:

      * direct ``parsed_base_names`` membership — ``ClickException(Exception)``
        anchors immediately;
      * transitive ``DEPENDS_ON`` ancestry plus alias-resolved bases —
        ``UsageError(ClickException)`` inherits the anchor through the
        in-workspace chain.

    ``error_model`` is the *exception-definition* side of error handling
    (the class that carries the error and formats it), distinct from
    ``error_dispatch`` (the code that catches and routes exceptions).
    Both back ``error_surface``. Returns the number of class rows
    updated.
    """
    ctx = _prepare_class_inheritance_pass(db, lance, workspace_id, project_path=project_path)
    if ctx is None:
        return 0

    ancestors_by_uid = _ancestors_by_uid(
        ctx.rows, ctx.alias_targets, ctx.candidate_uids_by_local_qn
    )
    error_model_uids = resolve_error_model_uids(ctx.parsed_bases_by_uid, ancestors_by_uid)
    update_map = _build_error_model_lance_updates(
        error_model_uids, ctx.lance_kinds, ctx.name_by_uid
    )
    return _persist_lance_container_kind_updates(lance, workspace_id, update_map)


def _full_ancestor_uids_for_class(
    row: dict[str, Any],
    alias_targets: dict[str, list[str]],
    candidate_uids_by_local_qn: dict[str, str],
) -> list[str]:
    uid = row["class_uid"]
    ancestor_uids = list(row.get("ancestor_uids") or [])
    for local_qn in alias_targets.get(uid, []):
        anc_uid = candidate_uids_by_local_qn.get(local_qn)
        if anc_uid and anc_uid not in ancestor_uids:
            ancestor_uids.append(anc_uid)
    return ancestor_uids


def _registry_class_update_payload(
    row: dict[str, Any],
    self_data: dict[str, Any],
    anc_uid: str,
) -> dict[str, Any]:
    uid = row["class_uid"]
    new_container_kinds = sorted(set(self_data["container_kinds"]) | {"registry_class"})
    try:
        existing_matches = json.loads(self_data["axis_container_kinds_json"] or "[]")
    except json.JSONDecodeError:
        existing_matches = []
    existing_matches.append(
        {
            "kind": "registry_class",
            "symbol_uid": uid,
            "qualified_name": row["class_qn"],
            "evidence_bits": [["struct", "class_def"]],
            "evidence_probes": [f"inherited_registry_class_via:{anc_uid}"],
            "payload": {"propagated_from_ancestor_uid": anc_uid},
        }
    )
    return {
        "container_kinds": new_container_kinds,
        "axis_container_kinds_json": json.dumps(existing_matches, sort_keys=True),
    }


def _build_registry_class_lance_updates(
    rows: list[dict[str, Any]],
    lance_kinds: dict[str, dict[str, Any]],
    alias_targets: dict[str, list[str]],
    candidate_uids_by_local_qn: dict[str, str],
) -> dict[str, dict[str, Any]]:
    update_map: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = row["class_uid"]
        self_data = lance_kinds.get(uid)
        if not self_data or "registry_class" in self_data["container_kinds"]:
            continue
        ancestor_uids = _full_ancestor_uids_for_class(
            row, alias_targets, candidate_uids_by_local_qn
        )
        for anc_uid in ancestor_uids:
            anc = lance_kinds.get(anc_uid)
            if not anc or "registry_class" not in anc["container_kinds"]:
                continue
            update_map[uid] = _registry_class_update_payload(row, self_data, anc_uid)
            break
    return update_map


def propagate_registry_class_via_inheritance(
    db: Neo4jClient,
    lance,
    workspace_id: str,
    *,
    project_path: str | None = None,
) -> int:
    """Run the propagation pass. Returns the number of class rows updated."""
    ctx = _prepare_class_inheritance_pass(db, lance, workspace_id, project_path=project_path)
    if ctx is None:
        return 0

    update_map = _build_registry_class_lance_updates(
        ctx.rows,
        ctx.lance_kinds,
        ctx.alias_targets,
        ctx.candidate_uids_by_local_qn,
    )
    return _persist_lance_container_kind_updates(lance, workspace_id, update_map)
