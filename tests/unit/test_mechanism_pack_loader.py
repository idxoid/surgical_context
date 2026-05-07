"""YAML mechanism pack loader (no indexer)."""

from __future__ import annotations

import json

from sidecar.context.mechanism_packs.loader import (
    ROLE_CATALOG_MECHANISM_BACKFILL_KEY,
    ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY,
    load_pack_overlay_merged,
    merge_into_base_extensions,
)


def test_merge_into_base_extensions_with_pack(tmp_path, monkeypatch):
    pack = tmp_path / "extra.yaml"
    pack.write_text(
        """
mechanism_required_roles:
  pack_mech:
    - api_surface
    - runtime_surface
mechanism_role_backfill:
  pack_mech:
    api_surface:
      - name: PackSymbol
        path_hint: /pkg
        priority: 0.91
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MECHANISM_PACK_PATH", str(pack))
    base = {
        ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {},
        ROLE_CATALOG_MECHANISM_BACKFILL_KEY: {},
    }
    merged = merge_into_base_extensions(base)
    assert merged[ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY]["pack_mech"] == [
        "api_surface",
        "runtime_surface",
    ]
    rows = merged[ROLE_CATALOG_MECHANISM_BACKFILL_KEY]["pack_mech"]["api_surface"]
    assert rows[0]["name"] == "PackSymbol"
    json.dumps(merged)


def test_pack_overlay_empty_without_mechanism_pack_path(monkeypatch):
    monkeypatch.delenv("MECHANISM_PACK_PATH", raising=False)
    overlay = load_pack_overlay_merged()
    assert ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY in overlay
    assert overlay[ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY] == {}
    assert overlay[ROLE_CATALOG_MECHANISM_BACKFILL_KEY] == {}
