"""Shared route dependency bridge for test monkeypatching via ``context_engine.main``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from context_engine.api.state import SidecarState


@dataclass(frozen=True)
class MainRouteDeps:
    main: Any
    state: SidecarState


_main_deps: MainRouteDeps | None = None


def configure_main_routes(deps: MainRouteDeps) -> None:
    global _main_deps
    _main_deps = deps


def require_main() -> Any:
    if _main_deps is None:
        raise RuntimeError("main routes are not configured")
    return _main_deps.main


def require_state() -> SidecarState:
    if _main_deps is None:
        raise RuntimeError("main routes are not configured")
    return _main_deps.state
