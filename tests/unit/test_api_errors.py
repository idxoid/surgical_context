from sidecar.api.errors import PUBLIC_INTERNAL_ERROR, degraded_llm_answer


def test_degraded_llm_answer_is_generic():
    text = degraded_llm_answer()
    assert "Error:" not in text
    assert "unreachable" in text.lower()


def test_public_internal_error_is_stable():
    assert PUBLIC_INTERNAL_ERROR == "An internal error occurred"
