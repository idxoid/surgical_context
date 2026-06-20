"""Health and Prometheus metrics routes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from context_engine.api.schemas import HealthResponse
from context_engine.observability import default_metrics

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health():
    return {"status": "ok"}


@router.get("/metrics")
def metrics():
    return PlainTextResponse(default_metrics.render_prometheus(), media_type="text/plain")
