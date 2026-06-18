"""Tests for structured request/stage logging."""

import json
import logging

from context_engine.observability.metrics import MetricsRegistry, RequestTrace


def _json_messages(records):
    return [
        json.loads(record.message)
        for record in records
        if record.name == "context_engine.observability.metrics"
    ]


def test_request_trace_stage_emits_structured_log(caplog):
    caplog.set_level(logging.INFO, logger="context_engine.observability.metrics")
    trace = RequestTrace(trace_id="trace-1", endpoint="/ask", workspace_id="acme/repo@main")

    with trace.stage("context"):
        pass

    events = _json_messages(caplog.records)
    stage_event = next(event for event in events if event["event"] == "context_engine.stage")
    assert stage_event["trace_id"] == "trace-1"
    assert stage_event["endpoint"] == "/ask"
    assert stage_event["workspace_id"] == "acme/repo@main"
    assert stage_event["stage"] == "context"
    assert stage_event["elapsed_ms"] >= 0


def test_metrics_registry_record_trace_emits_request_summary(caplog):
    caplog.set_level(logging.INFO, logger="context_engine.observability.metrics")
    registry = MetricsRegistry()
    trace = RequestTrace(trace_id="trace-2", endpoint="/ask", workspace_id="acme/repo@main")
    trace.stage_timings_ms = {"context": 1.25, "llm": 2.5}
    trace.token_counts = {"context": 42}
    trace.model_route = {"provider": "ollama", "model": "llama3"}

    registry.record_trace(trace, status="ok")

    events = _json_messages(caplog.records)
    request_event = next(event for event in events if event["event"] == "context_engine.request")
    assert request_event["trace_id"] == "trace-2"
    assert request_event["status"] == "ok"
    assert request_event["total_latency_ms"] == 3.75
    assert request_event["latency_slo"]["status"] == "met"
    assert request_event["latency_slo"]["target_ms"] == 200.0
    assert request_event["stage_timings_ms"] == {"context": 1.25, "llm": 2.5}
    assert request_event["token_counts"] == {"context": 42}
    assert request_event["model_route"]["provider"] == "ollama"


def test_metrics_registry_tracks_latency_slo_breach(monkeypatch, caplog):
    monkeypatch.setenv("SIDECAR_REQUEST_LATENCY_SLO_MS", "3")
    caplog.set_level(logging.INFO, logger="context_engine.observability.metrics")
    registry = MetricsRegistry()
    trace = RequestTrace(trace_id="trace-3", endpoint="/ask", workspace_id="acme/repo@main")
    trace.stage_timings_ms = {"context": 1.25, "llm": 2.5}

    registry.record_trace(trace, status="ok")

    rendered = registry.render_prometheus()
    assert (
        'sidecar_request_slo_checks_total{endpoint="/ask",status="breached",target_ms="3"} 1'
        in rendered
    )
    assert 'sidecar_request_slo_violations_total{endpoint="/ask",target_ms="3"} 1' in rendered

    events = _json_messages(caplog.records)
    request_event = next(event for event in events if event["event"] == "context_engine.request")
    assert request_event["latency_slo"] == {
        "target_ms": 3.0,
        "status": "breached",
        "breached": True,
        "latency_ms": 3.75,
    }
