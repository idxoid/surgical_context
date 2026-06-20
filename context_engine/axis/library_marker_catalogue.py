"""Catalogue: external symbol qualified name → container kind.

⚠️ TRANSITION SHIM — DO NOT GROW WITHOUT REPLACEMENT PLAN.

This module is a hand-authored ``external_qn → kind`` table. By shape it is
the same fixture pattern that Phase 9.5 removed (``_target_query_bonus``,
``_GENERIC_AUTO_ROLE_PLANS``, …) — only keyed on upstream qualified names
instead of local symbol names. It exists ONLY so the L2 layer has SOME
non-zero surface on consumer-style ``app = FastAPI()`` while the structural
catalogue is built.

The structural endgame (replacement, not extension):

  1. Index external library stubs (pyi / typeshed for starlette, flask,
     celery, …) under the same L1 axis extractor.
  2. Run the L2 ``ContainerKindClassifier`` over the resulting profiles.
     The bit-signature of ``starlette.routing.Router`` will classify as
     ``web_route_register`` because the class has the registry write /
     read / dispatch fingerprint — not because we wrote it down here.
  3. Cache the result as ``external_qn → {kind, bit_signature_hash}``.
     The catalogue file becomes the *output* of an index pass, not an
     input authored by hand.
  4. Delete every literal entry in this module. Any kind that cannot be
     proved structurally on the external symbol's own AST is unearned
     and must stay unproven — same rule as for local code.

Until that bootstrap exists, treat every entry below as debt. Adding a
new entry is paying interest on a loan that has not been refinanced.

The discipline for the transition period:

  - An entry is **(external qualified_name, container_kind)**. The
    qualified_name is workspace-independent; the kind is one of the
    structural kinds registered in :mod:`context_engine.axis.container_kind`.

  - An entry is added only when the external symbol's own structural
    fingerprint matches the kind. If you cannot describe why
    ``starlette.routing.Router`` is a ``web_route_register`` in terms of
    axis bits and graph topology (registry-like write/read/dispatch on
    callables), the entry has not been earned yet — leave it out.

  - Never add an entry for a single project's internal symbol. If only
    one project's classes match, that is a fixture in disguise.

  - The catalogue grows when a *new* external symbol with the *same*
    structural fingerprint as an existing kind enters circulation
    (e.g. a new task queue framework). It does not grow when a new
    framework needs a new contract — that is a container-kind decision,
    not a catalogue decision.

Anti-patterns this catalogue is **not**:

  - Not a query → role table (that was the removed answer-key layer).
  - Not a symbol-name → role table (Router/Application/Signal in raw
    string form belong nowhere in the runtime stack).
  - Not a file-stem → role table.

Each (qn → kind) entry is a structural assertion about an external symbol,
independently testable by inspecting its upstream definition.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Web route registries: classes whose instances hold a mapping of URL/HTTP
# literal → handler callable, populated by decorator/method writes and read by
# a runtime dispatch loop.
# ---------------------------------------------------------------------------
_WEB_ROUTE_REGISTER: tuple[str, ...] = (
    "starlette.routing.Router",
    "starlette.routing.Route",
    "starlette.routing.Mount",
    "starlette.applications.Starlette",
    "flask.app.Flask",
    "flask.blueprints.Blueprint",
    "fastapi.applications.FastAPI",
    "fastapi.routing.APIRouter",
    "aiohttp.web.Application",
    "aiohttp.web.UrlDispatcher",
    "sanic.Sanic",
    "sanic.blueprints.Blueprint",
    # Re-export aliases (``flask.Flask`` → ``flask.app.Flask``,
    # ``fastapi.FastAPI`` → ``fastapi.applications.FastAPI``, …) are NOT
    # listed here. They are derived structurally from the indexed library's
    # ``RE_EXPORTS`` edges by ``QA.build_library_marker_aliases`` and
    # resolved through ``context_engine.axis.library_marker_aliases`` at lookup
    # time. Re-export plumbing therefore never needs to grow this table.
)

# ---------------------------------------------------------------------------
# Task / queue registries: classes whose decorator/method writes register
# callables for deferred execution by a worker loop reading the same registry.
# ---------------------------------------------------------------------------
_TASK_REGISTER: tuple[str, ...] = (
    # Celery's actual class lives at ``celery.app.base.Celery`` — the
    # ``celery.app.Celery`` and ``celery.Celery`` consumer forms are re-export
    # aliases resolved structurally via the alias map. Surfaced by
    # ``QA.library_marker_evidence``.
    "celery.app.base.Celery",
    "dramatiq.Broker",
    "rq.Queue",
    "huey.Huey",
    "arq.connections.ArqRedis",
)

# Signal hubs are no longer a name catalogue. ``signal_register`` is derived
# structurally from the EVENT_SUB/EVENT_PUB pub/sub topology — a symbol that is
# the target of co-occurrence-validated subscribe + publish edges — in
# ``container_kind._classify_signal_register`` via ``graph_probe.is_event_signal``.

# ``error_dispatch`` is no longer a name catalogue. Exception-handler registries
# are derived structurally from ``keyed_write`` keys that resolve to exception
# types (builtin hierarchy or ``inherits_builtin_exception`` on in-workspace
# classes) — see ``container_kind._classify_error_dispatch`` and
# ``error_dispatch_propagation.propagate_error_dispatch``.

# ``proxy_object`` is no longer a name catalogue. Lazy-resolution carriers
# are derived from ``proxy_binding`` / ``PROXY_OF`` / ``RESOLVES_ATTR`` graph
# topology and delegated-attribute method propagation — see
# ``container_kind._classify_proxy_object`` and
# ``proxy_object_propagation.propagate_proxy_object``.


def _build_catalogue() -> dict[str, str]:
    out: dict[str, str] = {}
    for kind, qns in (
        ("web_route_register", _WEB_ROUTE_REGISTER),
        ("task_register", _TASK_REGISTER),
    ):
        for qn in qns:
            if qn in out and out[qn] != kind:
                raise ValueError(
                    f"Library marker catalogue conflict for {qn!r}: {out[qn]!r} vs {kind!r}"
                )
            out[qn] = kind
    return out


LIBRARY_MARKER_CATALOGUE: dict[str, str] = _build_catalogue()


def kind_for_external_qualified_name(qualified_name: str) -> str | None:
    """Return the container kind a known external symbol carries, or ``None``.

    Looks up the literal catalogue first. On miss, attempts to resolve the
    QN through the structurally-derived alias map (re-exports from indexed
    library workspaces — :mod:`context_engine.axis.library_marker_aliases`) and
    re-queries with the canonical QN. Direct catalogue hits stay
    authoritative; aliasing is a fallback that never overrides an explicit
    entry.
    """
    direct = LIBRARY_MARKER_CATALOGUE.get(qualified_name)
    if direct is not None:
        return direct
    # Import locally to keep the alias map optional — if the JSON file is
    # absent (early bootstrap / clean checkout), catalogue still works.
    from context_engine.axis.library_marker_aliases import resolve_alias

    canonical = resolve_alias(qualified_name)
    if canonical is None:
        return None
    return LIBRARY_MARKER_CATALOGUE.get(canonical)


__all__ = [
    "LIBRARY_MARKER_CATALOGUE",
    "kind_for_external_qualified_name",
]
