"""In-process metrics and request tracing.

The sidecar runs locally, so this intentionally stays dependency-free and exports
Prometheus text directly instead of pulling in a global metrics stack.
"""

from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from time import perf_counter
from typing import Any

_LABEL_SAFE = re.compile(r"[^a-zA-Z0-9_:.-]")


def _labels_key(labels: dict[str, str] | None = None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    rendered = ",".join(f'{key}="{_escape_label_value(value)}"' for key, value in labels)
    return f"{{{rendered}}}"


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _safe_metric_name(name: str) -> str:
    return _LABEL_SAFE.sub("_", name)


def new_trace_id(incoming: str | None = None) -> str:
    """Use the caller trace id when present, otherwise create a compact UUID."""
    incoming = incoming.strip() if incoming else ""
    return incoming or uuid.uuid4().hex


def estimate_text_tokens(text: str) -> int:
    """Cheap fallback token estimate used for output/cost telemetry."""
    return max(0, (len(text) + 3) // 4)


def _env_float(name: str) -> float:
    try:
        return float(os.getenv(name, "0") or 0)
    except ValueError:
        return 0.0


def estimate_cost_usd(model_route: dict[str, Any], input_tokens: int, output_tokens: int) -> tuple[float, str]:
    """Estimate request cost from optional env-configured per-million-token rates.

    Defaults stay at zero because provider pricing changes and this local sidecar
    should not bake stale economics into code. Set e.g. CLAUDE_INPUT_COST_PER_1M
    and CLAUDE_OUTPUT_COST_PER_1M to enable non-zero estimates.
    """
    provider = str(model_route.get("provider") or "unknown").upper()
    input_rate = _env_float(f"{provider}_INPUT_COST_PER_1M")
    output_rate = _env_float(f"{provider}_OUTPUT_COST_PER_1M")
    if input_rate == 0 and output_rate == 0:
        return 0.0, "not_configured"
    total = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    return round(total, 8), "env_per_1m_tokens"


@dataclass
class RequestTrace:
    """Mutable per-request trace metadata threaded into responses and metrics."""

    trace_id: str
    endpoint: str
    workspace_id: str = ""
    stage_timings_ms: dict[str, float] = field(default_factory=dict)
    token_counts: dict[str, int] = field(default_factory=dict)
    model_route: dict[str, Any] = field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    cost_basis: str = "not_configured"

    @contextmanager
    def stage(self, name: str):
        started = perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (perf_counter() - started) * 1000
            self.stage_timings_ms[name] = round(
                self.stage_timings_ms.get(name, 0.0) + elapsed_ms,
                3,
            )

    @property
    def total_latency_ms(self) -> float:
        return round(sum(self.stage_timings_ms.values()), 3)

    def to_metadata(self, resolver_version: str) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "workspace_id": self.workspace_id,
            "resolver_version": resolver_version,
            "stage_timings_ms": dict(self.stage_timings_ms),
            "token_counts": dict(self.token_counts),
            "model_route": dict(self.model_route),
            "estimated_cost_usd": self.estimated_cost_usd,
            "cost_basis": self.cost_basis,
        }


class MetricsRegistry:
    """Small Prometheus-compatible metrics registry."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def increment(
        self,
        name: str,
        value: float = 1.0,
        labels: dict[str, str] | None = None,
    ) -> None:
        metric = (_safe_metric_name(name), _labels_key(labels))
        with self._lock:
            self._counters[metric] = self._counters.get(metric, 0.0) + value

    def observe_ms(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        self.increment(f"{name}_ms_sum", value, labels)
        self.increment(f"{name}_ms_count", 1, labels)

    def record_trace(self, trace: RequestTrace, status: str) -> None:
        labels = {"endpoint": trace.endpoint, "status": status}
        self.increment("sidecar_requests_total", labels=labels)
        self.observe_ms("sidecar_request_latency", trace.total_latency_ms, {"endpoint": trace.endpoint})
        for stage, elapsed in trace.stage_timings_ms.items():
            self.observe_ms(
                "sidecar_stage_latency",
                elapsed,
                {"endpoint": trace.endpoint, "stage": stage},
            )
        for kind, count in trace.token_counts.items():
            self.increment("sidecar_tokens_total", count, {"endpoint": trace.endpoint, "kind": kind})
        self.increment("sidecar_estimated_cost_usd_total", trace.estimated_cost_usd, {"endpoint": trace.endpoint})

    def render_prometheus(self) -> str:
        with self._lock:
            rows = sorted(self._counters.items())
        lines = [
            "# HELP sidecar_requests_total Total sidecar requests by endpoint and status.",
            "# TYPE sidecar_requests_total counter",
            "# HELP sidecar_request_latency_ms Request latency in milliseconds.",
            "# TYPE sidecar_request_latency_ms summary",
            "# HELP sidecar_stage_latency_ms Per-stage request latency in milliseconds.",
            "# TYPE sidecar_stage_latency_ms summary",
            "# HELP sidecar_tokens_total Estimated tokens processed by endpoint and kind.",
            "# TYPE sidecar_tokens_total counter",
            "# HELP sidecar_estimated_cost_usd_total Estimated request cost in USD.",
            "# TYPE sidecar_estimated_cost_usd_total counter",
        ]
        for (name, labels), value in rows:
            value_text = str(int(value)) if value.is_integer() else f"{value:.6f}".rstrip("0").rstrip(".")
            lines.append(f"{name}{_format_labels(labels)} {value_text}")
        return "\n".join(lines) + "\n"


default_metrics = MetricsRegistry()
