import pytest

from QA.axis_query_smoke import execute_axis_query, parse_axis_requirement
from sidecar.axis.query_plan import AxisQueryRequest, AxisRequirement


def test_parse_axis_requirement_accepts_axis_colon_bit():
    req = parse_axis_requirement("dfg:keyed_write")

    assert req == AxisRequirement("dfg", "keyed_write")


def test_parse_axis_requirement_rejects_missing_separator():
    with pytest.raises(ValueError, match="axis:bit"):
        parse_axis_requirement("keyed_write")


def test_execute_axis_query_searches_seeds_and_expands_graph():
    class FakeLance:
        def __init__(self):
            self.calls = []

        def search_axis_symbols(self, query, plan, *, threshold=0.4):
            self.calls.append((query, plan, threshold))
            return [{"uid": "seed-a"}, {"uid": ""}, {"uid": "seed-b"}]

    class FakeHit:
        def __init__(self, uid):
            self.uid = uid

        def to_dict(self):
            return {"uid": self.uid}

    class FakeTraversal:
        def __init__(self):
            self.calls = []

        def expand(self, seed_uids, plan):
            self.calls.append((seed_uids, plan))
            return [FakeHit("graph-a")]

    lance = FakeLance()
    traversal = FakeTraversal()
    request = AxisQueryRequest(
        traversal_mode="deferred_binding_flow",
        required_bits=(AxisRequirement("dfg", "keyed_write"),),
        container_kinds=("metadata_carrier",),
        limit=3,
    )

    result = execute_axis_query(
        query="metadata registration",
        workspace_id="ws",
        request=request,
        lance=lance,
        traversal=traversal,
        threshold=0.8,
    )

    assert result["query"] == "metadata registration"
    assert result["workspace_id"] == "ws"
    assert result["plan"]["limit"] == 3
    assert result["seeds"] == [{"uid": "seed-a"}, {"uid": ""}, {"uid": "seed-b"}]
    assert result["graph_hits"] == [{"uid": "graph-a"}]
    assert lance.calls[0][0] == "metadata registration"
    assert lance.calls[0][2] == 0.8
    assert traversal.calls[0][0] == ["seed-a", "seed-b"]
