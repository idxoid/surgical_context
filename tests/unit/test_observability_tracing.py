"""Tests for optional OpenTelemetry stage tracing."""

import sys
import types

from context_engine.observability.metrics import RequestTrace


class FakeSpan:
    def __init__(self):
        self.attributes = {}
        self.exceptions = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def record_exception(self, exc):
        self.exceptions.append(exc)


class FakeSpanContext:
    def __init__(self, span):
        self.span = span

    def __enter__(self):
        return self.span

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeTracer:
    def __init__(self):
        self.names = []
        self.spans = []

    def start_as_current_span(self, name):
        span = FakeSpan()
        self.names.append(name)
        self.spans.append(span)
        return FakeSpanContext(span)


def install_fake_otel(monkeypatch, tracer):
    fake_otel = types.ModuleType("opentelemetry")
    fake_otel.trace = types.SimpleNamespace(get_tracer=lambda name: tracer)
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)


def test_request_stage_creates_otel_span_when_enabled(monkeypatch):
    tracer = FakeTracer()
    install_fake_otel(monkeypatch, tracer)
    monkeypatch.setenv("SIDECAR_OTEL_ENABLED", "true")
    trace = RequestTrace(trace_id="trace-otel", endpoint="/ask", workspace_id="acme/repo@main")

    with trace.stage("context"):
        assert trace.trace_id == "trace-otel"

    assert tracer.names == ["context_engine.ask.context"]
    span = tracer.spans[0]
    assert span.attributes["context_engine.trace_id"] == "trace-otel"
    assert span.attributes["http.route"] == "/ask"
    assert span.attributes["context_engine.workspace_id"] == "acme/repo@main"
    assert span.attributes["context_engine.stage"] == "context"
    assert span.attributes["context_engine.elapsed_ms"] >= 0


def test_request_stage_records_otel_exception(monkeypatch):
    tracer = FakeTracer()
    install_fake_otel(monkeypatch, tracer)
    monkeypatch.setenv("SIDECAR_OTEL_ENABLED", "true")
    trace = RequestTrace(trace_id="trace-otel", endpoint="/ask", workspace_id="acme/repo@main")

    try:
        with trace.stage("llm"):
            raise RuntimeError("model down")
    except RuntimeError:
        pass

    span = tracer.spans[0]
    assert isinstance(span.exceptions[0], RuntimeError)
    assert span.attributes["context_engine.error"] is True
    assert span.attributes["context_engine.error_type"] == "RuntimeError"
