"""LLM judge for automated context quality evaluation.

Feeds assembled context to judge models via local CLI bridges (Claude Code + Codex),
not direct HTTP APIs. For each question runs a matrix of providers × effort tiers in
parallel (default: 6 calls — claude + codex × low/medium/high).

Effort → model mapping (override per cell with QA_JUDGE_<PROVIDER>_MODEL_<TIER>):
  low    → haiku / gpt-5.4-mini
  medium → sonnet / gpt-5.4
  high   → opus / gpt-5.4
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Literal

from QA.llm_bridge import BridgeRequest, build_bridge_provider

_log = logging.getLogger(__name__)

Effort = Literal["low", "medium", "high"]
Provider = Literal["claude", "codex"]
AnswerQuality = Literal["correct", "partial", "wrong"]
ContextSufficiency = Literal["sufficient", "overfed", "incomplete"]

EFFORTS: tuple[Effort, ...] = ("low", "medium", "high")
PROVIDERS: tuple[Provider, ...] = ("claude", "codex")

_DEFAULT_TIER_MODELS: dict[Provider, dict[Effort, str]] = {
    "claude": {
        "low": "claude-haiku-4-5-20251001",
        "medium": "claude-sonnet-4-6",
        "high": "claude-opus-4-7",
    },
    "codex": {
        "low": "gpt-5.4-mini",
        "medium": "gpt-5.4",
        "high": "gpt-5.4",
    },
}

_JUDGE_SYSTEM = """\
You are a strict code-review judge. You will receive a code context block and a question.

Rules:
- Answer the question using ONLY the information in the context block. Do not use outside knowledge.
- After your answer, output a self-assessment block with these lines exactly:
  VERDICT: <pass|warn|fail>
  CORRECTNESS: <correct|partial|wrong>
  GROUNDING: <grounded|mixed|ungrounded>
  COMPLETENESS: <complete|partial|insufficient>
  CONTEXT_SUFFICIENT: <yes|no>
  UNSUPPORTED_CLAIMS: <none|short phrase>
  MISSING_EVIDENCE: <none|short phrase>
  NOTES: <optional short phrase or none>

Also include legacy lines (must match CORRECTNESS / CONTEXT_SUFFICIENCY):
  ANSWER_QUALITY: <correct|partial|wrong>
  CONTEXT_SUFFICIENCY: <sufficient|overfed|incomplete>

Definitions:
  pass/warn/fail — overall judgment: pass=answerable from context; warn=partial; fail=wrong/ungrounded
  grounded — all claims trace to context; mixed — some inference; ungrounded — outside knowledge or hallucination
  complete/partial/insufficient — answer coverage relative to the question
  CONTEXT_SUFFICIENT yes — context had what was needed; no — retrieval gap (maps to incomplete/overfed context)
"""


@dataclass
class JudgeResult:
    answer: str
    answer_quality: AnswerQuality
    context_sufficiency: ContextSufficiency
    provider: Provider
    model: str
    effort: Effort
    input_tokens: int
    output_tokens: int
    verdict: str = "fail"
    correctness: str = "wrong"
    grounding: str = "ungrounded"
    completeness: str = "insufficient"
    context_sufficient: str = "no"
    unsupported_claims: str = "none"
    missing_evidence: str = "none"
    notes: str = ""
    latency_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def tier_model(provider: Provider, effort: Effort) -> str:
    env_key = f"QA_JUDGE_{provider.upper()}_MODEL_{effort.upper()}"
    override = (os.getenv(env_key) or "").strip()
    if override:
        return override
    return _DEFAULT_TIER_MODELS[provider][effort]


def bridges_available() -> dict[str, bool]:
    return {
        "claude": build_bridge_provider("claude-code").healthcheck(),
        "codex": build_bridge_provider("codex").healthcheck(),
    }


def _line_value(text: str, key: str) -> str:
    prefix = f"{key}:"
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith(prefix.upper()):
            return line.split(":", 1)[1].strip()
    return ""


def _derive_verdict(aq: AnswerQuality, cs: ContextSufficiency) -> str:
    if aq == "wrong":
        return "fail"
    if aq == "partial" or cs == "incomplete":
        return "warn"
    if aq == "correct":
        return "pass"
    return "fail"


def _parse_verdict(text: str) -> dict[str, str]:
    aq_raw = _line_value(text, "CORRECTNESS") or _line_value(text, "ANSWER_QUALITY")
    cs_raw = _line_value(text, "CONTEXT_SUFFICIENCY")
    aq: AnswerQuality = "wrong"
    if aq_raw.lower() in ("correct", "partial", "wrong"):
        aq = aq_raw.lower()  # type: ignore[assignment]
    cs: ContextSufficiency = "incomplete"
    if cs_raw.lower() in ("sufficient", "overfed", "incomplete"):
        cs = cs_raw.lower()  # type: ignore[assignment]

    ctx_suff_raw = _line_value(text, "CONTEXT_SUFFICIENT").lower()
    if ctx_suff_raw in ("yes", "no"):
        context_sufficient = ctx_suff_raw
    else:
        context_sufficient = "yes" if cs == "sufficient" else "no"

    verdict = _line_value(text, "VERDICT").lower()
    if verdict not in ("pass", "warn", "fail"):
        verdict = _derive_verdict(aq, cs)

    correctness = aq_raw.lower() if aq_raw else aq
    grounding = _line_value(text, "GROUNDING").lower() or (
        "grounded" if aq == "correct" else "mixed" if aq == "partial" else "ungrounded"
    )
    completeness = _line_value(text, "COMPLETENESS").lower() or (
        "complete" if aq == "correct" else "partial" if aq == "partial" else "insufficient"
    )
    unsupported = _line_value(text, "UNSUPPORTED_CLAIMS") or "none"
    missing = _line_value(text, "MISSING_EVIDENCE") or (
        "none" if cs != "incomplete" else "context gap"
    )
    notes = _line_value(text, "NOTES") or ""

    return {
        "answer_quality": aq,
        "context_sufficiency": cs,
        "verdict": verdict,
        "correctness": correctness,
        "grounding": grounding,
        "completeness": completeness,
        "context_sufficient": context_sufficient,
        "unsupported_claims": unsupported,
        "missing_evidence": missing,
        "notes": notes,
    }


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def judge_question_via_bridge(
    system_prompt: str,
    question: str,
    *,
    provider: Provider,
    effort: Effort,
    intent: str = "",
) -> JudgeResult:
    """Single judge call via one CLI bridge."""
    bridge_name = "claude-code" if provider == "claude" else "codex"
    model = tier_model(provider, effort)
    user_content = f"{system_prompt}\n\n---\nQuestion: {question}"
    if intent:
        user_content = f"Intent: {intent}\n\n{user_content}"

    t0 = time.monotonic()
    try:
        bridge = build_bridge_provider(bridge_name)
        response = bridge.complete(
            BridgeRequest(system=_JUDGE_SYSTEM, prompt=user_content, model=model)
        )
    except Exception as exc:
        _log.warning("llm_judge: %s/%s failed: %s", provider, effort, exc)
        return JudgeResult(
            answer="",
            answer_quality="wrong",
            context_sufficiency="incomplete",
            provider=provider,
            model=model,
            effort=effort,
            input_tokens=_estimate_tokens(user_content),
            output_tokens=0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=str(exc),
        )

    text = response.text
    parsed = _parse_verdict(text)
    latency_ms = int((time.monotonic() - t0) * 1000)
    return JudgeResult(
        answer=text,
        answer_quality=parsed["answer_quality"],  # type: ignore[arg-type]
        context_sufficiency=parsed["context_sufficiency"],  # type: ignore[arg-type]
        provider=provider,
        model=response.model or model,
        effort=effort,
        input_tokens=_estimate_tokens(user_content),
        output_tokens=_estimate_tokens(text),
        verdict=parsed["verdict"],
        correctness=parsed["correctness"],
        grounding=parsed["grounding"],
        completeness=parsed["completeness"],
        context_sufficient=parsed["context_sufficient"],
        unsupported_claims=parsed["unsupported_claims"],
        missing_evidence=parsed["missing_evidence"],
        notes=parsed["notes"],
        latency_ms=latency_ms,
    )


def judge_question_matrix(
    system_prompt: str,
    question: str,
    *,
    intent: str = "",
    efforts: tuple[Effort, ...] | None = None,
    providers: tuple[Provider, ...] | None = None,
    max_workers: int = 6,
) -> dict[str, JudgeResult | dict]:
    """Run claude+codex judges for each effort tier in parallel.

    Returns:
        {
          "matrix": { "low": {"claude": JudgeResult, "codex": ...}, ... },
          "available": {"claude": bool, "codex": bool},
        }
    """
    efforts = efforts or EFFORTS
    providers = providers or PROVIDERS
    available = bridges_available()

    matrix: dict[str, dict[str, JudgeResult]] = {effort: {} for effort in efforts}

    jobs: list[tuple[Provider, Effort]] = [
        (provider, effort)
        for effort in efforts
        for provider in providers
        if available.get(provider, False)
    ]

    if not jobs:
        return {"matrix": matrix, "available": available, "error": "no CLI bridges on PATH"}

    workers = min(max_workers, len(jobs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                judge_question_via_bridge,
                system_prompt,
                question,
                provider=provider,
                effort=effort,
                intent=intent,
            ): (provider, effort)
            for provider, effort in jobs
        }
        for future in as_completed(futures):
            provider, effort = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                _log.warning("llm_judge: worker %s/%s: %s", provider, effort, exc)
                result = JudgeResult(
                    answer="",
                    answer_quality="wrong",
                    context_sufficiency="incomplete",
                    provider=provider,
                    model=tier_model(provider, effort),
                    effort=effort,
                    input_tokens=0,
                    output_tokens=0,
                    error=str(exc),
                )
            matrix[effort][provider] = result

    for effort in efforts:
        for provider in providers:
            if provider not in matrix[effort]:
                matrix[effort][provider] = JudgeResult(
                    answer="",
                    answer_quality="wrong",
                    context_sufficiency="incomplete",
                    provider=provider,
                    model=tier_model(provider, effort),
                    effort=effort,
                    input_tokens=0,
                    output_tokens=0,
                    error=f"{provider} CLI not available on PATH",
                )

    return {"matrix": matrix, "available": available}


def judge_question(
    system_prompt: str,
    question: str,
    *,
    intent: str = "",
    effort: Effort = "medium",
) -> JudgeResult | None:
    """Backward-compatible single judge (claude bridge, one tier)."""
    if not bridges_available().get("claude"):
        return None
    return judge_question_via_bridge(
        system_prompt, question, provider="claude", effort=effort, intent=intent
    )
