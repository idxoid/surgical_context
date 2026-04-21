"""Neo4j schema migration for typed semantic edges (Phase 5, Gap 2).

Usage:
    python -m sidecar.database.schema_migration status
    python -m sidecar.database.schema_migration migrate-edges [--drop-old]
"""

import argparse
import sys

from neo4j import GraphDatabase


class SchemaMigrator:
    """Migrate Neo4j schema for typed semantic edges."""

    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def status(self):
        """Report current schema state."""
        with self.driver.session() as session:
            indexes = session.run(
                "SHOW INDEXES WHERE name CONTAINS 'rel_' OR name CONTAINS 'CALLS'"
            )
            print("Current indexes:")
            for idx in indexes:
                print(f"  {idx['name']}: {idx['type']}")

            calls_count = session.run("MATCH ()-[r:CALLS]->() RETURN count(r) AS count").single()
            if calls_count:
                print("\nEdge counts:")
                print(f"  CALLS edges: {calls_count['count']}")

            typed_counts = session.run(
                """
                RETURN
                  (size([()-[r:CALLS_DIRECT]->()|r])) AS direct,
                  (size([()-[r:CALLS_DYNAMIC]->()|r])) AS dynamic,
                  (size([()-[r:CALLS_INFERRED]->()|r])) AS inferred
                """
            )
            tc = typed_counts.single()
            print(f"  CALLS_DIRECT edges: {tc['direct']}")
            print(f"  CALLS_DYNAMIC edges: {tc['dynamic']}")
            print(f"  CALLS_INFERRED edges: {tc['inferred']}")

    def migrate_edges(self, drop_old=False):
        """
        Migrate CALLS edges to typed semantic edges.

        If drop_old=True, deletes old CALLS edges after conversion.
        Conservative default: migrate to CALLS_DIRECT (assumes all existing calls are direct).
        """
        with self.driver.session() as session:
            # Create relationship indexes for new edge types
            print("Creating relationship indexes...")
            indexes_to_create = [
                "rel_calls_direct",
                "rel_calls_dynamic",
                "rel_calls_inferred",
                "rel_implements",
                "rel_overrides",
                "rel_references",
            ]

            for idx_name in indexes_to_create:
                try:
                    if idx_name == "rel_calls_direct":
                        session.run(
                            "CREATE INDEX rel_calls_direct FOR ()-[r:CALLS_DIRECT]-() ON (r.uid)"
                        )
                    elif idx_name == "rel_calls_dynamic":
                        session.run(
                            "CREATE INDEX rel_calls_dynamic FOR ()-[r:CALLS_DYNAMIC]-() ON (r.uid)"
                        )
                    elif idx_name == "rel_calls_inferred":
                        session.run(
                            "CREATE INDEX rel_calls_inferred FOR ()-[r:CALLS_INFERRED]-() ON (r.uid)"
                        )
                    elif idx_name == "rel_implements":
                        session.run(
                            "CREATE INDEX rel_implements FOR ()-[r:IMPLEMENTS]-() ON (r.uid)"
                        )
                    elif idx_name == "rel_overrides":
                        session.run("CREATE INDEX rel_overrides FOR ()-[r:OVERRIDES]-() ON (r.uid)")
                    elif idx_name == "rel_references":
                        session.run(
                            "CREATE INDEX rel_references FOR ()-[r:REFERENCES]-() ON (r.uid)"
                        )
                    print(f"  ✓ Created {idx_name}")
                except Exception as e:
                    if "already exists" in str(e):
                        print(f"  ℹ {idx_name} already exists")
                    else:
                        print(f"  ✗ Error creating {idx_name}: {e}")

            # Migrate CALLS edges to CALLS_DIRECT (conservative default)
            print("\nMigrating CALLS → CALLS_DIRECT (conservative default)...")
            result = session.run(
                """
                MATCH (caller)-[r:CALLS]->(callee)
                WITH caller, callee, r
                DELETE r
                CREATE (caller)-[:CALLS_DIRECT]->(callee)
                RETURN count(*) AS migrated
                """
            )
            migrated = result.single()["migrated"]
            print(f"  ✓ Migrated {migrated} edges")

            if drop_old:
                print("\nVerifying no CALLS edges remain...")
                remaining = session.run("MATCH ()-[r:CALLS]->() RETURN count(r) AS count").single()
                if remaining["count"] == 0:
                    print("  ✓ All CALLS edges removed")
                else:
                    print(f"  ⚠ {remaining['count']} CALLS edges still exist")

    def create_indexes(self):
        """Create relationship indexes only (without edge migration)."""
        with self.driver.session() as session:
            print("Creating relationship indexes...")
            indexes = [
                ("rel_calls_direct", "CALLS_DIRECT"),
                ("rel_calls_dynamic", "CALLS_DYNAMIC"),
                ("rel_calls_inferred", "CALLS_INFERRED"),
                ("rel_implements", "IMPLEMENTS"),
                ("rel_overrides", "OVERRIDES"),
                ("rel_references", "REFERENCES"),
            ]

            for idx_name, rel_type in indexes:
                try:
                    session.run(f"CREATE INDEX {idx_name} FOR ()-[r:{rel_type}]-() ON (r.uid)")
                    print(f"  ✓ Created {idx_name}")
                except Exception as e:
                    if "already exists" in str(e):
                        print(f"  ℹ {idx_name} already exists")
                    else:
                        print(f"  ✗ Error: {e}")


def main():
    parser = argparse.ArgumentParser(description="Migrate Neo4j schema for typed semantic edges")
    parser.add_argument("--uri", default="bolt://localhost:7687", help="Neo4j connection URI")
    parser.add_argument("--user", default="neo4j", help="Neo4j username")
    parser.add_argument("--password", default="password", help="Neo4j password")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    subparsers.add_parser("status", help="Show current schema state")

    migrate = subparsers.add_parser(
        "migrate-edges",
        help="Migrate CALLS edges to typed semantic edges (CALLS_DIRECT by default)",
    )
    migrate.add_argument(
        "--drop-old",
        action="store_true",
        help="Verify and confirm CALLS edges are dropped after migration",
    )

    subparsers.add_parser("create-indexes", help="Create relationship indexes only")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    migrator = SchemaMigrator(args.uri, args.user, args.password)

    try:
        if args.command == "status":
            migrator.status()
        elif args.command == "migrate-edges":
            migrator.migrate_edges(drop_old=args.drop_old)
        elif args.command == "create-indexes":
            migrator.create_indexes()
    finally:
        migrator.close()


if __name__ == "__main__":
    main()
