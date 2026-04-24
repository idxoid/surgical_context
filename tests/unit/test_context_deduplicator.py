"""Unit tests for ContextDeduplicator."""

import pytest

from sidecar.context.deduplicator import ContextDeduplicator
from sidecar.context.types import Subgraph, SubgraphNode


class TestContextDeduplicator:
    """Test deduplication rules: UID normalization, line range collapse, budget tracking."""

    @pytest.fixture
    def deduplicator(self):
        return ContextDeduplicator()

    @pytest.fixture
    def primary_node(self):
        return SubgraphNode(
            uid="primary123",
            name="target_function",
            file_path="/app/main.py",
            range=[1, 10],
            token_estimate=80,
            relation="self",
            direction="primary",
            depth=0,
            relevance_score=1.0,
        )

    def test_primary_never_removed(self, deduplicator, primary_node):
        """Primary uid also in nodes → node entry removed, primary untouched."""
        duplicate_node = SubgraphNode(
            uid=primary_node.uid,
            name=primary_node.name,
            file_path=primary_node.file_path,
            range=primary_node.range,
            token_estimate=50,
            relation="duplicate",
            direction="invalid",
            depth=1,
            relevance_score=0.5,
        )
        nodes = [duplicate_node]

        subgraph = Subgraph(
            primary=primary_node,
            nodes=nodes,
            budget={"limit": 40000, "spent": 130},
        )

        result = deduplicator.deduplicate(subgraph)

        # Primary unchanged
        assert result.primary == primary_node
        # Nodes should be empty (duplicate removed)
        assert len(result.nodes) == 0

    def test_keep_lower_depth_duplicate(self, deduplicator, primary_node):
        """Same uid, depths 1 and 2 → depth-1 kept."""
        node_depth_1 = SubgraphNode(
            uid="shared_util",
            name="format_amount",
            file_path="/app/utils.py",
            range=[50, 60],
            token_estimate=60,
            relation="callee",
            direction="callee",
            depth=1,
            relevance_score=0.8,
        )
        node_depth_2 = SubgraphNode(
            uid="shared_util",
            name="format_amount",
            file_path="/app/utils.py",
            range=[50, 60],
            token_estimate=60,
            relation="callee",
            direction="callee",
            depth=2,
            relevance_score=0.8,
        )

        subgraph = Subgraph(
            primary=primary_node,
            nodes=[node_depth_2, node_depth_1],
            budget={"limit": 40000, "spent": 200},
        )

        result = deduplicator.deduplicate(subgraph)

        assert len(result.nodes) == 1
        assert result.nodes[0].depth == 1
        assert result.nodes[0].relevance_score == 0.8

    def test_keep_higher_score_equal_depth(self, deduplicator, primary_node):
        """Same uid, same depth, scores 0.9 and 0.6 → 0.9 kept."""
        node_high = SubgraphNode(
            uid="process_payment",
            name="process_payment",
            file_path="/app/payment.py",
            range=[10, 30],
            token_estimate=100,
            relation="caller",
            direction="caller",
            depth=1,
            relevance_score=0.9,
        )
        node_low = SubgraphNode(
            uid="process_payment",
            name="process_payment",
            file_path="/app/payment.py",
            range=[10, 30],
            token_estimate=100,
            relation="caller",
            direction="caller",
            depth=1,
            relevance_score=0.6,
        )

        subgraph = Subgraph(
            primary=primary_node,
            nodes=[node_low, node_high],
            budget={"limit": 40000, "spent": 180},
        )

        result = deduplicator.deduplicate(subgraph)

        assert len(result.nodes) == 1
        assert result.nodes[0].relevance_score == 0.9

    def test_collapse_same_symbol_different_ranges(self, deduplicator, primary_node):
        """Same symbol UID with different range copies → deduplicated by UID, not merged by range."""
        # This tests the UID dedup, not line range merging (which is for same file references)
        node1 = SubgraphNode(
            uid="helper",
            name="helper",
            file_path="/app/code.py",
            range=[1, 10],
            token_estimate=60,
            relation="callee",
            direction="callee",
            depth=1,
            relevance_score=0.8,
        )
        node2 = SubgraphNode(
            uid="helper",
            name="helper",
            file_path="/app/code.py",
            range=[1, 10],  # Same range, same UID
            token_estimate=60,
            relation="callee",
            direction="callee",
            depth=2,  # Different depth
            relevance_score=0.7,
        )

        subgraph = Subgraph(
            primary=primary_node,
            nodes=[node1, node2],
            budget={"limit": 40000, "spent": 200},
        )

        result = deduplicator.deduplicate(subgraph)

        # Deduplicated by UID, lower depth kept
        assert len(result.nodes) == 1
        assert result.nodes[0].depth == 1

    def test_no_cross_file_collapse(self, deduplicator, primary_node):
        """Same ranges different files → both kept."""
        node1 = SubgraphNode(
            uid="func_a",
            name="func_a",
            file_path="/app/file1.py",
            range=[1, 10],
            token_estimate=60,
            relation="callee",
            direction="callee",
            depth=1,
            relevance_score=0.8,
        )
        node2 = SubgraphNode(
            uid="func_b",
            name="func_b",
            file_path="/app/file2.py",
            range=[1, 10],
            token_estimate=60,
            relation="callee",
            direction="callee",
            depth=1,
            relevance_score=0.8,
        )

        subgraph = Subgraph(
            primary=primary_node,
            nodes=[node1, node2],
            budget={"limit": 40000, "spent": 200},
        )

        result = deduplicator.deduplicate(subgraph)

        assert len(result.nodes) == 2

    def test_unknown_file_path_skipped(self, deduplicator, primary_node):
        """file_path == '<unknown>' → no collapse attempted."""
        node_unknown = SubgraphNode(
            uid="unknown_func",
            name="unknown_func",
            file_path="<unknown>",
            range=[1, 10],
            token_estimate=50,
            relation="reference",
            direction="unknown",
            depth=2,
            relevance_score=0.5,
        )

        subgraph = Subgraph(
            primary=primary_node,
            nodes=[node_unknown],
            budget={"limit": 40000, "spent": 130},
        )

        result = deduplicator.deduplicate(subgraph)

        assert len(result.nodes) == 1
        assert result.nodes[0].file_path == "<unknown>"

    def test_budget_updated_after_dedup(self, deduplicator, primary_node):
        """Token estimates sum correctly, dedup_saved accurate."""
        node1 = SubgraphNode(
            uid="a",
            name="a",
            file_path="/app.py",
            range=[1, 5],
            token_estimate=40,
            relation="rel",
            direction="dir",
            depth=1,
            relevance_score=0.8,
        )
        node2 = SubgraphNode(
            uid="b",
            name="b",
            file_path="/app.py",
            range=[10, 15],
            token_estimate=40,
            relation="rel",
            direction="dir",
            depth=2,
            relevance_score=0.6,
        )

        subgraph = Subgraph(
            primary=primary_node,
            nodes=[node1, node2],
            budget={"limit": 40000, "spent": 160},
        )

        result = deduplicator.deduplicate(subgraph)

        # After dedup: both nodes remain (different UIDs)
        assert result.budget["spent"] == 80 + 40 + 40  # primary + 2 nodes
        assert result.budget["dedup_saved"] == 0  # no duplicates

    def test_no_duplicates_noop(self, deduplicator, primary_node):
        """Unique subgraph → identical output, dedup_saved == 0."""
        node1 = SubgraphNode(
            uid="x",
            name="x",
            file_path="/a.py",
            range=[1, 5],
            token_estimate=30,
            relation="rel",
            direction="dir",
            depth=1,
            relevance_score=0.8,
        )
        node2 = SubgraphNode(
            uid="y",
            name="y",
            file_path="/b.py",
            range=[10, 20],
            token_estimate=50,
            relation="rel",
            direction="dir",
            depth=1,
            relevance_score=0.7,
        )

        subgraph = Subgraph(
            primary=primary_node,
            nodes=[node1, node2],
            budget={"limit": 40000, "spent": 160},
        )

        result = deduplicator.deduplicate(subgraph)

        assert len(result.nodes) == 2
        assert result.budget["spent"] == 160
        assert result.budget["dedup_saved"] == 0

    def test_multiple_duplicates_chain(self, deduplicator, primary_node):
        """Three copies of same uid → lowest-depth one kept."""
        node_d1 = SubgraphNode(
            uid="util",
            name="util",
            file_path="/util.py",
            range=[1, 5],
            token_estimate=30,
            relation="rel",
            direction="dir",
            depth=1,
            relevance_score=0.9,
        )
        node_d2 = SubgraphNode(
            uid="util",
            name="util",
            file_path="/util.py",
            range=[1, 5],
            token_estimate=30,
            relation="rel",
            direction="dir",
            depth=2,
            relevance_score=0.8,
        )
        node_d3 = SubgraphNode(
            uid="util",
            name="util",
            file_path="/util.py",
            range=[1, 5],
            token_estimate=30,
            relation="rel",
            direction="dir",
            depth=3,
            relevance_score=0.7,
        )

        subgraph = Subgraph(
            primary=primary_node,
            nodes=[node_d3, node_d1, node_d2],
            budget={"limit": 40000, "spent": 170},
        )

        result = deduplicator.deduplicate(subgraph)

        assert len(result.nodes) == 1
        assert result.nodes[0].depth == 1
        assert result.budget["dedup_saved"] == 60  # Two copies removed
