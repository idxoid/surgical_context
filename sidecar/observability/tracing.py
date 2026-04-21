"""Optional OpenTelemetry integration.

OpenTelemetry stays opt-in so local sidecar runs do not need another runtime
dependency. When `SIDECAR_OTEL_ENABLED=true` and opentelemetry-api is available,
request stages create spans using the process-configured tracer provider.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

_TRUTHY = {"1", "true", "yes", "on"}


def otel_enabled() -> bool:
    return os.getenv("SIDECAR_OTEL_ENABLED", "false").lower() in _TRUTHY


def _get_tracer():
    if not otel_enabled():
        return None
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return None
    return otel_trace.get_tracer("surgical_context.sidecar")


def _set_attribute(span: Any, key: str, value: Any) -> None:
    if value is None or not hasattr(span, "set_attribute"):
        return
    span.set_attribute(key, value)


@contextmanager
def start_span(name: str, attributes: dict[str, Any] | None = None):
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            _set_attribute(span, key, value)
        yield span


def set_span_attributes(span: Any, attributes: dict[str, Any]) -> None:
    for key, value in attributes.items():
        _set_attribute(span, key, value)


def record_span_exception(span: Any, exc: Exception) -> None:
    if span is None:
        return
    if hasattr(span, "record_exception"):
        span.record_exception(exc)
    _set_attribute(span, "sidecar.error", True)
    _set_attribute(span, "sidecar.error_type", type(exc).__name__)
