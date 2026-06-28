"""Context-local warning collection for best-effort axis stages."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StageWarning:
    stage: str
    code: str
    message: str
    severity: str = "warning"
    error_type: str = ""
    source: str = "axis"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "error_type": self.error_type,
            "source": self.source,
            "details": dict(self.details),
        }


_WARNINGS: ContextVar[list[StageWarning] | None] = ContextVar(
    "axis_stage_warnings",
    default=None,
)


@contextmanager
def collect_stage_warnings() -> Iterator[list[StageWarning]]:
    bucket: list[StageWarning] = []
    token = _WARNINGS.set(bucket)
    try:
        yield bucket
    finally:
        _WARNINGS.reset(token)


def record_stage_warning(
    stage: str,
    code: str,
    message: str,
    *,
    error: Exception | None = None,
    details: dict[str, Any] | None = None,
    source: str = "axis",
) -> None:
    bucket = _WARNINGS.get()
    if bucket is None:
        return
    bucket.append(
        StageWarning(
            stage=stage,
            code=code,
            message=message,
            error_type=type(error).__name__ if error is not None else "",
            source=source,
            details=details or {},
        )
    )


def stage_warning_dicts(
    warnings: list[StageWarning],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for warning in warnings:
        key = (warning.stage, warning.code, warning.error_type, warning.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(warning.to_dict())
        if len(out) >= limit:
            break
    return out


__all__ = [
    "StageWarning",
    "collect_stage_warnings",
    "record_stage_warning",
    "stage_warning_dicts",
]
