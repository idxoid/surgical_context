"""Migration CLI for LanceDB embedding versioning.

Usage:
    python -m sidecar.database.embedding_migration status
    python -m sidecar.database.embedding_migration migrate [--from MODEL] [--to MODEL]
"""

import argparse
import json
import sys

import lancedb

from sidecar.database.embedding_registry import KNOWN_MODELS, get_model_metadata
from sidecar.database.lancedb_client import DB_PATH, DOCS_TABLE, SYMBOLS_TABLE


def get_table_metadata(db, table_name: str) -> dict:
    """Inspect a table and report embedding metadata statistics."""
    try:
        table = db.open_table(table_name)
    except Exception as e:
        return {"error": str(e)}

    df = table.to_pandas()
    if df.empty:
        return {"total_rows": 0, "models": {}}

    metadata_counts: dict[str, int] = {}

    for _, row in df.iterrows():
        meta_str = row.get("embedding_metadata", "{}")
        try:
            meta = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
        except json.JSONDecodeError:
            continue

        model_name = meta.get("model_name", "unknown")
        model_version = meta.get("model_version", "unknown")
        key = f"{model_name} (v{model_version})"
        metadata_counts[key] = metadata_counts.get(key, 0) + 1

    return {
        "total_rows": len(df),
        "models": metadata_counts,
        "unversioned_rows": sum(1 for _, row in df.iterrows() if not row.get("embedding_metadata")),
    }


def status_command():
    """Report current embedding versions in LanceDB."""
    db = lancedb.connect(DB_PATH)
    available_tables = db.table_names()

    print(f"LanceDB path: {DB_PATH}")
    print(f"Available tables: {available_tables}\n")

    for table_name in [DOCS_TABLE, SYMBOLS_TABLE]:
        if table_name not in available_tables:
            print(f"{table_name}: NOT FOUND")
            continue

        info = get_table_metadata(db, table_name)
        if "error" in info:
            print(f"{table_name}: ERROR - {info['error']}")
            continue

        print(f"{table_name}:")
        print(f"  Total rows: {info['total_rows']}")
        if info["models"]:
            print("  Models:")
            for model_key, count in info["models"].items():
                print(f"    {model_key}: {count} rows")
        else:
            print("  Models: (none)")
        if info["unversioned_rows"] > 0:
            print(f"  ⚠️  Unversioned rows: {info['unversioned_rows']}")
        print()


def migrate_command(args):
    """Migrate table from one embedding model to another."""
    from_model = args.from_model or "all-MiniLM-L6-v2"
    to_model = args.to_model or "all-MiniLM-L6-v2"

    if from_model == to_model:
        print(f"Source and target models are the same ({from_model}). No migration needed.")
        return

    from_meta = get_model_metadata(from_model)
    to_meta = get_model_metadata(to_model)

    if not from_meta or not to_meta:
        print(f"Unknown model. Available models: {list(KNOWN_MODELS.keys())}")
        return

    print(f"Migrating from {from_model} to {to_model}...")
    print(f"  Source dimensions: {from_meta.dimensions}")
    print(f"  Target dimensions: {to_meta.dimensions}")

    # For now, this is a placeholder. Full migration would:
    # 1. Load all chunks/symbols
    # 2. Re-embed with new model
    # 3. Update metadata
    # 4. Re-write to table with new schema
    #
    # This is deferred pending confirmation that a model switch is needed.
    print("\n⚠️  Full migration not yet implemented.")
    print("To use a different embedding model:")
    print(f"  1. Set EMBED_MODEL={to_model}")
    print("  2. Delete or rename ./data/lancedb to start fresh")
    print("  3. Re-index your project")


def main():
    parser = argparse.ArgumentParser(description="Manage LanceDB embedding versions")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser("status", help="Show current embedding metadata")

    migrate_parser = subparsers.add_parser("migrate", help="Migrate to a different embedding model")
    migrate_parser.add_argument(
        "--from",
        dest="from_model",
        help="Source embedding model (auto-detected if not specified)",
    )
    migrate_parser.add_argument(
        "--to",
        dest="to_model",
        help="Target embedding model (defaults to all-MiniLM-L6-v2)",
    )

    args = parser.parse_args()

    if args.command == "status":
        status_command()
    elif args.command == "migrate":
        migrate_command(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
