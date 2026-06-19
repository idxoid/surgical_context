import numpy as np

from context_engine.axis.role_retrieval import (
    RoleCandidate,
    WorkspaceScan,
    find_seeds_by_doc_anchor,
    invalidate_workspace_scan_cache,
)


WORKSPACE = "test-ws"


class _FakeLance:
    def scan_doc_anchors_workspace(self, workspace_id):
        assert workspace_id == WORKSPACE
        return [
            {
                "owner_uid": "uid-module",
                "file_path": "packages/core/module.ts",
                "vector": [1.0, 0.0],
            },
            {
                "owner_uid": "uid-body",
                "file_path": "packages/core/other.ts",
                "vector": [0.0, 1.0],
            },
        ]

    def search_doc_anchors(self, query_vector, *, workspace_id, limit=12, oversample=8):
        rows = self.scan_doc_anchors_workspace(workspace_id)
        import numpy as np

        matrix = np.asarray([r["vector"] for r in rows], dtype=np.float32)
        qv = np.asarray(query_vector, dtype=np.float32)
        distances = np.linalg.norm(matrix - qv, axis=1)
        order = np.argsort(distances)[:limit]
        out = []
        for idx in order:
            row = dict(rows[int(idx)])
            row["_distance"] = float(distances[int(idx)])
            out.append(row)
        return out


def test_find_seeds_by_doc_anchor_ranks_and_maps_owner(monkeypatch):
    invalidate_workspace_scan_cache()
    scan = WorkspaceScan(
        rows=[
            {
                "uid": "uid-module",
                "name": "Module",
                "file_path": "packages/core/module.ts",
                "file_tier": "core",
                "qualified_name": "core.Module",
            },
            {
                "uid": "uid-body",
                "name": "Other",
                "file_path": "packages/core/other.ts",
                "file_tier": "core",
                "qualified_name": "core.Other",
            },
        ],
        vectors=None,
        rows_by_uid={
            "uid-module": {
                "uid": "uid-module",
                "name": "Module",
                "file_path": "packages/core/module.ts",
                "file_tier": "core",
                "qualified_name": "core.Module",
            },
            "uid-body": {
                "uid": "uid-body",
                "name": "Other",
                "file_path": "packages/core/other.ts",
                "file_tier": "core",
                "qualified_name": "core.Other",
            },
        },
    )

    out = find_seeds_by_doc_anchor(
        WORKSPACE,
        "module metadata decorator",
        embed_fn=lambda _t: np.asarray([1.0, 0.0], dtype=np.float32),
        limit=1,
        prescanned=scan,
        lance=_FakeLance(),
    )
    assert len(out) == 1
    assert out[0].role == "doc_anchor"
    assert out[0].uid == "uid-module"
    assert out[0].name == "Module"
    assert out[0].file_path == "packages/core/module.ts"


def test_find_seeds_by_doc_anchor_empty_without_query():
    assert find_seeds_by_doc_anchor(WORKSPACE, "", embed_fn=lambda t: [0.0], lance=_FakeLance()) == []
