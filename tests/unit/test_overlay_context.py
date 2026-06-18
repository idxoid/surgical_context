"""Overlay merge into axis context bundles."""

from __future__ import annotations

from context_engine.axis.context_builder import ContextBundle, ContextSymbol
from context_engine.axis.overlay_context import (
    apply_dirty_overlay_to_bundles,
    merge_saved_overlay_payloads,
)
from context_engine.overlay import InMemoryOverlay

WORKSPACE = "qa_repo/test@overlay"


def test_saved_overlay_replaces_lance_code_at_fetch():
    ov = InMemoryOverlay()
    ov.update(
        "/proj/foo.py",
        "def registry():\n    return 'saved'\n",
        workspace_id=WORKSPACE,
        dirty=False,
    )
    payloads = {
        "u:1": {
            "code": "def registry():\n    return 'old'\n",
            "name": "registry",
            "file_path": "/proj/foo.py",
            "qualified_name": "registry",
        }
    }
    merged = merge_saved_overlay_payloads(
        payloads,
        overlay=ov,
        workspace_id=WORKSPACE,
        user_id="anonymous",
    )
    assert "saved" in (merged["u:1"]["code"] or "")


def test_dirty_overlay_patches_bundles_before_budget():
    ov = InMemoryOverlay()
    ov.update(
        "/proj/foo.py",
        "def registry():\n    return 'dirty'\n",
        workspace_id=WORKSPACE,
        dirty=True,
    )
    bundle = ContextBundle(
        role="binding_surface",
        seed=ContextSymbol(
            uid="u:1",
            name="registry",
            file_path="/proj/foo.py",
            role="binding_surface",
            distance_from_seed=0,
            expansion_step=None,
            code="def registry():\n    return 'committed'\n",
        ),
        related=(),
        utility_score=1.0,
    )
    [patched] = apply_dirty_overlay_to_bundles(
        [bundle],
        overlay=ov,
        workspace_id=WORKSPACE,
        user_id="anonymous",
    )
    assert "dirty" in (patched.seed.code or "")
