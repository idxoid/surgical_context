"""Role-driven retrieval primitive.

Stubs Lance with synthetic axis-contract rows so the tests focus on the
ranking logic (structural-only vs structural-plus-semantic) rather than
on Lance behaviour. Real Lance + embedding integration lives in the
``axis_role_report`` QA tool.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
import pytest

WORKSPACE = "qa_repo/test@axis"

_SCAN_COLS = [
    "uid",
    "name",
    "file_path",
    "axis_contracts_json",
    "axis_container_kinds_json",
    "workspace_id",
]


def _rows_to_arrow(rows):
    """Build a real pyarrow Table from stub rows so the scan's
    ``.drop``/``.column``/``combine_chunks`` path runs on genuine
    Arrow. Vectors are a nullable list column — when a row has no
    vector (structural-only tests) the matrix extraction backs off and
    distance is ``None``, exactly as those tests expect."""
    data = {c: [r.get(c) for r in rows] for c in _SCAN_COLS}
    data["vector"] = pa.array([r.get("vector") for r in rows], type=pa.list_(pa.float32()))
    return pa.table(data)


@pytest.fixture
def fake_lance(monkeypatch):
    """Stub ``lancedb.connect(...).open_table(...)`` with controllable
    behaviour. The fixture returns the underlying ``FakeTable`` so each
    test can mutate ``rows`` and ``vector_distances``."""

    class FakeTable:
        def __init__(self):
            self.rows: list[dict[str, Any]] = []
            self.vector_distances: dict[str, float] = {}

        def to_lance(self):
            outer = self

            class _LanceWrap:
                def to_table(self, columns=None, filter=None):
                    rows = list(outer.rows)
                    if filter:
                        import re

                        m = re.search(r"workspace_id = '(.*)'", filter)
                        if m:
                            ws = m.group(1).replace("''", "'")
                            rows = [r for r in rows if r.get("workspace_id") == ws]
                    return _rows_to_arrow(rows)

            return _LanceWrap()

        def search(self, vector):
            outer = self

            class _Query:
                _ws_predicate: str | None = None
                _row_limit: int = 50

                def where(self, predicate, prefilter=False):
                    self._ws_predicate = predicate
                    return self

                def limit(self, n):
                    self._row_limit = n
                    return self

                def to_list(self):
                    results = []
                    for row in outer.rows:
                        copy = dict(row)
                        copy["_distance"] = outer.vector_distances.get(row["uid"], 0.5)
                        results.append(copy)
                    return results

            return _Query()

    class FakeDB:
        def __init__(self):
            self.table = FakeTable()

        def open_table(self, name):
            return self.table

    fake_db = FakeDB()
    monkeypatch.setattr(
        "sidecar.axis.role_retrieval.lancedb",
        type(
            "_LancedbMock",
            (),
            {"connect": staticmethod(lambda path: fake_db)},
        ),
    )
    return fake_db.table


def _row(
    uid: str,
    name: str,
    contracts: list[str],
    path: str = "/tmp/x.py",
    vector: list[float] | None = None,
) -> dict[str, Any]:
    contract_objs = [{"contract": c} for c in contracts]
    return {
        "uid": uid,
        "name": name,
        "file_path": path,
        "axis_contracts_json": json.dumps(contract_objs),
        "workspace_id": WORKSPACE,
        "vector": vector,
    }


def test_returns_empty_list_for_unknown_role(fake_lance):
    from sidecar.axis.role_retrieval import find_symbols_by_role

    fake_lance.rows = [_row("u:1", "foo", ["route_register_binding"])]
    assert find_symbols_by_role(WORKSPACE, "definitely_not_a_role") == []


def test_returns_empty_list_when_no_contracts_match_role(fake_lance):
    from sidecar.axis.role_retrieval import find_symbols_by_role

    fake_lance.rows = [_row("u:1", "foo", ["data_shape_declaration"])]
    # routing_surface only satisfied by route_register_binding.
    assert find_symbols_by_role(WORKSPACE, "routing_surface") == []


def test_structural_only_ranks_by_count_of_role_contracts(fake_lance):
    from sidecar.axis.role_retrieval import find_symbols_by_role

    fake_lance.rows = [
        # binding_surface has 9 contracts; one match → low structural score.
        _row("u:1", "minor", ["proxy_indirection"]),
        # 3 matches → higher structural score.
        _row(
            "u:3",
            "major",
            [
                "route_register_binding",
                "registry_binding_inferred",
                "metadata_key_roundtrip",
            ],
        ),
    ]
    results = find_symbols_by_role(WORKSPACE, "binding_surface")
    assert [r.name for r in results] == ["major", "minor"]
    assert results[0].contract_count == 3
    assert results[1].contract_count == 1
    # No query supplied → vector_distance is None on every result.
    assert all(r.vector_distance is None for r in results)


def test_query_text_brings_in_vector_distance_and_reweights(fake_lance):
    from sidecar.axis.role_retrieval import find_symbols_by_role

    # ``near_match`` has a stored vector that's L2-zero away from the
    # query; ``far_match`` is meaningfully distant. Both match structurally.
    fake_lance.rows = [
        _row(
            "u:far",
            "far_match",
            ["route_register_binding"],
            vector=[10.0, 10.0, 10.0, 10.0],
        ),
        _row(
            "u:near",
            "near_match",
            ["route_register_binding"],
            vector=[0.0, 0.0, 0.0, 0.0],
        ),
    ]

    results = find_symbols_by_role(
        WORKSPACE,
        "routing_surface",
        query_text="how does routing work",
        embed_fn=lambda _: [0.0, 0.0, 0.0, 0.0],
    )

    assert [r.name for r in results] == ["near_match", "far_match"]
    assert results[0].vector_distance == 0.0
    assert results[1].vector_distance > 0


def test_limit_is_respected(fake_lance):
    from sidecar.axis.role_retrieval import find_symbols_by_role

    fake_lance.rows = [_row(f"u:{i}", f"sym{i}", ["route_register_binding"]) for i in range(8)]
    results = find_symbols_by_role(WORKSPACE, "routing_surface", limit=3)
    assert len(results) == 3


def test_satisfying_contracts_only_include_role_relevant_ones(fake_lance):
    """A symbol may carry contracts that don't satisfy the requested
    role (e.g. ``data_shape_declaration`` when asking for
    ``routing_surface``). Those must NOT show up in
    ``satisfying_contracts`` — that field is the answer to "which
    contracts proved THIS role" specifically.
    """
    from sidecar.axis.role_retrieval import find_symbols_by_role

    fake_lance.rows = [
        _row(
            "u:1",
            "mixed",
            [
                "route_register_binding",  # routing_surface
                "data_shape_declaration",  # data_model_surface (unrelated)
            ],
        )
    ]
    results = find_symbols_by_role(WORKSPACE, "routing_surface")
    assert results[0].satisfying_contracts == ("route_register_binding",)
