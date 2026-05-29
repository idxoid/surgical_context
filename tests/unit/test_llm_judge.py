from unittest.mock import MagicMock, patch

from QA.llm_judge import (
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


def test_tier_model_env_override(monkeypatch):
    monkeypatch.setenv("QA_JUDGE_CLAUDE_MODEL_HIGH", "custom-claude")
    assert tier_model("claude", "high") == "custom-claude"


@patch("QA.llm_judge.build_bridge_provider")
def test_judge_question_via_bridge_success(mock_build):
    bridge = MagicMock()
    bridge.complete.return_value = MagicMock(
        text="Done.\nANSWER_QUALITY: partial\nCONTEXT_SUFFICIENCY: sufficient\n",
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
