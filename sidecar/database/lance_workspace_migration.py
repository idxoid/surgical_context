"""Migrate monolithic Lance tables into per-workspace partitions.

Usage::

    python -m sidecar.database.lance_workspace_migration status
    python -m sidecar.database.lance_workspace_migration migrate [--purge-legacy]
"""

from __future__ import annotations

import argparse
import os
import sys

import lancedb

from sidecar.database.lance_workspace_tables import workspace_partitioned_enabled
from sidecar.database.lancedb_client import AXIS_ADJACENCY_TABLE, DB_PATH, LanceDBClient
from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE, resolve_index_profile


def _workspace_ids_in_table(table, *, column: str = "workspace_id") -> list[str]:
    try:
        rows = table.search().limit(0).select([column]).to_list()
    except Exception:
        rows = table.to_pandas()[[column]].drop_duplicates().to_dict(orient="records")
    return sorted({str(r.get(column) or "") for r in rows if r.get(column)})


def _legacy_table_row_count(table) -> int:
    try:
        return int(table.count_rows())
    except Exception:
        return len(_workspace_ids_in_table(table))


def _legacy_workspace_ids(client: LanceDBClient) -> list[str]:
    """Union of workspace ids still present in either monolithic table."""
    ids: set[str] = set()
    for table in (client._sym_table, client._axis_adjacency_table):  # noqa: SLF001
        if _legacy_table_row_count(table) <= 0:
            continue
        ids.update(_workspace_ids_in_table(table))
    return sorted(ids)


def _partition_stats(db, base_table: str) -> tuple[int, int]:
    prefix = f"{base_table}__ws_"
    tables = 0
    rows = 0
    if not os.path.isdir(DB_PATH):
        return tables, rows
    for name in os.listdir(DB_PATH):
        if not name.startswith(prefix) or not name.endswith(".lance"):
            continue
        tables += 1
        rows += int(db.open_table(name.removesuffix(".lance")).count_rows())
    return tables, rows


def _purge_legacy_workspace_rows(table, workspace_ids: list[str]) -> int:
    purged = 0
    for workspace_id in workspace_ids:
        ws = workspace_id.replace("'", "''")
        predicate = f"workspace_id = '{ws}'"
        try:
            before = int(table.count_rows(predicate))
        except Exception:
            before = 0
        if before <= 0:
            continue
        try:
            table.delete(predicate)
            purged += before
        except Exception:
            pass
    return purged


def status_command() -> None:
    if not workspace_partitioned_enabled():
        print("LANCEDB_WORKSPACE_PARTITIONED is off — partitions are disabled.")
    db = lancedb.connect(DB_PATH)
    profile = resolve_index_profile(AXIS_PYTHON_V1_PROFILE)
    print(f"LanceDB path: {DB_PATH}")
    print(f"workspace partitions enabled: {workspace_partitioned_enabled()}")

    for label, base in (
        ("symbols", profile.symbols_table),
        ("adjacency", AXIS_ADJACENCY_TABLE),
    ):
        try:
            legacy = db.open_table(base)
            legacy_rows = _legacy_table_row_count(legacy)
            legacy_ws = len(_workspace_ids_in_table(legacy)) if legacy_rows else 0
        except Exception:
            legacy_rows = 0
            legacy_ws = 0
        part_tables, part_rows = _partition_stats(db, base)
        print(
            f"{label:10} legacy_rows={legacy_rows:6} legacy_workspaces={legacy_ws:3} "
            f"partition_tables={part_tables:3} partition_rows={part_rows:6}"
        )


def migrate_command(*, purge_legacy: bool) -> None:
    if not workspace_partitioned_enabled():
        print("Enable LANCEDB_WORKSPACE_PARTITIONED=true before migrating.")
        sys.exit(1)
    client = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)
    legacy_sym = client._sym_table  # noqa: SLF001
    legacy_adj = client._axis_adjacency_table  # noqa: SLF001
    workspace_ids = _legacy_workspace_ids(client)
    sym_migrated = 0
    adj_migrated = 0

    for workspace_id in workspace_ids:
        sym_target = client.symbols_table(workspace_id)
        try:
            sym_before = int(sym_target.count_rows())
        except Exception:
            sym_before = 0
        if sym_before == 0:
            client._maybe_migrate_workspace_partition(workspace_id, sym_target, legacy_sym)  # noqa: SLF001
            try:
                if int(sym_target.count_rows()) > 0:
                    sym_migrated += 1
                    print(f"migrated symbols: {workspace_id}")
            except Exception:
                pass

        adj_target = client.axis_adjacency_table(workspace_id)
        try:
            adj_before = int(adj_target.count_rows())
        except Exception:
            adj_before = 0
        if adj_before == 0:
            client._maybe_migrate_workspace_partition(workspace_id, adj_target, legacy_adj)  # noqa: SLF001
            try:
                if int(adj_target.count_rows()) > 0:
                    adj_migrated += 1
                    print(f"migrated adjacency: {workspace_id}")
            except Exception:
                pass

    if purge_legacy and workspace_ids:
        sym_purged = _purge_legacy_workspace_rows(legacy_sym, workspace_ids)
        adj_purged = _purge_legacy_workspace_rows(legacy_adj, workspace_ids)
        print(
            f"purged legacy rows for {len(workspace_ids)} workspaces "
            f"(symbols={sym_purged}, adjacency={adj_purged})"
        )

    print(
        f"migration complete "
        f"(symbols={sym_migrated} adjacency={adj_migrated} workspaces={len(workspace_ids)})"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Lance per-workspace partition migration")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Report partition coverage")
    migrate = sub.add_parser("migrate", help="Copy legacy rows into workspace tables")
    migrate.add_argument(
        "--purge-legacy",
        action="store_true",
        help="Delete migrated rows from monolithic tables",
    )
    args = parser.parse_args(argv)
    if args.command == "status":
        status_command()
    elif args.command == "migrate":
        migrate_command(purge_legacy=args.purge_legacy)


if __name__ == "__main__":
    main()
