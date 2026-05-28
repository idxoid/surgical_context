#!/usr/bin/env python3
"""Context frontier and evidence-citation gate for QA benchmark reports.

The script consumes reports produced by ``QA/qa_benchmark.py``. It does not
re-index repositories. It rebuilds smaller prompt variants from
``ready_context.contract`` and can optionally ask one LLM bridge to answer with
citations, then reports the smallest passing context found by the configured
budget curve and optional greedy pruning.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import tiktoken
except Exception:  # pragma: no cover - dependency is present in normal runs.
    tiktoken = None

try:
    from QA.llm_bridge import BridgeRequest, build_bridge_provider
    from QA.llm_judge import Effort, Provider, tier_model
    from QA.qa_benchmark import _expected_file_matches
    from sidecar.context.role_taxonomy import normalize_roles
except ImportError:  # pragma: no cover - direct script invocation fallback.
    from llm_bridge import BridgeRequest, build_bridge_provider
    from llm_judge import Effort, Provider, tier_model
    from qa_benchmark import _expected_file_matches
    from sidecar.context.role_taxonomy import normalize_roles


_ENCODER = None


@dataclass(frozen=True)
class ContextUnit:
    unit_id: str
    kind: str
    label: str
    text: str
    token_count: int
    file_path: str = ""
    symbol: str = ""
    relation: str = ""
    depth: int = 0
    score: float = 0.0


@dataclass(frozen=True)
class Variant:
    variant_id: str
    token_budget: int | None
    units: tuple[ContextUnit, ...]
    text: str
    token_count: int
    strategy: str

    @property
    def unit_ids(self) -> list[str]:
        return [unit.unit_id for unit in self.units]


@dataclass
class GateResult:
    verdict: str
    correctness: str
    grounding: str
    completeness: str
    context_sufficient: str
    gate_pass: bool
    gate_reasons: list[str]
    citations: list[dict[str, Any]]
    evidence_roles_covered: list[str]
    unsupported_claims: str = "none"
    missing_evidence: str = "none"
    answer: str = ""
    raw_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    error: str | None = None


def estimate_tokens(text: str) -> int:
    global _ENCODER
    if tiktoken is None:
        return max(1, len(text) // 4)
    if _ENCODER is None:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return len(_ENCODER.encode(text or ""))


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").strip("\n")


def _unit_id(kind: str, index: int, item: dict[str, Any]) -> str:
    label = item.get("symbol") or item.get("chunk_id") or item.get("source_file") or kind
    label = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(label)).strip("_")[:80]
    return f"{kind}:{index}:{label or 'unit'}"


def _score(item: dict[str, Any]) -> float:
    scores = item.get("scores") or {}
    for key in ("blended_score", "relevance", "relevance_score"):
        value = scores.get(key, item.get(key))
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _render_primary(item: dict[str, Any]) -> str:
    symbol = item.get("symbol") or "target"
    code = _clean_text(item.get("code"))
    path = item.get("file_path") or ""
    return f"--- TARGET SYMBOL: {symbol} ---\n# file: {path}\n{code}\n"


def _render_graph(item: dict[str, Any]) -> str:
    symbol = item.get("symbol") or "dependency"
    relation = item.get("relation") or "dependency"
    depth = int(item.get("depth") or 0)
    score = _score(item)
    path = item.get("file_path") or ""
    code = _clean_text(item.get("code"))
    return (
        f"# {symbol} [{relation}, depth={depth}, score={score:.2f}]\n"
        f"# file: {path}\n"
        f"{code}\n"
    )


def _render_doc(item: dict[str, Any]) -> str:
    source_file = item.get("source_file") or ""
    chunk_id = item.get("chunk_id") or source_file or "doc"
    anchor_type = item.get("anchor_type") or (item.get("anchor") or {}).get("type") or "doc"
    score = _score(item)
    content = _clean_text(item.get("content"))
    return (
        f"# DOC {chunk_id} [{anchor_type}, score={score:.2f}]\n"
        f"# file: {source_file}\n"
        f"{content}\n"
    )


def _make_unit(kind: str, index: int, item: dict[str, Any]) -> ContextUnit:
    if kind == "primary":
        text = _render_primary(item)
    elif kind == "doc":
        text = _render_doc(item)
    else:
        text = _render_graph(item)
    return ContextUnit(
        unit_id=_unit_id(kind, index, item),
        kind=kind,
        label=str(item.get("symbol") or item.get("chunk_id") or item.get("source_file") or kind),
        text=text,
        token_count=estimate_tokens(text),
        file_path=str(item.get("file_path") or item.get("source_file") or ""),
        symbol=str(item.get("symbol") or ""),
        relation=str(item.get("relation") or item.get("anchor_type") or ""),
        depth=int(item.get("depth") or 0),
        score=_score(item),
    )


def units_from_result(result: dict[str, Any]) -> list[ContextUnit]:
    ready = result.get("ready_context") or {}
    contract = ready.get("contract") or {}
    units: list[ContextUnit] = []
    primary = contract.get("primary_source")
    if isinstance(primary, dict):
        units.append(_make_unit("primary", 0, primary))
    for index, item in enumerate(contract.get("graph_context") or [], start=1):
        if isinstance(item, dict):
            units.append(_make_unit("graph", index, item))
    for index, item in enumerate(contract.get("documentation") or [], start=1):
        if isinstance(item, dict):
            units.append(_make_unit("doc", index, item))
    return units


def render_variant_text(
    units: Iterable[ContextUnit],
    *,
    question_id: str,
    strategy: str,
) -> str:
    grouped = list(units)
    primary = [unit.text for unit in grouped if unit.kind == "primary"]
    graph = [unit.text for unit in grouped if unit.kind == "graph"]
    docs = [unit.text for unit in grouped if unit.kind == "doc"]
    parts = [
        f"# Context variant: {question_id} / {strategy}",
        "Use only this context. Cite file paths and symbols for material claims.",
        "",
        *primary,
    ]
    if graph:
        parts.extend(["--- DEPENDENCIES ---", "", *graph])
    if docs:
        parts.extend(["--- DOCUMENTATION ---", "", *docs])
    return "\n".join(parts).strip() + "\n"


def _variant(
    question_id: str,
    variant_id: str,
    budget: int | None,
    units: list[ContextUnit],
    strategy: str,
) -> Variant:
    text = render_variant_text(units, question_id=question_id, strategy=strategy)
    return Variant(
        variant_id=variant_id,
        token_budget=budget,
        units=tuple(units),
        text=text,
        token_count=estimate_tokens(text),
        strategy=strategy,
    )


def _unit_selection_rank(
    unit: ContextUnit,
    *,
    expected_symbols: set[str],
) -> tuple[int, int, int, float, int]:
    is_mandatory = (unit.relation or "").upper() == "MANDATORY_CALLEE"
    expected_hit = unit.symbol in expected_symbols if expected_symbols and unit.symbol else False
    return (
        0 if is_mandatory else 1,
        0 if expected_hit else 1,
        -unit.score,
        unit.depth,
        unit.token_count,
    )


def select_units_under_budget(
    units: list[ContextUnit],
    token_budget: int,
    *,
    expected_symbols: Iterable[str] | None = None,
) -> list[ContextUnit]:
    if not units:
        return []
    expected = {str(symbol) for symbol in (expected_symbols or []) if symbol}
    selected = [unit for unit in units if unit.kind == "primary"][:1]
    spent = sum(unit.token_count for unit in selected) + 80
    rest = sorted(
        [unit for unit in units if unit.kind != "primary"],
        key=lambda unit: _unit_selection_rank(unit, expected_symbols=expected),
    )
    for unit in rest:
        if spent + unit.token_count > token_budget:
            continue
        selected.append(unit)
        spent += unit.token_count
    return selected


def build_budget_variants(
    result: dict[str, Any],
    budgets: list[int],
) -> list[Variant]:
    question_id = str(result.get("id") or "question")
    units = units_from_result(result)
    expected_symbols = result.get("expected_symbols") or []
    variants: list[Variant] = []
    seen: set[tuple[str, ...]] = set()
    for budget in budgets:
        selected = select_units_under_budget(
            units, budget, expected_symbols=expected_symbols
        )
        key = tuple(unit.unit_id for unit in selected)
        if key in seen:
            continue
        seen.add(key)
        variants.append(
            _variant(question_id, f"budget:{budget}", budget, selected, "budget_curve")
        )
    variants.append(_variant(question_id, "full", None, units, "full_context"))
    return sorted(variants, key=lambda item: (item.token_count, item.variant_id != "full"))


def _json_from_text(text: str) -> dict[str, Any] | None:
    stripped = (text or "").strip()
    candidates = [stripped]
    for pattern in (r"```json\s*(.*?)\s*```", r"```\s*(\{.*?\})\s*```"):
        match = re.search(pattern, stripped, re.DOTALL)
        if match:
            candidates.append(match.group(1).strip())
    if "{" in stripped and "}" in stripped:
        candidates.append(stripped[stripped.find("{") : stripped.rfind("}") + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _context_files_symbols(units: Iterable[ContextUnit]) -> tuple[set[str], set[str], str]:
    files = {unit.file_path for unit in units if unit.file_path}
    symbols = {unit.symbol for unit in units if unit.symbol}
    text = "\n".join(unit.text for unit in units)
    return files, symbols, text


def _normalize_path(path: str) -> str:
    return path.strip().strip("/").replace("\\", "/")


def _file_path_matches(hint: str, paths: set[str]) -> bool:
    """True when ``hint`` refers to the same file as any path in ``paths``.

    Judges often cite repo-relative paths (``src/click/core.py``) while context
    units carry absolute benchmark paths (``.../QA/repos/click/src/click/core.py``).
    Uses the same suffix / directory rules as ``QA.qa_benchmark._expected_file_matches``
    in both directions so either side may be relative or absolute.
    """
    hint_norm = _normalize_path(hint)
    if not hint_norm or not paths:
        return False
    normalized = {_normalize_path(path) for path in paths if path}
    if hint_norm in normalized:
        return True
    if _expected_file_matches(hint_norm, normalized):
        return True
    for path in normalized:
        if _expected_file_matches(path, {hint_norm}):
            return True
        if path.endswith("/" + hint_norm) or hint_norm.endswith("/" + path):
            return True
    return False


def _matches_file_hint(expected: str, files: set[str]) -> bool:
    """Backward-compatible alias for pack-hint ↔ path-set matching."""
    return _file_path_matches(expected, files)


def _citation_matches_file(citation: dict[str, Any], context_files: set[str]) -> bool:
    cited_file = str(citation.get("file_path") or citation.get("file") or "").strip()
    if not cited_file:
        return False
    return _file_path_matches(cited_file, context_files)


def _symbol_name_matches(cited: str, context_symbols: set[str]) -> bool:
    """Match exact ids or qualified heads/tails (``Consumer.create``, ``Request.execute``)."""
    cited = cited.strip()
    if not cited or not context_symbols:
        return False
    if cited in context_symbols:
        return True
    if "." in cited:
        head, tail = cited.split(".", 1)
        if head in context_symbols or tail in context_symbols:
            return True
    return False


def _citation_matches_symbol(citation: dict[str, Any], context_symbols: set[str]) -> bool:
    cited_symbol = str(citation.get("symbol") or "").strip()
    return _symbol_name_matches(cited_symbol, context_symbols)


_QUOTE_FUZZY_MIN_LEN = 12
_QUOTE_FUZZY_MIN_RATIO = 0.82
_QUOTE_FUZZY_LINE_RATIO = 0.88


def _normalize_quote_text(text: str) -> str:
    """Collapse whitespace so judge copies match rendered context units."""
    cleaned = (text or "").replace("\\n", "\n").replace("\r\n", "\n")
    return re.sub(r"\s+", " ", cleaned.strip())


def _quote_matches_context(
    quote: str,
    context_text: str,
    *,
    min_ratio: float = _QUOTE_FUZZY_MIN_RATIO,
) -> bool:
    """True when ``quote`` is present in context, allowing minor judge drift."""
    if not quote:
        return True
    quote_norm = _normalize_quote_text(quote)
    if not quote_norm:
        return True
    context_norm = _normalize_quote_text(context_text)
    if quote_norm in context_norm:
        return True
    if len(quote_norm) < _QUOTE_FUZZY_MIN_LEN:
        return False

    if difflib.SequenceMatcher(None, quote_norm, context_norm).ratio() >= min_ratio:
        return True

    for line in context_text.splitlines():
        line_norm = _normalize_quote_text(line)
        if len(line_norm) < _QUOTE_FUZZY_MIN_LEN:
            continue
        if difflib.SequenceMatcher(None, quote_norm, line_norm).ratio() >= _QUOTE_FUZZY_LINE_RATIO:
            return True

    window = len(quote_norm)
    if window > len(context_norm):
        return False
    best = 0.0
    step = max(1, window // 8)
    for start in range(0, len(context_norm) - window + 1, step):
        chunk = context_norm[start : start + window]
        score = difflib.SequenceMatcher(None, quote_norm, chunk).ratio()
        if score > best:
            best = score
        if best >= min_ratio:
            return True
    return best >= min_ratio


def _evidence_role_token(item: str) -> str:
    """Extract canonical role id from judge free-text role entries."""
    text = item.strip()
    if not text:
        return ""
    text = text.split(":", 1)[0].strip()
    if "(" in text:
        text = text.split("(", 1)[0].strip()
    return text


def _evidence_roles_covered_set(raw: Any) -> set[str]:
    """Parse judge ``evidence_roles_covered`` into canonical role ids.

    Judges often return ``"core_runtime: ..."`` or ``"executor (Pool)"`` instead
    of bare ``executor``. Strip suffixes, then ``normalize_roles``.
    """
    if not isinstance(raw, list):
        return set()
    role_tokens: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        token = _evidence_role_token(item)
        if token:
            role_tokens.append(token)
    return set(normalize_roles(role_tokens))


def score_citation_gate(
    payload: dict[str, Any],
    variant: Variant,
    result: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    verdict = str(payload.get("verdict") or "").lower()
    correctness = str(payload.get("correctness") or "").lower()
    grounding = str(payload.get("grounding") or "").lower()
    completeness = str(payload.get("completeness") or "").lower()
    context_sufficient = str(payload.get("context_sufficient") or "").lower()
    if verdict != "pass":
        reasons.append(f"verdict={verdict or 'missing'}")
    if correctness != "correct":
        reasons.append(f"correctness={correctness or 'missing'}")
    if grounding != "grounded":
        reasons.append(f"grounding={grounding or 'missing'}")
    if completeness != "complete":
        reasons.append(f"completeness={completeness or 'missing'}")
    if context_sufficient != "yes":
        reasons.append(f"context_sufficient={context_sufficient or 'missing'}")

    citations = payload.get("citations") or []
    if not isinstance(citations, list) or not citations:
        reasons.append("citations=missing")
        citations = []

    context_files, context_symbols, context_text = _context_files_symbols(variant.units)
    valid_citations = 0
    expected_files = set(result.get("expected_files") or [])
    expected_symbols = set(result.get("expected_symbols") or [])
    cited_expected_files: set[str] = set()
    cited_expected_symbols: set[str] = set()
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        file_ok = _citation_matches_file(citation, context_files)
        symbol_ok = _citation_matches_symbol(citation, context_symbols)
        quote = str(citation.get("quote") or "").strip()
        quote_ok = _quote_matches_context(quote, context_text)
        if (file_ok or symbol_ok) and quote_ok:
            valid_citations += 1
        cited_file = str(citation.get("file_path") or citation.get("file") or "").strip()
        for expected in expected_files:
            if cited_file and _file_path_matches(expected, {cited_file}):
                cited_expected_files.add(expected)
        cited_symbol = str(citation.get("symbol") or "")
        for expected in expected_symbols:
            if _symbol_name_matches(cited_symbol, {expected}):
                cited_expected_symbols.add(expected)
                break
            if _symbol_name_matches(expected, {cited_symbol}):
                cited_expected_symbols.add(expected)
                break

    if citations and valid_citations == 0:
        reasons.append("citations_do_not_match_context")
    if expected_files and not cited_expected_files:
        reasons.append("expected_files_not_cited")
    if expected_symbols and not cited_expected_symbols:
        reasons.append("expected_symbols_not_cited")

    required_roles = set(
        normalize_roles(
            result.get("required_roles_canonical")
            or result.get("required_roles")
            or result.get("expected_roles")
            or []
        )
    )
    covered_roles = _evidence_roles_covered_set(payload.get("evidence_roles_covered"))
    if required_roles and not required_roles.issubset(covered_roles):
        missing = ",".join(sorted(required_roles - covered_roles))
        reasons.append(f"evidence_roles_missing={missing}")
    return not reasons, reasons


_GATE_SYSTEM = """\
You are a strict context-sufficiency judge for code QA.

Rules:
- Use only the supplied context. Do not use pretrained repository knowledge.
- Answer the question briefly.
- Every material claim must have a citation object.
- A citation must name the file_path and symbol when available, plus a short quote copied from the context.
- If the context is missing a mechanism step, say so and return warn or fail.
- Return JSON only, with this exact shape:
{
  "answer": "...",
  "verdict": "pass|warn|fail",
  "correctness": "correct|partial|wrong",
  "grounding": "grounded|mixed|ungrounded",
  "completeness": "complete|partial|insufficient",
  "context_sufficient": "yes|no",
  "citations": [
    {"claim": "...", "file_path": "...", "symbol": "...", "quote": "..."}
  ],
  "evidence_roles_covered": ["..."],
  "unsupported_claims": "none|...",
  "missing_evidence": "none|..."
}
"""


def judge_variant(
    variant: Variant,
    result: dict[str, Any],
    *,
    provider: Provider,
    effort: Effort,
) -> GateResult:
    bridge_name = "claude-code" if provider == "claude" else "codex"
    model = tier_model(provider, effort)
    question = result.get("question") or ""
    prompt = (
        f"Question id: {result.get('id')}\n"
        f"Intent: {result.get('intent', '')}\n"
        f"Required evidence roles: {result.get('required_roles_canonical') or result.get('expected_roles') or []}\n"
        f"Expected symbols: {result.get('expected_symbols') or []}\n"
        f"Expected files: {result.get('expected_files') or []}\n\n"
        f"{variant.text}\n\n"
        f"Question: {question}\n"
    )
    input_tokens = estimate_tokens(_GATE_SYSTEM + "\n" + prompt)
    started = time.monotonic()
    try:
        response = build_bridge_provider(bridge_name).complete(
            BridgeRequest(system=_GATE_SYSTEM, prompt=prompt, model=model)
        )
    except Exception as exc:
        return GateResult(
            verdict="fail",
            correctness="wrong",
            grounding="ungrounded",
            completeness="insufficient",
            context_sufficient="no",
            gate_pass=False,
            gate_reasons=[f"bridge_error={exc}"],
            citations=[],
            evidence_roles_covered=[],
            input_tokens=input_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=str(exc),
        )

    raw = response.text
    payload = _json_from_text(raw) or {}
    gate_pass, reasons = score_citation_gate(payload, variant, result)
    return GateResult(
        verdict=str(payload.get("verdict") or "fail"),
        correctness=str(payload.get("correctness") or "wrong"),
        grounding=str(payload.get("grounding") or "ungrounded"),
        completeness=str(payload.get("completeness") or "insufficient"),
        context_sufficient=str(payload.get("context_sufficient") or "no"),
        gate_pass=gate_pass,
        gate_reasons=reasons,
        citations=payload.get("citations") if isinstance(payload.get("citations"), list) else [],
        evidence_roles_covered=payload.get("evidence_roles_covered")
        if isinstance(payload.get("evidence_roles_covered"), list)
        else [],
        unsupported_claims=str(payload.get("unsupported_claims") or "none"),
        missing_evidence=str(payload.get("missing_evidence") or "none"),
        answer=str(payload.get("answer") or ""),
        raw_text=raw,
        input_tokens=input_tokens,
        output_tokens=estimate_tokens(raw),
        latency_ms=int((time.monotonic() - started) * 1000),
    )


def _drop_order(units: tuple[ContextUnit, ...]) -> list[ContextUnit]:
    droppable = [unit for unit in units if unit.kind != "primary"]
    return sorted(droppable, key=lambda unit: (unit.score, -unit.depth, unit.token_count))


def _variant_without(base: Variant, unit: ContextUnit, question_id: str) -> Variant:
    units = [item for item in base.units if item.unit_id != unit.unit_id]
    return _variant(
        question_id,
        f"{base.variant_id}-drop:{unit.unit_id}",
        base.token_budget,
        units,
        "greedy_ablation",
    )


def run_question_frontier(
    result: dict[str, Any],
    *,
    budgets: list[int],
    run_judge: bool,
    provider: Provider,
    effort: Effort,
    greedy: bool,
    max_greedy_attempts: int,
) -> dict[str, Any]:
    variants = build_budget_variants(result, budgets)
    attempts: list[dict[str, Any]] = []
    passing: list[tuple[Variant, GateResult | None]] = []

    for variant in variants:
        gate = judge_variant(variant, result, provider=provider, effort=effort) if run_judge else None
        if gate and gate.gate_pass:
            passing.append((variant, gate))
        attempts.append(
            {
                "variant_id": variant.variant_id,
                "strategy": variant.strategy,
                "token_budget": variant.token_budget,
                "token_count": variant.token_count,
                "unit_count": len(variant.units),
                "unit_ids": variant.unit_ids,
                "gate": asdict(gate) if gate else {"verdict": "not_run"},
            }
        )
        if gate and gate.gate_pass:
            break

    if run_judge and greedy and passing:
        base_variant, base_gate = passing[0]
        current = base_variant
        current_gate = base_gate
        for index, unit in enumerate(_drop_order(current.units), start=1):
            if index > max_greedy_attempts:
                break
            candidate = _variant_without(current, unit, str(result.get("id") or "question"))
            gate = judge_variant(candidate, result, provider=provider, effort=effort)
            attempts.append(
                {
                    "variant_id": candidate.variant_id,
                    "strategy": candidate.strategy,
                    "token_budget": candidate.token_budget,
                    "token_count": candidate.token_count,
                    "unit_count": len(candidate.units),
                    "unit_ids": candidate.unit_ids,
                    "dropped_unit": unit.unit_id,
                    "gate": asdict(gate),
                }
            )
            if gate.gate_pass:
                current = candidate
                current_gate = gate
        passing.append((current, current_gate))

    if run_judge and passing:
        frontier_variant, frontier_gate = min(passing, key=lambda item: item[0].token_count)
        frontier = {
            "variant_id": frontier_variant.variant_id,
            "token_count": frontier_variant.token_count,
            "unit_count": len(frontier_variant.units),
            "unit_ids": frontier_variant.unit_ids,
            "gate": asdict(frontier_gate),
        }
    elif run_judge and attempts:
        best_failed = min(attempts, key=lambda item: int(item.get("token_count") or 0))
        frontier = {
            "variant_id": best_failed.get("variant_id", ""),
            "token_count": best_failed.get("token_count", 0),
            "unit_count": best_failed.get("unit_count", 0),
            "unit_ids": best_failed.get("unit_ids", []),
            "gate": best_failed.get("gate", {}),
            "status": "no_passing_context_found",
        }
    else:
        smallest = min(variants, key=lambda item: item.token_count) if variants else None
        frontier = {
            "variant_id": smallest.variant_id if smallest else "",
            "token_count": smallest.token_count if smallest else 0,
            "unit_count": len(smallest.units) if smallest else 0,
            "unit_ids": smallest.unit_ids if smallest else [],
            "gate": {"verdict": "not_run"},
            "status": "not_judged",
        }

    original_tokens = (result.get("ready_context") or {}).get("token_count") or result.get(
        "tokens_surgical", 0
    )
    judge_tokens = 0
    for attempt in attempts:
        gate = attempt.get("gate") or {}
        judge_tokens += int(gate.get("input_tokens") or 0) + int(gate.get("output_tokens") or 0)
    return {
        "question_id": result.get("id", ""),
        "repo": result.get("repo", ""),
        "intent": result.get("intent", ""),
        "question": result.get("question", ""),
        "original_tokens": original_tokens,
        "full_rebuilt_tokens": max((variant.token_count for variant in variants), default=0),
        "attempt_count": len(attempts),
        "judge_tokens": judge_tokens,
        "frontier": frontier,
        "attempts": attempts,
    }


def _load_reports(paths: list[str]) -> list[dict[str, Any]]:
    reports = []
    for path in paths:
        reports.append(json.loads(Path(path).read_text(encoding="utf-8")))
    return reports


def _iter_results(
    reports: list[dict[str, Any]],
    *,
    question_ids: set[str],
    repo: str,
    limit: int | None,
) -> Iterable[dict[str, Any]]:
    yielded = 0
    for report in reports:
        for result in report.get("results") or []:
            if not result.get("ready_context"):
                continue
            if question_ids and result.get("id") not in question_ids:
                continue
            if repo and result.get("repo") != repo:
                continue
            yield result
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def _parse_budgets(raw: str) -> list[int]:
    budgets = sorted({int(part.strip()) for part in raw.split(",") if part.strip()})
    if not budgets:
        raise ValueError("At least one budget is required")
    return budgets


def run_frontier(args: argparse.Namespace) -> dict[str, Any]:
    reports = _load_reports(args.reports)
    budgets = _parse_budgets(args.budgets)
    question_ids = set(args.question_id or [])
    rows = [
        run_question_frontier(
            result,
            budgets=budgets,
            run_judge=args.run_judge,
            provider=args.provider,
            effort=args.effort,
            greedy=args.greedy,
            max_greedy_attempts=args.max_greedy_attempts,
        )
        for result in _iter_results(
            reports,
            question_ids=question_ids,
            repo=args.repo or "",
            limit=args.limit,
        )
    ]
    return {
        "mode": "judge" if args.run_judge else "dry_run",
        "budgets": budgets,
        "provider": args.provider if args.run_judge else "",
        "effort": args.effort if args.run_judge else "",
        "greedy": bool(args.greedy),
        "summary": {
            "questions": len(rows),
            "judge_tokens": sum(row.get("judge_tokens", 0) for row in rows),
            "frontier_pass": sum(
                1
                for row in rows
                if (row.get("frontier") or {}).get("gate", {}).get("gate_pass") is True
            ),
        },
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find context budget/frontier candidates with optional evidence-citation judging."
    )
    parser.add_argument("reports", nargs="+", help="qa_benchmark JSON report paths")
    parser.add_argument(
        "--budgets",
        default="4000,8000,16000",
        help="Comma-separated answer-context budgets to test (default: 4000,8000,16000)",
    )
    parser.add_argument("--question-id", action="append", help="Run only this question id")
    parser.add_argument("--repo", default="", help="Run only this repo id")
    parser.add_argument("--limit", type=int, default=None, help="Max questions to process")
    parser.add_argument(
        "--run-judge",
        action="store_true",
        help="Call one LLM bridge for each frontier attempt. Omit for cheap dry-run.",
    )
    parser.add_argument("--provider", choices=["claude", "codex"], default="codex")
    parser.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    parser.add_argument(
        "--greedy",
        action="store_true",
        help="After the first passing budget, try greedy one-unit ablation.",
    )
    parser.add_argument(
        "--max-greedy-attempts",
        type=int,
        default=24,
        help="Max greedy ablation judge calls per question.",
    )
    parser.add_argument("-o", "--output", help="Write JSON report here")
    args = parser.parse_args()

    payload = run_frontier(args)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        path = Path(args.output).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        print(f"Frontier report: {path}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
