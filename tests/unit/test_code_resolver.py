"""Unit tests for CodeResolver overlay scoping."""

from sidecar.context.code_resolver import CodeResolver
from sidecar.context.overlay import InMemoryOverlay


def test_code_resolver_reads_overlay_for_requesting_user_only(tmp_path):
    source = tmp_path / "module.py"
    source.write_text("on disk\n", encoding="utf-8")

    overlay = InMemoryOverlay()
    ws = "acme/repo@main"
    overlay.update(str(source), "alice unsaved\n", workspace_id=ws, user_id="alice")

    alice_resolver = CodeResolver(
        overlay, workspace_id=ws, user_id="alice", workspace_root=tmp_path
    )
    bob_resolver = CodeResolver(overlay, workspace_id=ws, user_id="bob", workspace_root=tmp_path)

    alice_code, alice_dirty = alice_resolver.resolve(str(source), 1, 1)
    bob_code, bob_dirty = bob_resolver.resolve(str(source), 1, 1)

    assert alice_dirty is True
    assert alice_code == "alice unsaved\n"
    assert bob_dirty is False
    assert bob_code == "on disk\n"
