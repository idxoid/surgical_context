"""Intent classifier — free-text question → L4 role(s).

The bridge between a user's question and the structurally-derived role
layer. Today the legacy ``/ask`` endpoint does intent + retrieval +
ranking in one tangled cascade; this module is the smallest honest
shim that lets a future axis-only ``/ask`` reach L4 roles from plain
English / Russian without the cascade.

Mechanism: each role carries a short canonical description (one
sentence stating what kind of question the role answers). At call time
we embed both the question and every role description using the same
embedder Lance uses for symbol vectors, then rank roles by cosine
similarity. The role embeddings are cached after first use; only the
incoming question pays for an embed per call.

Discipline:

  - No keyword / regex rules. Surface-level token matching is fragile
    and quickly becomes the next ``_target_query_bonus`` (Phase 9.5).
  - No LLM call. Intent classification has to be deterministic and
    local so retrieval is reproducible across processes.
  - Every role described here must also exist in ``ROLE_CONTRACT_MAP``;
    a description that points at a non-existent role is dead code and
    flagged by a unit test.
  - Descriptions are *what kind of question the role answers*, not
    *what kind of symbol satisfies it*. That keeps the embeddings
    closer to user-shaped phrasing.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from sidecar.axis.role_resolver import ROLE_CONTRACT_MAP


# Each description is one short sentence the embedder can map to a
# query in similar shape. Keep them user-facing, not implementation-
# facing. Add a description only when a role is actually intent-shaped
# (i.e. a real consumer might ask a question that points at it).
ROLE_INTENT_DESCRIPTIONS: dict[str, str] = {
    "routing_surface": (
        "How are URL routes or HTTP endpoints registered and dispatched "
        "to handler functions?"
    ),
    "task_surface": (
        "How are background tasks, queued jobs, or scheduled workers "
        "registered and executed?"
    ),
    "error_surface": (
        "How are exception handlers, error responses, and error "
        "middleware dispatched?"
    ),
    "proxy_mechanism": (
        "How does a lazy proxy or context-bound global (like "
        "current_app, request) resolve to its underlying object?"
    ),
    "dependency_solver": (
        "How is a dependency injected into a function parameter via a "
        "provider, marker, or Depends-style binding?"
    ),
    "data_model_surface": (
        "Where are data model classes, typed records, or schema-like "
        "structures declared?"
    ),
    "configuration_surface": (
        "Where are configuration carriers, settings classes, or option "
        "defaults defined?"
    ),
    "metadata_surface": (
        "Where is keyed metadata stored, written, and read back by "
        "name?"
    ),
    "dispatch_surface": (
        "How does a middleware chain or callable container iterate and "
        "invoke its stored callables at runtime?"
    ),
    "binding_surface": (
        "How is a value bound or registered now so that it can be "
        "dispatched later by the runtime?"
    ),
}


_missing_roles = set(ROLE_INTENT_DESCRIPTIONS) - set(ROLE_CONTRACT_MAP)
if _missing_roles:  # pragma: no cover - module-load consistency check
    raise RuntimeError(
        f"intent descriptions reference unknown roles: {sorted(_missing_roles)}"
    )


@dataclass(frozen=True)
class IntentMatch:
    """One role that the classifier thinks the question is asking about."""

    role: str
    similarity: float
    description: str

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "similarity": self.similarity,
            "description": self.description,
        }


_ROLE_VECTOR_CACHE: dict[str, list[float]] = {}


def _ensure_list(vector) -> list[float]:
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    return [float(x) for x in vector]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _role_vector(role: str, embed_fn: Callable[[str], object]) -> list[float]:
    cached = _ROLE_VECTOR_CACHE.get(role)
    if cached is not None:
        return cached
    vec = _ensure_list(embed_fn(ROLE_INTENT_DESCRIPTIONS[role]))
    _ROLE_VECTOR_CACHE[role] = vec
    return vec


def clear_role_vector_cache() -> None:
    """Test/debug helper — forget cached role embeddings."""
    _ROLE_VECTOR_CACHE.clear()


def classify_intent(
    question: str,
    embed_fn: Callable[[str], object],
    *,
    top_k: int = 3,
    threshold: float = 0.20,
) -> list[IntentMatch]:
    """Return up to ``top_k`` roles whose canonical description embeds
    closest to ``question`` (cosine ≥ ``threshold``), sorted desc.

    ``embed_fn`` is supplied by the caller so this module stays
    decoupled from the concrete embedder. Production code should pass
    LanceDB's embedder so query / role / symbol vectors all share the
    same space; tests can pass any deterministic function.
    """
    if not question.strip():
        return []
    query_vec = _ensure_list(embed_fn(question))
    matches: list[IntentMatch] = []
    for role, description in ROLE_INTENT_DESCRIPTIONS.items():
        role_vec = _role_vector(role, embed_fn)
        sim = _cosine_similarity(query_vec, role_vec)
        if sim >= threshold:
            matches.append(
                IntentMatch(
                    role=role,
                    similarity=sim,
                    description=description,
                )
            )
    matches.sort(key=lambda m: m.similarity, reverse=True)
    return matches[:top_k]


__all__ = [
    "IntentMatch",
    "ROLE_INTENT_DESCRIPTIONS",
    "classify_intent",
    "clear_role_vector_cache",
]
