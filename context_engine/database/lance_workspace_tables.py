"""Per-workspace Lance table naming for partitioned vector storage.

Each workspace gets its own physical Lance table (``{base}__ws_{digest}``)
so reads avoid scanning a monolithic table with a ``workspace_id`` filter.
The digest is a stable SHA-256 prefix of the workspace id.
"""

from __future__ import annotations

import hashlib
import os

WORKSPACE_TABLE_DIGEST_LEN = 20
PARTITION_MARKER = "__ws_"


def workspace_partitioned_enabled() -> bool:
    raw = os.getenv("LANCEDB_WORKSPACE_PARTITIONED", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def workspace_partition_table_name(base_table: str, workspace_id: str) -> str:
    """Return the Lance table name that holds one workspace's rows."""
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:WORKSPACE_TABLE_DIGEST_LEN]
    return f"{base_table}{PARTITION_MARKER}{digest}"


def is_workspace_partition_table(table_name: str, base_table: str) -> bool:
    return table_name.startswith(f"{base_table}{PARTITION_MARKER}")


def list_workspace_partition_tables(table_names: list[str], base_table: str) -> list[str]:
    prefix = f"{base_table}{PARTITION_MARKER}"
    return sorted(name for name in table_names if name.startswith(prefix))


def workspace_partition_table_exists(db, base_table: str, workspace_id: str) -> bool:
    """Return whether the workspace partition is openable in this LanceDB."""
    name = workspace_partition_table_name(base_table, workspace_id)
    if name in db.table_names():
        return True
    try:
        db.open_table(name)
        return True
    except Exception:
        return False


def drop_workspace_partition_table(db, base_table: str, workspace_id: str) -> bool:
    """Drop a workspace partition table when it exists on disk.

    LanceDB can leave orphaned ``.lance`` datasets that ``open_table`` can
    still read but ``table_names()`` omits. Wipe paths must drop by existence,
    not catalog membership.
    """
    if not workspace_partition_table_exists(db, base_table, workspace_id):
        return False
    name = workspace_partition_table_name(base_table, workspace_id)
    db.drop_table(name)
    return True


__all__ = [
    "PARTITION_MARKER",
    "WORKSPACE_TABLE_DIGEST_LEN",
    "is_workspace_partition_table",
    "drop_workspace_partition_table",
    "list_workspace_partition_tables",
    "workspace_partition_table_name",
    "workspace_partition_table_exists",
    "workspace_partitioned_enabled",
]
