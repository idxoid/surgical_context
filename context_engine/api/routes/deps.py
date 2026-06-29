"""Per-app route dependency resolution.

Deps live on ``app.state.route_deps`` (set by ``create_app``); the HTTP path
resolves them from the incoming ``Request`` so multiple app instances in one
process stay isolated. ``_default_deps`` is only a fallback for direct
(non-HTTP) calls — unit tests invoke route functions without a ``Request``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import Header, Request

from context_engine.api.state import SidecarState

UserIdHeader = Annotated[str | None, Header()]
AuthHeader = Annotated[str | None, Header()]
WorkspaceHeader = Annotated[str | None, Header()]
TraceIdHeader = Annotated[str | None, Header()]


@dataclass(frozen=True)
class MainRouteDeps:
    services: Any
    state: SidecarState


_default_deps: MainRouteDeps | None = None


def configure_main_routes(deps: MainRouteDeps) -> None:
    """Bind the direct-call fallback deps (HTTP requests resolve per-app)."""
    global _default_deps
    _default_deps = deps


def route_deps(request: Request | None = None) -> MainRouteDeps:
    if request is not None:
        deps = getattr(request.app.state, "route_deps", None)
        if deps is not None:
            return cast(MainRouteDeps, deps)
    if _default_deps is None:
        raise RuntimeError("main routes are not configured")
    return _default_deps


def require_services(request: Request | None = None) -> Any:
    return route_deps(request).services


def require_state(request: Request | None = None) -> SidecarState:
    return route_deps(request).state
