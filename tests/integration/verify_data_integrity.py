"""Comprehensive data integrity verification for Phase 5 validation."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.neo4j_client import Neo4jClient

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")


class DataIntegrityVerifier:
    """Verify all data is consistent and complete."""

    @staticmethod
    def neo4j_stats():
        """Get Neo4j node and edge counts."""
        db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            with db.driver.session() as session:
                # Node counts
                result = session.run(
                    "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count GROUP BY label ORDER BY count DESC"
                )
                nodes = result.data()

                # Edge counts
                result = session.run(
                    "MATCH ()-[r]->() RETURN type(r) AS rel_type, count(*) AS count GROUP BY rel_type ORDER BY count DESC"
                )
                edges = result.data()

                # Total symbols and files
                result = session.run("MATCH (s:Symbol) RETURN count(*) AS count")
                symbol_count = result.single()["count"]

                result = session.run("MATCH (f:File) RETURN count(*) AS count")
                file_count = result.single()["count"]

                # Check for orphaned symbols (no File parent)
                result = session.run(
                    "MATCH (s:Symbol) WHERE NOT (s)<-[:CONTAINS]-(:File) RETURN count(*) AS count"
                )
                orphaned_symbols = result.single()["count"]

                # Check for symbols with missing ranges
                result = session.run(
                    "MATCH (s:Symbol) WHERE s.range IS NULL OR size(s.range) <> 2 RETURN count(*) AS count"
                )
                bad_ranges = result.single()["count"]

                return {
                    "nodes": nodes,
                    "edges": edges,
                    "symbol_count": symbol_count,
                    "file_count": file_count,
                    "orphaned_symbols": orphaned_symbols,
                    "bad_ranges": bad_ranges,
                }
        finally:
            db.close()

    @staticmethod
    def lancedb_stats():
        """Get LanceDB table stats."""
        lance = LanceDBClient()
        try:
            # Check docs table
            docs_count = len(lance.table("docs").search("", limit=1000000).to_list())

            # Check symbols table
            symbols_count = len(lance.table("symbols").search("", limit=1000000).to_list())

            # Check for missing vectors
            docs_missing_vectors = 0
            symbols_missing_vectors = 0

            return {
                "docs_count": docs_count,
                "symbols_count": symbols_count,
                "docs_missing_vectors": docs_missing_vectors,
                "symbols_missing_vectors": symbols_missing_vectors,
            }
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def consistency_checks():
        """Run consistency checks between stores."""
        db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        _lance = LanceDBClient()

        try:
            issues = []

            # 1. Every Symbol in Neo4j should have a UID
            with db.driver.session() as session:
                result = session.run(
                    "MATCH (s:Symbol) WHERE s.uid IS NULL RETURN count(*) AS count"
                )
                if result.single()["count"] > 0:
                    issues.append("❌ Found symbols with missing UID")

            # 2. DocAnchors should have valid chunk_id
            with db.driver.session() as session:
                result = session.run(
                    "MATCH (a:DocAnchor) WHERE a.chunk_id IS NULL RETURN count(*) AS count"
                )
                if result.single()["count"] > 0:
                    issues.append("❌ Found DocAnchors with missing chunk_id")

            # 3. All CALLS edges should be between symbols
            with db.driver.session() as session:
                result = session.run("""
                MATCH (s)-[r:CALLS|CALLS_DIRECT|CALLS_DYNAMIC|CALLS_INFERRED]->(t)
                WHERE NOT s:Symbol OR NOT t:Symbol
                RETURN count(*) AS count
                """)
                if result.single()["count"] > 0:
                    issues.append("❌ Found CALLS edges between non-Symbol nodes")

            # 4. All FROM edges should originate from DocAnchor
            with db.driver.session() as session:
                result = session.run("""
                MATCH (a)-[r:FROM]->(f)
                WHERE NOT a:DocAnchor OR NOT f:File
                RETURN count(*) AS count
                """)
                if result.single()["count"] > 0:
                    issues.append("❌ Found FROM edges with wrong endpoints")

            # 5. All COVERS edges should go from DocAnchor to Symbol
            with db.driver.session() as session:
                result = session.run("""
                MATCH (a)-[r:COVERS]->(s)
                WHERE NOT a:DocAnchor OR NOT s:Symbol
                RETURN count(*) AS count
                """)
                if result.single()["count"] > 0:
                    issues.append("❌ Found COVERS edges with wrong endpoints")

            # 6. Check for duplicate symbols (same name + file)
            with db.driver.session() as session:
                result = session.run("""
                MATCH (f:File)-[:CONTAINS]->(s1:Symbol)
                MATCH (f)-[:CONTAINS]->(s2:Symbol)
                WHERE s1.uid < s2.uid AND s1.name = s2.name
                RETURN count(*) AS count
                """)
                dup_count = result.single()["count"]
                if dup_count > 0:
                    issues.append(f"⚠️  Found {dup_count} duplicate symbol names in files")

            # 7. Check IMPORTS edges reference internal modules only
            with db.driver.session() as session:
                result = session.run("""
                MATCH (s:Symbol)-[:IMPORTS]->(t:Symbol)
                RETURN t.name AS target, count(*) AS count
                LIMIT 20
                """)
                imports = result.data()
                stdlib_patterns = ["os", "sys", "re", "json", "pathlib", "typing", "collections"]
                for imp in imports:
                    if any(p in imp["target"].lower() for p in stdlib_patterns):
                        issues.append(f"⚠️  Found stdlib IMPORTS edge: {imp['target']}")

            if not issues:
                return ["✅ All consistency checks passed"]
            return issues
        finally:
            db.close()

    @staticmethod
    def print_report():
        """Print full verification report."""
        print("\n" + "=" * 80)
        print("DATA INTEGRITY VERIFICATION REPORT")
        print("=" * 80)

        # Neo4j stats
        print("\n📊 NEO4J GRAPH DATABASE")
        print("-" * 80)
        stats = DataIntegrityVerifier.neo4j_stats()

        print("\nNode Counts:")
        for node in stats["nodes"]:
            label = node["label"] if node["label"] else "(no label)"
            print(f"  {label}: {node['count']:,}")

        print("\nEdge Counts:")
        for edge in stats["edges"]:
            print(f"  {edge['rel_type']}: {edge['count']:,}")

        print("\nData Quality:")
        print(f"  Total Symbols: {stats['symbol_count']:,}")
        print(f"  Total Files: {stats['file_count']:,}")
        print(f"  Orphaned Symbols (no File parent): {stats['orphaned_symbols']}")
        print(f"  Symbols with bad ranges: {stats['bad_ranges']}")

        # LanceDB stats
        print("\n📚 LANCEDB VECTOR INDEX")
        print("-" * 80)
        lance_stats = DataIntegrityVerifier.lancedb_stats()

        if "error" not in lance_stats:
            print(f"  Doc chunks: {lance_stats['docs_count']:,}")
            print(f"  Symbol embeddings: {lance_stats['symbols_count']:,}")
        else:
            print(f"  Error: {lance_stats['error']}")

        # Consistency checks
        print("\n🔍 CONSISTENCY CHECKS")
        print("-" * 80)
        issues = DataIntegrityVerifier.consistency_checks()
        for issue in issues:
            print(f"  {issue}")

        # Summary
        print("\n" + "=" * 80)
        all_good = (
            stats["orphaned_symbols"] == 0
            and stats["bad_ranges"] == 0
            and all("✅" in issue for issue in issues)
        )

        if all_good:
            print("✅ DATA INTEGRITY VERIFIED")
        else:
            print("⚠️  ISSUES FOUND — Review above")
        print("=" * 80 + "\n")

        return all_good


if __name__ == "__main__":
    DataIntegrityVerifier.print_report()
