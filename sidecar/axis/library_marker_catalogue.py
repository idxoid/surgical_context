"""Catalogue: external symbol qualified name → container kind.

The catalogue is the place — and the only place — where the axis layer
admits library-specific knowledge. The discipline:

  - An entry is **(external qualified_name, container_kind)**. The
    qualified_name is workspace-independent; the kind is one of the
    structural kinds registered in :mod:`sidecar.axis.container_kind`.

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
    "aiohttp.web.Application",
    "aiohttp.web.UrlDispatcher",
    "sanic.Sanic",
    "sanic.blueprints.Blueprint",
)

# ---------------------------------------------------------------------------
# Task / queue registries: classes whose decorator/method writes register
# callables for deferred execution by a worker loop reading the same registry.
# ---------------------------------------------------------------------------
_TASK_REGISTER: tuple[str, ...] = (
    "celery.app.Celery",
    "celery.Celery",
    "dramatiq.Broker",
    "rq.Queue",
    "huey.Huey",
    "arq.connections.ArqRedis",
)

# ---------------------------------------------------------------------------
# Signal hubs: bidirectional callable storage (receivers attached, later
# iterated and called) with no web/task/data fingerprint.
# ---------------------------------------------------------------------------
_SIGNAL_REGISTER: tuple[str, ...] = (
    "blinker.Signal",
    "blinker.Namespace",
    "django.dispatch.Signal",
    "django.dispatch.dispatcher.Signal",
    "celery.utils.dispatch.Signal",
)

# ---------------------------------------------------------------------------
# Error dispatch tables: keyed map exception class → handler callable.
# ---------------------------------------------------------------------------
_ERROR_DISPATCH: tuple[str, ...] = (
    "starlette.exceptions.ExceptionMiddleware",
    "starlette.middleware.exceptions.ExceptionMiddleware",
)

# ---------------------------------------------------------------------------
# Proxy objects: objects whose attribute reads/writes resolve to a scoped
# target via ``__getattr__`` / context lookup.
# ---------------------------------------------------------------------------
_PROXY_OBJECT: tuple[str, ...] = (
    "werkzeug.local.LocalProxy",
    "werkzeug.local.Local",
    "werkzeug.local.LocalStack",
)


def _build_catalogue() -> dict[str, str]:
    out: dict[str, str] = {}
    for kind, qns in (
        ("web_route_register", _WEB_ROUTE_REGISTER),
        ("task_register", _TASK_REGISTER),
        ("signal_register", _SIGNAL_REGISTER),
        ("error_dispatch", _ERROR_DISPATCH),
        ("proxy_object", _PROXY_OBJECT),
    ):
        for qn in qns:
            if qn in out and out[qn] != kind:
                raise ValueError(
                    f"Library marker catalogue conflict for {qn!r}: "
                    f"{out[qn]!r} vs {kind!r}"
                )
            out[qn] = kind
    return out


LIBRARY_MARKER_CATALOGUE: dict[str, str] = _build_catalogue()


def kind_for_external_qualified_name(qualified_name: str) -> str | None:
    """Return the container kind a known external symbol carries, or ``None``."""
    return LIBRARY_MARKER_CATALOGUE.get(qualified_name)


__all__ = [
    "LIBRARY_MARKER_CATALOGUE",
    "kind_for_external_qualified_name",
]
