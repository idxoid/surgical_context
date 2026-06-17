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
from pathlib import Path
from typing import Any

from sidecar.database.neo4j_client import Neo4jClient
from sidecar.parser.adapters.python_adapter import PythonAdapter
from sidecar.parser.uid import current_project_root, project_root_scope

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
        return session.run(
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
        ).data()


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
    bindings = cached_bindings.get(file_path)
    if bindings is None:
        try:
            with open(file_path, encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            cached_bindings[file_path] = {}
            return []
        bindings = _PYTHON_ADAPTER._extract_import_bindings(source, file_path)  # noqa: SLF001
        cached_bindings[file_path] = bindings
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
    package = _workspace_package(db, workspace_id)
    rows = _query_class_inheritance_context(db, workspace_id)
    if not rows:
        return 0
    lance_kinds = _read_lance_kinds(lance, workspace_id)

    if not project_path:
        project_path = current_project_root()

    parsed_bases_by_uid: dict[str, list[str]] = {
        r["class_uid"]: list(r.get("parsed_base_names") or []) for r in rows
    }

    # Alias-resolved ancestor uids per class — same resolution the
    # registry propagator uses, so a base imported under a local alias
    # still links to its in-workspace class uid.
    depends_on_names_by_class: dict[str, set[str]] = {}
    name_by_uid: dict[str, str] = {
        r["class_uid"]: (r["class_qn"] or "").split(".")[-1] for r in rows
    }
    for r in rows:
        names = {name_by_uid.get(a, "") for a in (r.get("ancestor_uids") or [])}
        depends_on_names_by_class[r["class_uid"]] = names

    needed_local_qns: set[str] = set()
    alias_targets: dict[str, list[str]] = {}
    cached_bindings: dict[str, dict[str, str]] = {}
    with project_root_scope(project_path, workspace_id):
        for r in rows:
            base_names = parsed_bases_by_uid.get(r["class_uid"], [])
            if not base_names:
                continue
            file_path = str(r.get("file_path") or "")
            bindings = cached_bindings.get(file_path)
            if bindings is None:
                try:
                    with open(file_path, encoding="utf-8") as fh:
                        source = fh.read()
                except OSError:
                    cached_bindings[file_path] = {}
                    continue
                bindings = _PYTHON_ADAPTER._extract_import_bindings(  # noqa: SLF001
                    source, file_path
                )
                cached_bindings[file_path] = bindings
            depends_on_names = depends_on_names_by_class.get(r["class_uid"], set())
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
            if local_qns:
                alias_targets[r["class_uid"]] = local_qns

    candidate_uids_by_local_qn = _query_classes_by_local_qn(
        db,
        workspace_id,
        needed_local_qns,
    )

    # Full ancestor set per class = DEPENDS_ON transitive ∪ alias-resolved.
    ancestors_by_uid: dict[str, set[str]] = {}
    for r in rows:
        uid = r["class_uid"]
        ancestors = set(r.get("ancestor_uids") or [])
        for local_qn in alias_targets.get(uid, []):
            anc_uid = candidate_uids_by_local_qn.get(local_qn)
            if anc_uid:
                ancestors.add(anc_uid)
        ancestors_by_uid[uid] = ancestors

    error_model_uids = resolve_error_model_uids(parsed_bases_by_uid, ancestors_by_uid)

    # Build Lance row updates for classes that don't already carry it.
    update_map: dict[str, dict[str, Any]] = {}
    for uid in error_model_uids:
        self_data = lance_kinds.get(uid)
        if not self_data:
            continue
        if "error_model" in self_data["container_kinds"]:
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

    if not update_map:
        return 0
    from sidecar.database.lance_workspace_tables import workspace_partitioned_enabled

    table = lance.symbols_table(workspace_id)  # type: ignore[attr-defined]
    existing_rows = [
        r
        for r in table.to_lance().to_table().to_pylist()
        if r.get("uid") in update_map
        and (workspace_partitioned_enabled() or r.get("workspace_id") == workspace_id)
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


def propagate_registry_class_via_inheritance(
    db: Neo4jClient,
    lance,
    workspace_id: str,
    *,
    project_path: str | None = None,
) -> int:
    """Run the propagation pass. Returns the number of class rows updated."""
    package = _workspace_package(db, workspace_id)
    rows = _query_class_inheritance_context(db, workspace_id)
    if not rows:
        return 0

    lance_kinds = _read_lance_kinds(lance, workspace_id)

    # Pass 1: resolve alias-based ancestor candidates. Collect every
    # upstream-local QN we will need to look up so the resolution Cypher
    # runs in one batch.
    depends_on_names_by_class: dict[str, set[str]] = {}
    for r in rows:
        depends_on_names_by_class[r["class_uid"]] = set()
    needed_local_qns: set[str] = set()
    cached_bindings: dict[str, dict[str, str]] = {}

    # Collect names that ARE in DEPENDS_ON via class.qn lookup. The query
    # above gives us ancestor uids only, so we rebuild a quick uid→name map.
    name_by_uid: dict[str, str] = {}
    for r in rows:
        name_by_uid[r["class_uid"]] = (r["class_qn"] or "").split(".")[-1]
    for r in rows:
        for anc_uid in r.get("ancestor_uids") or []:
            depends_on_names_by_class.setdefault(r["class_uid"], set())
            depends_on_names_by_class[r["class_uid"]].add(name_by_uid.get(anc_uid, ""))

    if not project_path:
        project_path = current_project_root()

    # First pass: enumerate candidate local QNs (so we batch-resolve to uids).
    alias_targets: dict[str, list[str]] = {}
    with project_root_scope(project_path, workspace_id):
        for r in rows:
            base_names = list(r.get("parsed_base_names") or [])
            if not base_names:
                continue
            file_path = str(r.get("file_path") or "")
            bindings = cached_bindings.get(file_path)
            if bindings is None:
                try:
                    with open(file_path, encoding="utf-8") as fh:
                        source = fh.read()
                except OSError:
                    cached_bindings[file_path] = {}
                    continue
                bindings = _PYTHON_ADAPTER._extract_import_bindings(source, file_path)  # noqa: SLF001
                cached_bindings[file_path] = bindings
            local_qns: list[str] = []
            depends_on_names = depends_on_names_by_class.get(r["class_uid"], set())
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
            if local_qns:
                alias_targets[r["class_uid"]] = local_qns

    candidate_uids_by_local_qn = _query_classes_by_local_qn(
        db,
        workspace_id,
        needed_local_qns,
    )

    # Second pass: compute which classes need registry_class added.
    classes_to_update: list[tuple[str, dict[str, Any]]] = []
    for r in rows:
        uid = r["class_uid"]
        self_data = lance_kinds.get(uid)
        if not self_data:
            continue
        if "registry_class" in self_data["container_kinds"]:
            continue
        ancestor_uids = list(r.get("ancestor_uids") or [])
        # Add alias-resolved ancestors.
        for local_qn in alias_targets.get(uid, []):
            anc_uid = candidate_uids_by_local_qn.get(local_qn)
            if anc_uid and anc_uid not in ancestor_uids:
                ancestor_uids.append(anc_uid)
        if not ancestor_uids:
            continue
        # Check any ancestor for registry_class.
        for anc_uid in ancestor_uids:
            anc = lance_kinds.get(anc_uid)
            if not anc:
                continue
            if "registry_class" not in anc["container_kinds"]:
                continue
            new_container_kinds = sorted(set(self_data["container_kinds"]) | {"registry_class"})
            existing_json = self_data["axis_container_kinds_json"]
            try:
                existing_matches = json.loads(existing_json or "[]")
            except json.JSONDecodeError:
                existing_matches = []
            existing_matches.append(
                {
                    "kind": "registry_class",
                    "symbol_uid": uid,
                    "qualified_name": r["class_qn"],
                    "evidence_bits": [["struct", "class_def"]],
                    "evidence_probes": [
                        f"inherited_registry_class_via:{anc_uid}",
                    ],
                    "payload": {"propagated_from_ancestor_uid": anc_uid},
                }
            )
            new_json = json.dumps(existing_matches, sort_keys=True)
            classes_to_update.append(
                (
                    uid,
                    {
                        "container_kinds": new_container_kinds,
                        "axis_container_kinds_json": new_json,
                    },
                )
            )
            break

    # Persist via merge_insert on (uid, workspace_id). Lance's SQL UPDATE
    # in 0.10 can't take JSON-bearing string literals (curly braces /
    # quotes choke its parser) and won't accept single-quoted string lists,
    # so we build a PyArrow table of the rows-to-replace and upsert them.
    if not classes_to_update:
        return 0
    from sidecar.database.lance_workspace_tables import workspace_partitioned_enabled

    table = lance.symbols_table(workspace_id)  # type: ignore[attr-defined]
    update_map = {uid: payload for uid, payload in classes_to_update}
    existing_rows = [
        r
        for r in table.to_lance().to_table().to_pylist()
        if r.get("uid") in update_map
        and (workspace_partitioned_enabled() or r.get("workspace_id") == workspace_id)
    ]
    if not existing_rows:
        return 0
    for row in existing_rows:
        new_payload = update_map[row["uid"]]
        row["container_kinds"] = list(new_payload["container_kinds"])
        row["axis_container_kinds_json"] = new_payload["axis_container_kinds_json"]
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
