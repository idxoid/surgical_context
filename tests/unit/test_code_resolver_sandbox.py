"""CodeResolver graph path sandbox tests."""

from sidecar.context.code_resolver import CodeResolver


def test_code_resolver_skips_paths_outside_workspace_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    inside = root / "in.py"
    inside.write_text("def ok():\n    pass\n", encoding="utf-8")
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET\n", encoding="utf-8")

    resolver = CodeResolver(workspace_root=root)
    code, dirty = resolver.resolve(str(outside), 1, 1)

    assert code == ""
    assert dirty is False

    code_in, _ = resolver.resolve(str(inside), 1, 2)
    assert "def ok" in code_in
