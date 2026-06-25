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

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

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
You are a strict code-review judge for the Surgical Context axis pipeline.
You will receive an axis-ready context packet and a question.

Rules:
- Answer the question using ONLY the supplied context packet. Do not use outside knowledge.
- Treat the packet as the production answer contract: evidence may come from
  rendered code, symbols, graph neighbors, role/echelon metadata, and budget notes.
- Cite the concrete files/symbols or short evidence snippets that support the answer.
- If the packet lacks a required edge, caller, callee, role, or source excerpt, say so
  instead of filling the gap from memory.
- Return exactly one JSON object and no surrounding prose:
  {
    "answer": "<answer grounded only in the packet>",
    "verdict": "pass|warn|fail",
    "correctness": "correct|partial|wrong",
    "grounding": "grounded|mixed|ungrounded",
    "completeness": "complete|partial|insufficient",
    "context_sufficient": "yes|no",
    "context_sufficiency": "sufficient|overfed|incomplete",
    "citations": [{"file_path": "<path>", "symbol": "<symbol or empty>", "quote": "<short quote or paraphrase>"}],
    "evidence_roles_covered": ["<role names from the packet, if present>"],
    "unsupported_claims": "none|short phrase",
    "missing_evidence": "none|short phrase",
    "notes": "none|short phrase"
  }

Definitions:
  pass/warn/fail — overall judgment: pass=answerable from context; warn=partial; fail=wrong/ungrounded
  grounded — all claims trace to context; mixed — some inference; ungrounded — outside knowledge or hallucination
  complete/partial/insufficient — answer coverage relative to the question
  context_sufficient=yes means the packet had what was needed.
  context_sufficiency=sufficient means enough and reasonably focused; overfed means enough but noisy; incomplete means retrieval gap.
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
    citations: list[dict[str, str]] = field(default_factory=list)
    evidence_roles_covered: list[str] = field(default_factory=list)
    raw_text: str = ""
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
    pattern = re.compile(
        rf"^\s*(?:[-+]\s+|\*\s+)?[`*_]*\s*{re.escape(key)}\s*[`*_]*\s*:\s*(.*)$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip().strip("`*_").strip()
    return ""


def _derive_verdict(aq: AnswerQuality, cs: ContextSufficiency) -> str:
    if aq == "wrong":
        return "fail"
    if aq == "partial" or cs == "incomplete":
        return "warn"
    if aq == "correct":
        return "pass"
    return "fail"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    stripped = text.strip()
    if not stripped:
        return None

    for candidate in (stripped, *_json_fence_candidates(stripped)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    for match in re.finditer(r"\{", text):
        try:
            parsed, _end = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _json_fence_candidates(text: str) -> list[str]:
    """Extract markdown fenced-block bodies in O(n) time (no backtracking regex)."""
    candidates: list[str] = []
    marker = "```"
    marker_len = len(marker)
    pos = 0
    text_len = len(text)

    while pos < text_len:
        start = text.find(marker, pos)
        if start == -1:
            break

        content_start = start + marker_len
        if content_start + 4 <= text_len and text[content_start : content_start + 4].casefold() == "json":
            content_start += 4

        while content_start < text_len and text[content_start] in " \t\r\n":
            content_start += 1

        close = text.find(marker, content_start)
        if close == -1:
            break

        candidates.append(text[content_start:close].strip())
        pos = close + marker_len

    return candidates


def _normalise(value: Any, allowed: tuple[str, ...], default: str) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in allowed else default


def _string_or_none(value: Any, default: str = "none") -> str:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    text = str(value).strip()
    return text or default


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip() and value.strip().lower() != "none":
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _citation_list(value: Any) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    if not isinstance(value, list):
        return citations
    for item in value:
        if isinstance(item, dict):
            citations.append(
                {
                    "file_path": str(
                        item.get("file_path") or item.get("path") or item.get("file") or ""
                    ).strip(),
                    "symbol": str(item.get("symbol") or "").strip(),
                    "quote": str(
                        item.get("quote") or item.get("evidence") or item.get("snippet") or ""
                    ).strip(),
                }
            )
        elif str(item).strip():
            citations.append({"file_path": "", "symbol": "", "quote": str(item).strip()})
    return citations


def _parse_json_verdict(parsed: dict[str, Any]) -> dict[str, Any]:
    aq = _normalise(
        parsed.get("correctness") or parsed.get("answer_quality"),
        (
            "correct",
            "partial",
            "wrong",
        ),
        "wrong",
    )
    ctx_sufficient_raw = parsed.get("context_sufficient")
    cs = _normalise(
        parsed.get("context_sufficiency") or parsed.get("context_efficiency"),
        ("sufficient", "overfed", "incomplete"),
        "",
    )
    if not cs:
        if isinstance(ctx_sufficient_raw, bool):
            cs = "sufficient" if ctx_sufficient_raw else "incomplete"
        else:
            ctx_text = str(ctx_sufficient_raw or "").strip().lower()
            cs = "sufficient" if ctx_text == "yes" else "incomplete"
    if isinstance(ctx_sufficient_raw, bool):
        context_sufficient = "yes" if ctx_sufficient_raw else "no"
    else:
        context_sufficient = _normalise(ctx_sufficient_raw, ("yes", "no"), "")
        if not context_sufficient:
            context_sufficient = "yes" if cs in ("sufficient", "overfed") else "no"

    verdict = _normalise(parsed.get("verdict"), ("pass", "warn", "fail"), "")
    if not verdict:
        verdict = _derive_verdict(aq, cs)  # type: ignore[arg-type]

    return {
        "answer": _string_or_none(parsed.get("answer"), ""),
        "answer_quality": aq,
        "context_sufficiency": cs,
        "verdict": verdict,
        "correctness": aq,
        "grounding": _normalise(
            parsed.get("grounding"),
            ("grounded", "mixed", "ungrounded"),
            "grounded" if aq == "correct" else "mixed" if aq == "partial" else "ungrounded",
        ),
        "completeness": _normalise(
            parsed.get("completeness"),
            ("complete", "partial", "insufficient"),
            "complete" if aq == "correct" else "partial" if aq == "partial" else "insufficient",
        ),
        "context_sufficient": context_sufficient,
        "unsupported_claims": _string_or_none(parsed.get("unsupported_claims")),
        "missing_evidence": _string_or_none(
            parsed.get("missing_evidence"),
            "none" if cs != "incomplete" else "context gap",
        ),
        "notes": _string_or_none(parsed.get("notes"), ""),
        "citations": _citation_list(parsed.get("citations")),
        "evidence_roles_covered": _string_list(parsed.get("evidence_roles_covered")),
    }


def _parse_verdict(text: str) -> dict[str, Any]:
    parsed_json = _extract_json_object(text)
    if parsed_json is not None:
        return _parse_json_verdict(parsed_json)

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
        "answer": "",
        "verdict": verdict,
        "correctness": correctness,
        "grounding": grounding,
        "completeness": completeness,
        "context_sufficient": context_sufficient,
        "unsupported_claims": unsupported,
        "missing_evidence": missing,
        "notes": notes,
        "citations": [],
        "evidence_roles_covered": [],
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
    user_content = f"Axis context packet:\n{system_prompt}\n\n---\nQuestion: {question}"
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
        answer=parsed.get("answer") or text,
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
        citations=parsed["citations"],
        evidence_roles_covered=parsed["evidence_roles_covered"],
        raw_text=text,
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
) -> dict[str, JudgeResult | dict | str]:
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
