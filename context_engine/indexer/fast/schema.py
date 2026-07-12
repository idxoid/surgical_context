"""One-shot index/constraint bootstrap for the fast indexer.

Problem:
    Baseline Neo4j schema does not declare a uniqueness constraint on
    ``Symbol.uid`` nor indexes on ``Symbol.name``, ``Symbol.qualified_name``,
    or ``File.(path, workspace_id)``. The hot-path queries in
    ``context_engine.database.neo4j_client`` (``MERGE (s:Symbol {uid: ...})``,
    ``MATCH (callee:Symbol {name: ...})``, ``MATCH (:File {path, workspace_id})``)
    therefore degrade from constant-time to full label scans as the graph
    grows. On a large repo the indexer slows super-linearly past a few
    thousand symbols.

Fix:
    Declare the missing indexes before the pipeline starts. All statements
    use ``IF NOT EXISTS`` so running this on an already-migrated graph is
    a no-op. We deliberately do **not** modify the existing schema module
    — this parallel track stays additive.

Cost:
    Initial migration on an existing big graph can take seconds while the
    indexes populate. After that each ``MERGE`` on ``Symbol.uid`` is O(1).

What we intentionally skip:
    - Relationship property indexes on workspace_id — Neo4j planner rarely
      uses these for non-range lookups and adding them costs memory.
    - Indexes on File.hash — only read by ``get_file_hashes`` which
      already runs a single bulk query (one ``IN $paths`` lookup).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# Each entry: (label, statement). ``IF NOT EXISTS`` semantics are part of
# the statement so reruns are idempotent. Order matters only in that the
# uniqueness constraint must come before any code relies on O(1) MERGE —
# we create it first for clarity even though Neo4j would accept any order.
_SCHEMA = [
    # Uniqueness constraint on Symbol.uid. This is the critical one.
    # ``MERGE (s:Symbol {uid: $uid})`` against a label with no uniqueness
    # index must scan every Symbol node. With this constraint the lookup
    # is O(1) via the backing index that Neo4j creates automatically.
    (
        "symbol_uid_unique",
        """
        CREATE CONSTRAINT symbol_uid_unique IF NOT EXISTS
        FOR (s:Symbol) REQUIRE s.uid IS UNIQUE
        """,
    ),
    # Lookup by name during call resolution fallback
    # (MATCH (callee:Symbol {name: $callee_name})).
    (
        "symbol_name",
        """
        CREATE INDEX symbol_name IF NOT EXISTS
        FOR (s:Symbol) ON (s.name)
        """,
    ),
    # Lookup by qualified_name in _create_call_relations (second branch).
    (
        "symbol_qualified_name",
        """
        CREATE INDEX symbol_qualified_name IF NOT EXISTS
        FOR (s:Symbol) ON (s.qualified_name)
        """,
    ),
    # Workspace-scoped file lookups — the most frequent pattern in
    # upsert_file_structure, prune_symbols_for_file, delete_imports_for_file.
    (
        "file_path_workspace",
        """
        CREATE INDEX file_path_workspace IF NOT EXISTS
        FOR (f:File) ON (f.path, f.workspace_id)
        """,
    ),
    # Workspace node lookup by id — every write bumps graph_version.
    (
        "workspace_id",
        """
        CREATE CONSTRAINT workspace_id_unique IF NOT EXISTS
        FOR (w:Workspace) REQUIRE w.id IS UNIQUE
        """,
    ),
    # DocAnchor lookup by chunk_id + workspace_id — used by the anchor
    # pipeline during doc indexing.
    (
        "docanchor_chunk_workspace",
        """
        CREATE INDEX docanchor_chunk_workspace IF NOT EXISTS
        FOR (a:DocAnchor) ON (a.chunk_id, a.workspace_id)
        """,
    ),
    # External-boundary MERGE key. Every phase that routes an edge to an
    # upstream symbol (external_boundary, instantiations, extends_external)
    # runs ``MERGE (e:ExternalSymbol {uid, workspace_id})`` — without this
    # index each row is a full ExternalSymbol label scan, which turns those
    # phases O(rows × label) on dense repos.
    (
        "external_symbol_uid_workspace",
        """
        CREATE INDEX external_symbol_uid_workspace IF NOT EXISTS
        FOR (e:ExternalSymbol) ON (e.uid, e.workspace_id)
        """,
    ),
    # Text index on File.path specifically to accelerate the
    # ``target.path ENDS WITH $suffix`` predicate in
    # ``_create_import_relations``. Without this the query does a full
    # label scan per imported module — another O(files) growth term.
    # Text indexes in Neo4j 5 support ENDS WITH / STARTS WITH / CONTAINS
    # as index seeks.
    (
        "file_path_text",
        """
        CREATE TEXT INDEX file_path_text IF NOT EXISTS
        FOR (f:File) ON (f.path)
        """,
    ),
]


def ensure_fast_indexes(neo4j_client) -> list[str]:
    """Create missing indexes and constraints. Idempotent.

    Returns the names of schema objects that were freshly created (empty
    list if everything was already in place). Callers can log this once
    at startup.
    """
    created: list[str] = []
    with neo4j_client.driver.session() as session:
        for name, stmt in _SCHEMA:
            try:
                summary = session.run(stmt).consume()
            except Exception as e:  # pragma: no cover - defensive
                # Older Neo4j without ``IF NOT EXISTS`` would land here;
                # we surface the error so the run is not silently slow.
                log.warning("schema stmt failed (%s): %s", name, e)
                continue
            counters = summary.counters
            if counters.indexes_added or counters.constraints_added:
                created.append(name)
    return created
