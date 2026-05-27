"""LLM judge for automated context quality evaluation.

Feeds the assembled system_prompt to a judge model and asks it to:
  1. Answer the question using ONLY the provided context.
  2. Rate answer quality and context sufficiency per the §1.4 rubric.

Uses AIEngine as the auth/config bridge (ALLOW_CLOUD_LLM, API key, client init).
Model is selected by effort level via EFFORT_MODELS in sidecar.ai.engine:
  low    → haiku  (fast CI pass, ~20x cheaper)
  medium → sonnet (default)
  high   → opus   (release-quality judgment)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

_log = logging.getLogger(__name__)

Effort = Literal["low", "medium", "high"]
AnswerQuality = Literal["correct", "partial", "wrong"]
ContextSufficiency = Literal["sufficient", "overfed", "incomplete"]

_JUDGE_SYSTEM = """\
You are a strict code-review judge. You will receive a code context block and a question.

Rules:
- Answer the question using ONLY the information in the context block. Do not use outside knowledge.
- After your answer, output a self-assessment on two lines exactly as shown:
  ANSWER_QUALITY: <correct|partial|wrong>
  CONTEXT_SUFFICIENCY: <sufficient|overfed|incomplete>

Definitions:
  correct      — your answer is complete and accurate given the context
  partial      — your answer is correct but missing detail that the context does not provide
  wrong        — the context does not contain enough information to answer correctly
  sufficient   — the context contains exactly what was needed (no obvious excess)
  overfed      — the context contains significantly more than needed; a smaller context would give the same answer
  incomplete   — key information is missing from the context; a deeper retrieval would improve the answer
"""


@dataclass
class JudgeResult:
    answer: str
    answer_quality: AnswerQuality
    context_sufficiency: ContextSufficiency
    model: str
    effort: Effort
    input_tokens: int
    output_tokens: int


def _parse_verdict(text: str) -> tuple[AnswerQuality, ContextSufficiency]:
    aq: AnswerQuality = "wrong"
    cs: ContextSufficiency = "incomplete"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ANSWER_QUALITY:"):
            val = line.split(":", 1)[1].strip().lower()
            if val in ("correct", "partial", "wrong"):
                aq = val  # type: ignore[assignment]
        elif line.startswith("CONTEXT_SUFFICIENCY:"):
            val = line.split(":", 1)[1].strip().lower()
            if val in ("sufficient", "overfed", "incomplete"):
                cs = val  # type: ignore[assignment]
    return aq, cs


def judge_question(
    system_prompt: str,
    question: str,
    *,
    intent: str = "",
    effort: Effort = "medium",
) -> JudgeResult | None:
    """Call the judge model via AIEngine bridge. Returns None if cloud LLM unavailable."""
    from sidecar.ai.engine import AIEngine, EFFORT_MODELS

    try:
        engine = AIEngine(model_preference="claude")
    except ValueError:
        return None

    if engine.anthropic is None:
        return None

    model = EFFORT_MODELS[effort]
    user_content = f"{system_prompt}\n\n---\nQuestion: {question}"
    if intent:
        user_content = f"Intent: {intent}\n\n{user_content}"

    try:
        resp = engine.anthropic.messages.create(
            model=model,
            max_tokens=1024,
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:
        _log.warning("llm_judge: API call failed: %s", exc)
        return None

    text = resp.content[0].text if resp.content else ""
    aq, cs = _parse_verdict(text)
    return JudgeResult(
        answer=text,
        answer_quality=aq,
        context_sufficiency=cs,
        model=model,
        effort=effort,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    )
