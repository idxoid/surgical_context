from types import SimpleNamespace

from sidecar.axis import role_retrieval as rr
from sidecar.axis.role_retrieval import WorkspaceScan, _row_indices_for_evidence


def test_workspace_scan_builds_contract_and_kind_indexes():
    scan = WorkspaceScan(
        rows=[
            {
                "uid": "a",
                "_contracts": {"c1"},
                "_kinds": {"k1"},
                "_idx": 0,
            },
            {
                "uid": "b",
                "_contracts": {"c2"},
                "_kinds": set(),
                "_idx": 1,
            },
        ],
        vectors=None,
    )
    assert scan.contract_index["c1"] == (0,)
    assert scan.contract_index["c2"] == (1,)
    assert scan.kind_index["k1"] == (0,)


def test_row_indices_for_evidence_unions_contract_and_kind_hits():
    scan = WorkspaceScan(
        rows=[
            {"uid": "a", "_contracts": {"c1"}, "_kinds": set(), "_idx": 0},
            {"uid": "b", "_contracts": set(), "_kinds": {"k1"}, "_idx": 1},
        ],
        vectors=None,
    )
    evidence = SimpleNamespace(contracts={"c1"}, kinds={"k1"})
    assert _row_indices_for_evidence(scan, evidence) == (0, 1)


def test_scan_cache_hit_short_circuits_lance_io():
    rr.invalidate_workspace_scan_cache()
    scan = WorkspaceScan(rows=[], vectors=None)
    key = rr._scan_cache_key("ws", "./data/lancedb", False, True)
    rr._SCAN_CACHE[key] = scan
    assert rr.scan_workspace_rows("ws") is scan
    rr.invalidate_workspace_scan_cache()
