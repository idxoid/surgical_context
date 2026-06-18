"""Client-safe error messages for HTTP and SSE responses."""

from __future__ import annotations

PUBLIC_INTERNAL_ERROR = "An internal error occurred"
INDEX_FAILED_REASON = "index_failed"
LLM_UNREACHABLE_REASON = "llm_unreachable_context_only"

_DEGRADED_LLM_ANSWER = (
    "The language model is currently unreachable, so this is a degraded "
    "context-only response. The assembled context is still included below "
    "for inspection."
)


def degraded_llm_answer() -> str:
    """User-visible text when the LLM is unreachable but context is still returned."""
    return _DEGRADED_LLM_ANSWER
