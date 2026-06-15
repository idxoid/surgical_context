"""Smoke checks for the library marker catalogue.

The catalogue is structural data, not a question→answer table. These tests
exist to catch *catalogue hygiene* regressions (duplicate qns mapped to
different kinds, unknown container kinds slipping in) — not to assert
specific framework names. Adding or removing a single entry should NOT
require updating these tests; only changing the catalogue shape should.
"""

from __future__ import annotations

import pytest

from sidecar.axis.container_kind import ContainerKindClassifier
from sidecar.axis.library_marker_catalogue import (
    LIBRARY_MARKER_CATALOGUE,
    kind_for_external_qualified_name,
)


def test_catalogue_is_non_empty():
    assert LIBRARY_MARKER_CATALOGUE
    assert all(qn and "." in qn for qn in LIBRARY_MARKER_CATALOGUE)


def test_every_catalogue_kind_is_a_registered_container_kind():
    registered = set(ContainerKindClassifier().registered_kinds())
    catalogue_kinds = set(LIBRARY_MARKER_CATALOGUE.values())
    unknown = catalogue_kinds - registered
    assert not unknown, (
        f"Catalogue maps to unknown container kinds: {sorted(unknown)}. "
        "Either register the kind in container_kind.py or remove the entry."
    )


def test_no_duplicate_qns_with_conflicting_kinds():
    # ``_build_catalogue`` already raises on conflict; this guards against
    # someone bypassing the builder.
    seen: dict[str, str] = {}
    for qn, kind in LIBRARY_MARKER_CATALOGUE.items():
        assert seen.setdefault(qn, kind) == kind, f"{qn} mapped twice"


@pytest.mark.parametrize(
    "qn, kind",
    [
        ("starlette.routing.Router", "web_route_register"),
        ("celery.app.base.Celery", "task_register"),
        ("werkzeug.local.LocalProxy", "proxy_object"),
    ],
)
def test_lookup_returns_expected_kind(qn: str, kind: str):
    assert kind_for_external_qualified_name(qn) == kind


def test_lookup_returns_none_for_unknown_qn():
    assert kind_for_external_qualified_name("not.in.catalogue") is None


def test_lookup_resolves_through_structural_alias_map():
    """The 4 re-export aliases were removed from the literal catalogue and now
    resolve structurally via ``sidecar.axis.library_marker_aliases`` (built
    from ``RE_EXPORTS`` edges of indexed library workspaces). The consumer
    QN ``flask.Flask`` must still classify as ``web_route_register``.
    """
    # No literal entry for the consumer form…
    assert "flask.Flask" not in LIBRARY_MARKER_CATALOGUE
    assert "fastapi.FastAPI" not in LIBRARY_MARKER_CATALOGUE
    # …but the canonical form is in the literal catalogue,
    assert LIBRARY_MARKER_CATALOGUE["flask.app.Flask"] == "web_route_register"
    # …and the resolver bridges consumer → canonical:
    assert kind_for_external_qualified_name("flask.Flask") == "web_route_register"
    assert kind_for_external_qualified_name("fastapi.FastAPI") == "web_route_register"
    assert kind_for_external_qualified_name("flask.Blueprint") == "web_route_register"
    assert kind_for_external_qualified_name("fastapi.APIRouter") == "web_route_register"
