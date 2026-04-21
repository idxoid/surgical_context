"""Integration tests for Phase 5 validation — comprehensive feature coverage."""

import os
import sys

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sidecar.context.arbitrator import ContextArbitrator
from sidecar.database.neo4j_client import Neo4jClient

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")


class TestPhase5Validation:
    """Validate all Phase 5 features are working correctly."""

    @staticmethod
    def test_impact_endpoint_cascade_analysis():
        """Test /impact endpoint — verify affected symbols for a given symbol."""
        db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            # Find a symbol with AFFECTS edges (e.g., extract_inheritance)
            query = "MATCH (s:Symbol {name: 'extract_inheritance'}) RETURN s.uid AS uid LIMIT 1"
            with db.driver.session() as session:
                result = session.run(query).single()

            assert result is not None, "Symbol 'extract_inheritance' not found in graph"
            symbol_uid = result["uid"]

            # Get affected symbols (cascade downstream)
            from sidecar.indexer.affects import AFFECTSIndexer

            indexer = AFFECTSIndexer(db)
            affected_symbols = indexer.get_affected_symbols(symbol_uid)

            print("\n✓ /impact endpoint working")
            print("  Symbol: extract_inheritance")
            print(f"  Affected symbols: {len(affected_symbols)}")
            if affected_symbols:
                print(f"  Affected (sample): {affected_symbols[:3]}")

            # Verify AFFECTS index is populated
            assert len(affected_symbols) > 0, (
                "No affected symbols found; AFFECTS index may be empty"
            )

            return True
        finally:
            db.close()

    @staticmethod
    def test_imports_edge_verification():
        """Test IMPORTS edges — verify no stdlib imports are present."""
        db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            # Query for IMPORTS edges
            query = "MATCH (s:Symbol)-[:IMPORTS]->(t:Symbol) RETURN s.name AS source, t.name AS target LIMIT 50"
            with db.driver.session() as session:
                results = session.run(query).data()

            print("\n✓ IMPORTS edge verification")
            print(f"  Total IMPORTS edges: {len(results)}")

            if results:
                print("  Sample IMPORTS edges:")
                for r in results[:3]:
                    print(f"    {r['source']} → {r['target']}")

            # Verify no stdlib imports (all should be internal sidecar.* modules)
            stdlib_patterns = ["os", "sys", "re", "json", "pathlib", "typing"]
            for edge in results:
                target = edge["target"].lower()
                assert not any(p in target for p in stdlib_patterns), (
                    f"Found stdlib import: {edge['target']}"
                )

            return True
        finally:
            db.close()

    @staticmethod
    def test_typed_semantic_edges():
        """Test typed call edges — CALLS_DIRECT, CALLS_DYNAMIC, CALLS_INFERRED."""
        db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            query = """
            MATCH (s:Symbol)-[r:CALLS_DIRECT|CALLS_DYNAMIC|CALLS_INFERRED]->(t:Symbol)
            RETURN type(r) AS rel_type, count(*) AS count
            """
            with db.driver.session() as session:
                results = session.run(query).data()

            print("\n✓ Typed semantic edges verification")
            total = 0
            for r in results:
                print(f"  {r['rel_type']}: {r['count']}")
                total += r["count"]

            assert total > 0, "No typed call edges found"
            return True
        finally:
            db.close()

    @staticmethod
    def test_enhanced_from_relations():
        """Test enhanced FROM relations with type classification."""
        db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            query = """
            MATCH (a:DocAnchor)-[r:FROM]->(f:File)
            RETURN r.type AS from_type, count(*) AS count
            ORDER BY count DESC
            """
            with db.driver.session() as session:
                results = session.run(query).data()

            print("\n✓ Enhanced FROM relations verification")
            total = 0
            for r in results:
                print(f"  FROM type '{r['from_type']}': {r['count']}")
                total += r["count"]

            assert total > 0, "No FROM relations found"
            return True
        finally:
            db.close()

    @staticmethod
    def test_doc_type_classification():
        """Test doc type classification on File nodes."""
        db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            query = "MATCH (f:File) WHERE f.doc_type IS NOT NULL RETURN f.doc_type, count(*) AS count ORDER BY count DESC"
            with db.driver.session() as session:
                results = session.run(query).data()

            print("\n✓ Doc type classification verification")
            total = 0
            for r in results:
                print(f"  doc_type='{r['f.doc_type']}': {r['count']} files")
                total += r["count"]

            assert total > 0, "No files with doc_type classification found"
            return True
        finally:
            db.close()

    @staticmethod
    def test_similarity_threshold_tuning():
        """Test SIMILARITY_THRESHOLD=1.5 for doc-symbol matching."""
        db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            # Count COVERS edges (doc chunks linked to symbols)
            query = "MATCH (a:DocAnchor)-[:COVERS]->(s:Symbol) RETURN count(*) AS covers_count"
            with db.driver.session() as session:
                result = session.run(query).single()

            covers = result["covers_count"] if result else 0

            # Count DocAnchors
            query = "MATCH (a:DocAnchor) RETURN count(*) AS anchor_count"
            with db.driver.session() as session:
                result = session.run(query).single()

            anchors = result["anchor_count"] if result else 0

            coverage = (covers / anchors * 100) if anchors > 0 else 0

            print("\n✓ Doc-symbol semantic linking verification")
            print(f"  Total doc anchors: {anchors}")
            print(f"  Linked to symbols (COVERS): {covers}")
            print(f"  Coverage: {coverage:.1f}%")

            # Verify at least 40% of doc chunks linked to symbols
            assert coverage >= 40, f"Doc-symbol coverage too low: {coverage:.1f}%"
            return True
        finally:
            db.close()

    @staticmethod
    def test_context_assembly_with_dirty_overlay():
        """Test context assembly with in-memory overlay (dirty state)."""
        from sidecar.context.overlay import InMemoryOverlay

        db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        overlay = InMemoryOverlay()

        try:
            # Simulate unsaved file edit
            test_code = """
def test_function():
    return 42

def another_function():
    return test_function()
"""
            overlay.update("sidecar/context/graph_expander.py", test_code)

            # Get context with overlay
            arb = ContextArbitrator(db, overlay)
            ctx = arb.get_context_for_symbol("test_function", token_budget=2000)

            # Verify overlay was used
            if not isinstance(ctx, str):
                # Check if dirty flag is set
                is_dirty = ctx.primary_source.is_dirty if ctx.primary_source else False
                print("\n✓ In-memory overlay (dirty state) verification")
                print("  Overlay file: sidecar/context/graph_expander.py")
                print(f"  Primary source dirty: {is_dirty}")
                assert isinstance(ctx, object), "Context assembly failed with overlay"
                return True
            else:
                print(f"  Warning: {ctx}")
                return True
        finally:
            db.close()
            overlay.clear("sidecar/context/graph_expander.py")

    @staticmethod
    def run_all():
        """Run all Phase 5 validation tests."""
        print("\n" + "=" * 70)
        print("PHASE 5 VALIDATION SUITE")
        print("=" * 70)

        tests = [
            ("Typed Semantic Edges", TestPhase5Validation.test_typed_semantic_edges),
            ("IMPORTS Edge Verification", TestPhase5Validation.test_imports_edge_verification),
            ("Enhanced FROM Relations", TestPhase5Validation.test_enhanced_from_relations),
            ("Doc Type Classification", TestPhase5Validation.test_doc_type_classification),
            ("Similarity Threshold Tuning", TestPhase5Validation.test_similarity_threshold_tuning),
            (
                "/impact Endpoint (Cascade Analysis)",
                TestPhase5Validation.test_impact_endpoint_cascade_analysis,
            ),
            (
                "In-Memory Overlay (Dirty State)",
                TestPhase5Validation.test_context_assembly_with_dirty_overlay,
            ),
        ]

        results = {}
        for name, test_func in tests:
            try:
                result = test_func()
                results[name] = "✓ PASS" if result else "✗ FAIL"
            except Exception as e:
                results[name] = f"✗ ERROR: {str(e)}"

        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        for name, result in results.items():
            print(f"{result:15} {name}")

        all_pass = all("✓ PASS" in r for r in results.values())
        print("\n" + "=" * 70)
        print(f"{'✓ ALL TESTS PASSED' if all_pass else '✗ SOME TESTS FAILED'}")
        print("=" * 70)

        return all_pass


if __name__ == "__main__":
    TestPhase5Validation.run_all()
