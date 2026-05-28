"""Pass 1 role-clustering algorithm tests on synthetic graphs.

The synthetic graph contains four topologically distinct archetypes
(entry, orchestrator, executor, data class). Universal clustering must
separate them using only structural features — no name patterns, no
file paths.
"""

import json

from sidecar.indexer.role_clustering import (
    RoleCluster,
    RoleTaxonomy,
    SymbolRow,
    assemble_symbol_rows,
    build_role_catalog,
    cluster_symbols,
    resolve_role_clusters,
)


def _entry(uid: str) -> SymbolRow:
    return SymbolRow(
        uid=uid,
        kind="function",
        fan_in=8,
        fan_out=2,
        cross_package_in=8,
        cross_package_out=0,
        depth_from_public=0,
        doc_anchor_count=4,
    )


def _orchestrator(uid: str) -> SymbolRow:
    return SymbolRow(
        uid=uid,
        kind="function",
        fan_in=2,
        fan_out=8,
        cross_package_in=1,
        cross_package_out=2,
        depth_from_public=1,
        doc_anchor_count=1,
    )


def _executor(uid: str) -> SymbolRow:
    return SymbolRow(
        uid=uid,
        kind="function",
        fan_in=3,
        fan_out=0,
        cross_package_in=0,
        cross_package_out=0,
        depth_from_public=2,
        doc_anchor_count=0,
    )


def _data_class(uid: str) -> SymbolRow:
    return SymbolRow(
        uid=uid,
        kind="class",
        fan_in=10,
        fan_out=0,
        cross_package_in=6,
        cross_package_out=0,
        depth_from_public=1,
        doc_anchor_count=2,
    )


def _build_synthetic_graph() -> list[SymbolRow]:
    rows: list[SymbolRow] = []
    rows.extend(_entry(f"u:entry_{i}") for i in range(8))
    rows.extend(_orchestrator(f"u:orch_{i}") for i in range(8))
    rows.extend(_executor(f"u:exec_{i}") for i in range(20))
    rows.extend(_data_class(f"u:data_{i}") for i in range(8))
    return rows


def test_role_clustering_separates_topologically_distinct_archetypes():
    rows = _build_synthetic_graph()

    taxonomy, uid_to_cluster = cluster_symbols(rows, seed=42)

    assert taxonomy.chosen_k >= 4, taxonomy

    entry_clusters = {uid_to_cluster[f"u:entry_{i}"] for i in range(8)}
    orch_clusters = {uid_to_cluster[f"u:orch_{i}"] for i in range(8)}
    exec_clusters = {uid_to_cluster[f"u:exec_{i}"] for i in range(20)}
    data_clusters = {uid_to_cluster[f"u:data_{i}"] for i in range(8)}

    assert len(entry_clusters) == 1, f"entry symbols split: {entry_clusters}"
    assert len(orch_clusters) == 1, f"orchestrator symbols split: {orch_clusters}"
    assert len(exec_clusters) == 1, f"executor symbols split: {exec_clusters}"
    assert len(data_clusters) == 1, f"data classes split: {data_clusters}"

    distinct = entry_clusters | orch_clusters | exec_clusters | data_clusters
    assert len(distinct) == 4, f"archetypes collapsed: {distinct}"


def test_role_clustering_signatures_reference_only_structural_features():
    rows = _build_synthetic_graph()
    taxonomy, _ = cluster_symbols(rows, seed=42)

    structural_features = set(taxonomy.feature_names)
    for cluster in taxonomy.clusters:
        if cluster.member_count == 0:
            continue
        for entry in cluster.signature:
            feature, sign = entry.rsplit(":", 1)
            assert feature in structural_features, (
                f"cluster {cluster.cluster_id} signature mentions non-structural feature '{feature}'"
            )
            assert sign in ("+", "-")


def test_role_clustering_signatures_are_unique_per_archetype():
    rows = _build_synthetic_graph()
    taxonomy, uid_to_cluster = cluster_symbols(rows, seed=42)

    archetype_cids = {
        "entry": uid_to_cluster["u:entry_0"],
        "orchestrator": uid_to_cluster["u:orch_0"],
        "executor": uid_to_cluster["u:exec_0"],
        "data": uid_to_cluster["u:data_0"],
    }
    signatures = {
        name: next(c.signature for c in taxonomy.clusters if c.cluster_id == cid)
        for name, cid in archetype_cids.items()
    }

    assert len(set(signatures.values())) == len(signatures), signatures


def test_role_clustering_returns_empty_for_empty_input():
    taxonomy, mapping = cluster_symbols([])

    assert taxonomy.sample_size == 0
    assert taxonomy.clusters == ()
    assert taxonomy.chosen_k == 0
    assert mapping == {}


def test_role_clustering_collapses_to_single_cluster_when_below_kmin():
    rows = [_executor(f"u:e_{i}") for i in range(3)]

    taxonomy, mapping = cluster_symbols(rows, seed=0)

    assert taxonomy.chosen_k == 1
    assert taxonomy.clusters[0].member_count == 3
    assert set(mapping.values()) == {0}


def test_role_clustering_is_deterministic_given_a_seed():
    rows = _build_synthetic_graph()

    taxonomy_a, mapping_a = cluster_symbols(rows, seed=42)
    taxonomy_b, mapping_b = cluster_symbols(rows, seed=42)

    assert mapping_a == mapping_b
    assert taxonomy_a.chosen_k == taxonomy_b.chosen_k
    assert taxonomy_a.silhouette == taxonomy_b.silhouette


def test_assemble_symbol_rows_computes_fan_in_out_per_symbol():
    symbols = [
        ("u:a", "function", "/repo/api/a.py"),
        ("u:b", "function", "/repo/api/b.py"),
        ("u:c", "function", "/repo/core/c.py"),
    ]
    edges = [
        ("u:a", "u:b"),
        ("u:a", "u:c"),
        ("u:b", "u:c"),
    ]

    rows = {row.uid: row for row in assemble_symbol_rows(symbols, edges, {})}

    assert rows["u:a"].fan_in == 0
    assert rows["u:a"].fan_out == 2
    assert rows["u:b"].fan_in == 1
    assert rows["u:b"].fan_out == 1
    assert rows["u:c"].fan_in == 2
    assert rows["u:c"].fan_out == 0


def test_assemble_symbol_rows_separates_cross_package_edges():
    symbols = [
        ("u:api", "function", "/repo/api/handler.py"),
        ("u:core_a", "function", "/repo/core/a.py"),
        ("u:core_b", "function", "/repo/core/b.py"),
    ]
    edges = [
        ("u:api", "u:core_a"),  # cross-package out for api, cross-package in for core_a
        ("u:core_a", "u:core_b"),  # in-package edge inside core
    ]

    rows = {row.uid: row for row in assemble_symbol_rows(symbols, edges, {})}

    assert rows["u:api"].cross_package_out == 1
    assert rows["u:api"].cross_package_in == 0
    assert rows["u:core_a"].cross_package_in == 1
    assert rows["u:core_a"].cross_package_out == 0
    assert rows["u:core_b"].cross_package_in == 0
    assert rows["u:core_b"].cross_package_out == 0


def test_assemble_symbol_rows_computes_depth_from_public_via_bfs():
    symbols = [
        ("u:public", "function", "/repo/api/x.py"),
        ("u:mid", "function", "/repo/core/y.py"),
        ("u:leaf", "function", "/repo/core/z.py"),
        ("u:isolated", "function", "/repo/core/iso.py"),
    ]
    edges = [
        ("u:public", "u:mid"),
        ("u:mid", "u:leaf"),
    ]

    rows = {row.uid: row for row in assemble_symbol_rows(symbols, edges, {})}

    assert rows["u:public"].depth_from_public == 0
    assert rows["u:mid"].depth_from_public == 1
    assert rows["u:leaf"].depth_from_public == 2
    # Isolated node was never reached → bumped to max+1 so the standardizer
    # still sees a finite, distinguishing value.
    assert rows["u:isolated"].depth_from_public == 3


def test_assemble_symbol_rows_attaches_doc_anchor_counts():
    symbols = [
        ("u:a", "function", "/repo/api/a.py"),
        ("u:b", "function", "/repo/api/b.py"),
    ]

    rows = {row.uid: row for row in assemble_symbol_rows(symbols, [], {"u:a": 5})}

    assert rows["u:a"].doc_anchor_count == 5
    assert rows["u:b"].doc_anchor_count == 0


def test_assemble_symbol_rows_attaches_weighted_doc_anchor_signals():
    symbols = [
        ("u:a", "function", "/repo/api/a.py"),
        ("u:b", "function", "/repo/api/b.py"),
    ]

    rows = {
        row.uid: row
        for row in assemble_symbol_rows(
            symbols,
            [],
            {"u:a": 3},
            doc_signal_by_uid={"u:a": {"definition": 1.4, "reference": 0.6, "example": 0.2}},
        )
    }

    assert rows["u:a"].doc_definition_weight == 1.4
    assert rows["u:a"].doc_reference_weight == 0.6
    assert rows["u:a"].doc_example_weight == 0.2
    assert rows["u:b"].doc_definition_weight == 0.0


def test_assemble_symbol_rows_falls_back_to_graph_sources_when_no_cross_package():
    symbols = [
        ("u:src", "function", "/repo/core/x.py"),
        ("u:mid", "function", "/repo/core/y.py"),
        ("u:leaf", "function", "/repo/core/z.py"),
    ]
    edges = [
        ("u:src", "u:mid"),
        ("u:mid", "u:leaf"),
    ]

    rows = {row.uid: row for row in assemble_symbol_rows(symbols, edges, {})}

    assert rows["u:src"].depth_from_public == 0
    assert rows["u:mid"].depth_from_public == 1
    assert rows["u:leaf"].depth_from_public == 2


def test_assemble_symbol_rows_threads_import_in_per_uid():
    symbols = [
        ("u:popular", "function", "/repo/api/x.py"),
        ("u:obscure", "function", "/repo/internal/y.py"),
    ]

    rows = {
        row.uid: row
        for row in assemble_symbol_rows(symbols, [], {}, import_in_per_uid={"u:popular": 12})
    }

    assert rows["u:popular"].import_in == 12
    assert rows["u:obscure"].import_in == 0


def test_features_separate_documented_from_undocumented():
    documented = SymbolRow(
        uid="u:doc",
        kind="function",
        fan_in=1,
        fan_out=1,
        cross_package_in=0,
        cross_package_out=0,
        depth_from_public=1,
        doc_anchor_count=1,
    )
    bare = SymbolRow(
        uid="u:bare",
        kind="function",
        fan_in=1,
        fan_out=1,
        cross_package_in=0,
        cross_package_out=0,
        depth_from_public=1,
        doc_anchor_count=0,
    )
    taxonomy, mapping = cluster_symbols(
        [documented] * 6 + [bare] * 6,
        seed=0,
        k_min=2,
        k_max=2,
    )

    assert taxonomy.chosen_k == 2
    assert mapping["u:doc"] != mapping["u:bare"]


def test_assemble_symbol_rows_drops_edges_referencing_unknown_symbols():
    symbols = [("u:a", "function", "/repo/api/a.py")]
    edges = [
        ("u:a", "u:missing"),
        ("u:missing", "u:a"),
        ("u:a", "u:a"),  # self-loop — also dropped
    ]

    rows = assemble_symbol_rows(symbols, edges, {})

    assert len(rows) == 1
    assert rows[0].fan_in == 0
    assert rows[0].fan_out == 0


def test_role_clustering_serializes_for_workspace_persistence():
    rows = _build_synthetic_graph()
    taxonomy, _ = cluster_symbols(rows, seed=42)

    payload = taxonomy.to_dict()

    assert payload["feature_names"] == list(taxonomy.feature_names)
    assert payload["chosen_k"] == taxonomy.chosen_k
    assert payload["sample_size"] == len(rows)
    for cluster_payload in payload["clusters"]:
        assert {"cluster_id", "centroid", "member_count", "signature"}.issubset(cluster_payload)
        assert isinstance(cluster_payload["centroid"], list)
        assert isinstance(cluster_payload["signature"], list)


def test_role_catalog_maps_cluster_shapes_to_portable_archetypes():
    taxonomy = RoleTaxonomy(
        feature_names=(
            "log_fan_in",
            "log_fan_out",
            "fan_in_ratio",
            "depth_from_public",
            "leaf_score",
            "cross_package_in_ratio",
            "cross_package_out_ratio",
            "log_import_in",
            "has_documentation",
            "doc_anchor_density",
            "log_doc_definition_weight",
            "log_doc_reference_weight",
            "log_doc_example_weight",
            "is_class",
            "is_function",
        ),
        clusters=(
            RoleCluster(
                cluster_id=10,
                centroid=(
                    0.0,
                    1.6,
                    -0.5,
                    -1.5,
                    -1.5,
                    0.0,
                    0.8,
                    0.0,
                    0.2,
                    0.2,
                    0.0,
                    0.0,
                    0.0,
                    -0.5,
                    1.2,
                ),
                member_count=20,
                signature=("log_fan_out:+", "leaf_score:-", "depth_from_public:-"),
            ),
            RoleCluster(
                cluster_id=20,
                centroid=(
                    1.6,
                    -0.4,
                    1.4,
                    0.2,
                    0.8,
                    1.4,
                    -0.2,
                    0.0,
                    0.1,
                    0.1,
                    0.0,
                    0.0,
                    0.0,
                    0.4,
                    -0.2,
                ),
                member_count=12,
                signature=("log_fan_in:+", "cross_package_in_ratio:+", "fan_in_ratio:+"),
            ),
            RoleCluster(
                cluster_id=30,
                centroid=(
                    -0.5,
                    -1.0,
                    -0.3,
                    0.8,
                    0.9,
                    0.0,
                    -0.2,
                    0.0,
                    1.6,
                    1.7,
                    1.8,
                    1.1,
                    -0.7,
                    0.1,
                    -0.1,
                ),
                member_count=16,
                signature=("log_doc_definition_weight:+", "doc_anchor_density:+", "log_fan_out:-"),
            ),
            RoleCluster(
                cluster_id=40,
                centroid=(
                    -0.2,
                    -0.5,
                    0.2,
                    0.7,
                    0.9,
                    0.1,
                    0.0,
                    0.0,
                    0.3,
                    0.2,
                    0.0,
                    0.0,
                    0.0,
                    1.6,
                    -1.4,
                ),
                member_count=10,
                signature=("is_class:+", "is_function:-", "leaf_score:+"),
            ),
        ),
        silhouette=0.5,
        chosen_k=4,
        sample_size=58,
    )

    catalog = build_role_catalog(taxonomy)

    assert catalog.archetypes["active_entrypoint"][0].cluster_id == 10
    assert catalog.archetypes["runtime_handle"][0].cluster_id == 20
    assert catalog.archetypes["passive_api_surface"][0].cluster_id == 30
    assert catalog.archetypes["representation_surface"][0].cluster_id == 40


def test_role_catalog_resolves_canonical_roles_to_cluster_preferences():
    rows = _build_synthetic_graph()
    taxonomy, mapping = cluster_symbols(rows, seed=42)
    catalog = build_role_catalog(taxonomy)

    core_matches = resolve_role_clusters(catalog, "core_runtime")
    api_matches = resolve_role_clusters(catalog.to_dict(), "api_surface")

    assert core_matches
    assert api_matches
    assert mapping["u:data_0"] in {match["cluster_id"] for match in core_matches}
    assert any(
        match["archetype"] in {"passive_api_surface", "active_entrypoint"} for match in api_matches
    )


# Example (inactive): same flask_registration.yaml template as test_mechanism_registry.
# Uncomment together with the pack YAML when activating Flask auto:registration_flow.
#
# def test_role_catalog_payload_includes_preloaded_mechanism_profiles(monkeypatch):
#     from pathlib import Path
#
#     from sidecar.context.mechanism_registry import (
#         ROLE_CATALOG_MECHANISM_BACKFILL_KEY,
#         ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY,
#         merge_preloaded_mechanisms_into_role_catalog,
#     )
#
#     pack = (
#         Path(__file__).resolve().parents[2]
#         / "sidecar/context/mechanism_packs/bundled/flask_registration.yaml"
#     )
#     monkeypatch.setenv("MECHANISM_PACK_PATH", str(pack))
#
#     taxonomy, _ = cluster_symbols([], seed=0)
#     catalog = build_role_catalog(taxonomy)
#     merged = merge_preloaded_mechanisms_into_role_catalog(catalog.to_dict())
#
#     assert merged["schema_version"] == catalog.schema_version
#     assert ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY in merged
#     assert ROLE_CATALOG_MECHANISM_BACKFILL_KEY in merged
#     assert merged[ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY] == {}
#     assert merged[ROLE_CATALOG_MECHANISM_BACKFILL_KEY].get("auto:registration_flow")
#
#     json.dumps(merged, sort_keys=True)
