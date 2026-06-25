from unittest.mock import MagicMock, patch

from QA.llm_judge import (
    _json_fence_candidates,
    _parse_verdict,
    judge_question_matrix,
    judge_question_via_bridge,
    tier_model,
)


def test_parse_verdict_extracts_quality_lines():
    text = (
        "Some answer.\n"
        "VERDICT: pass\n"
        "CORRECTNESS: correct\n"
        "GROUNDING: grounded\n"
        "COMPLETENESS: complete\n"
        "CONTEXT_SUFFICIENT: yes\n"
        "UNSUPPORTED_CLAIMS: none\n"
        "MISSING_EVIDENCE: none\n"
        "ANSWER_QUALITY: correct\n"
        "CONTEXT_SUFFICIENCY: overfed\n"
    )
    parsed = _parse_verdict(text)
    assert parsed["answer_quality"] == "correct"
    assert parsed["context_sufficiency"] == "overfed"
    assert parsed["verdict"] == "pass"
    assert parsed["context_sufficient"] == "yes"


def test_parse_verdict_accepts_markdown_wrapped_lines():
    text = (
        "Some answer.\n"
        "- VERDICT: pass\n"
        "`CORRECTNESS`: partial\n"
        "* GROUNDING: mixed\n"
        "**COMPLETENESS**: partial\n"
        "- CONTEXT_SUFFICIENT: no\n"
        "`UNSUPPORTED_CLAIMS`: none\n"
        "* MISSING_EVIDENCE: missing call site\n"
        "- ANSWER_QUALITY: partial\n"
        "- CONTEXT_SUFFICIENCY: sufficient\n"
    )
    parsed = _parse_verdict(text)
    assert parsed["answer_quality"] == "partial"
    assert parsed["context_sufficiency"] == "sufficient"
    assert parsed["verdict"] == "pass"
    assert parsed["grounding"] == "mixed"
    assert parsed["completeness"] == "partial"
    assert parsed["context_sufficient"] == "no"
    assert parsed["missing_evidence"] == "missing call site"


def test_parse_verdict_prefers_structured_json():
    text = """
```json
{
  "answer": "X calls Y through the rendered bundle.",
  "verdict": "warn",
  "correctness": "partial",
  "grounding": "mixed",
  "completeness": "partial",
  "context_sufficient": "no",
  "context_sufficiency": "incomplete",
  "citations": [
    {"file_path": "pkg/x.py", "symbol": "X", "quote": "X calls Y"}
  ],
  "evidence_roles_covered": ["entrypoint", "callee"],
  "unsupported_claims": "none",
  "missing_evidence": "caller edge",
  "notes": "needs impact evidence"
}
```
"""
    parsed = _parse_verdict(text)
    assert parsed["answer"] == "X calls Y through the rendered bundle."
    assert parsed["answer_quality"] == "partial"
    assert parsed["context_sufficiency"] == "incomplete"
    assert parsed["verdict"] == "warn"
    assert parsed["citations"] == [{"file_path": "pkg/x.py", "symbol": "X", "quote": "X calls Y"}]
    assert parsed["evidence_roles_covered"] == ["entrypoint", "callee"]
    assert parsed["missing_evidence"] == "caller edge"


def test_parse_verdict_derives_legacy_context_sufficiency_from_json_flag():
    parsed = _parse_verdict('{"correctness": "correct", "context_sufficient": "yes"}')
    assert parsed["answer_quality"] == "correct"
    assert parsed["context_sufficiency"] == "sufficient"
    assert parsed["context_sufficient"] == "yes"


def test_json_fence_candidates_linear_scan():
    assert _json_fence_candidates('```json\n{"a": 1}\n```') == ['{"a": 1}']
    assert _json_fence_candidates('```\n{"b": 2}\n```') == ['{"b": 2}']
    assert _json_fence_candidates('```json{"c": 3}```') == ['{"c": 3}']

    assert _json_fence_candidates("```json\n" + ("x" * 50_000)) == []


def test_tier_model_env_override(monkeypatch):
    monkeypatch.setenv("QA_JUDGE_CLAUDE_MODEL_HIGH", "custom-claude")
    assert tier_model("claude", "high") == "custom-claude"


@patch("QA.llm_judge.build_bridge_provider")
def test_judge_question_via_bridge_success(mock_build):
    bridge = MagicMock()
    bridge.complete.return_value = MagicMock(
        text=(
            '{"answer": "Done.", "correctness": "partial", '
            '"context_sufficiency": "sufficient", "context_sufficient": "yes", '
            '"citations": [{"file_path": "a.py", "symbol": "A", "quote": "A()"}], '
            '"evidence_roles_covered": ["definition"]}'
        ),
        model="claude-sonnet-4-6",
    )
    mock_build.return_value = bridge

    result = judge_question_via_bridge(
        "context block",
        "What does X do?",
        provider="claude",
        effort="medium",
    )

    assert result.error is None
    assert result.answer_quality == "partial"
    assert result.context_sufficiency == "sufficient"
    assert result.answer == "Done."
    assert result.citations == [{"file_path": "a.py", "symbol": "A", "quote": "A()"}]
    assert result.evidence_roles_covered == ["definition"]
    assert result.raw_text
    assert result.provider == "claude"
    assert result.effort == "medium"


@patch("QA.llm_judge.bridges_available", return_value={"claude": True, "codex": True})
@patch("QA.llm_judge.judge_question_via_bridge")
def test_judge_question_matrix_runs_all_cells(mock_single, _mock_avail):
    mock_single.side_effect = lambda *a, provider, effort, **kw: MagicMock(
        answer_quality="correct",
        context_sufficiency="sufficient",
        provider=provider,
        effort=effort,
        model=f"{provider}-{effort}",
        answer="ok",
        input_tokens=10,
        output_tokens=5,
        latency_ms=1,
        error=None,
        to_dict=lambda: {
            "provider": provider,
            "effort": effort,
            "answer_quality": "correct",
            "context_sufficiency": "sufficient",
        },
    )

    out = judge_question_matrix("ctx", "question?", max_workers=6)
    matrix = out["matrix"]
    assert set(matrix.keys()) == {"low", "medium", "high"}
    assert set(matrix["low"].keys()) == {"claude", "codex"}
    assert mock_single.call_count == 6
