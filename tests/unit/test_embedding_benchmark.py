"""Unit tests for the embedding model benchmark harness."""

import pytest

from QA.embedding_benchmark import (
    QuestionRecord,
    SymbolRecord,
    collect_symbols,
    evaluate_model,
    load_questions,
)


class BagOfWordsEncoder:
    vocabulary = ("payment", "validate", "amount", "currency", "symbol")

    def encode(self, texts, show_progress_bar=False):
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append([float(lowered.count(term)) for term in self.vocabulary])
        return vectors


def test_evaluate_model_scores_target_hits_with_fake_encoder():
    symbols = [
        SymbolRecord(
            uid="process_payment",
            name="process_payment",
            kind="function",
            file_path="payments/processor.py",
            start_line=1,
            end_line=3,
            code="def process_payment():\n    validate_amount(amount)",
        ),
        SymbolRecord(
            uid="format_amount",
            name="format_amount",
            kind="function",
            file_path="payments/utils.py",
            start_line=1,
            end_line=2,
            code="def format_amount():\n    return '$ currency symbol'",
        ),
    ]
    questions = [
        QuestionRecord(
            id="q1",
            symbol="process_payment",
            question="How does payment validate amount?",
            expected_symbols=("process_payment",),
        ),
        QuestionRecord(
            id="q2",
            symbol="format_amount",
            question="What currency symbol is used?",
            expected_symbols=("format_amount",),
        ),
    ]

    result = evaluate_model(
        "fake-bow",
        symbols,
        questions,
        top_k=1,
        encoder_factory=lambda _model: BagOfWordsEncoder(),
    )

    assert result["status"] == "ok"
    assert result["summary"]["target_hit_rate_at_k"] == pytest.approx(1.0)
    assert result["summary"]["mrr"] == pytest.approx(1.0)
    assert result["questions"][0]["top_symbols"][0]["name"] == "process_payment"
    assert result["questions"][1]["top_symbols"][0]["name"] == "format_amount"


def test_evaluate_model_reports_unavailable_model():
    symbols = [
        SymbolRecord(
            uid="process_payment",
            name="process_payment",
            kind="function",
            file_path="payments/processor.py",
            start_line=1,
            end_line=3,
            code="def process_payment(): pass",
        )
    ]
    questions = [
        QuestionRecord(
            id="q1",
            symbol="process_payment",
            question="How does payment work?",
            expected_symbols=("process_payment",),
        )
    ]

    result = evaluate_model(
        "missing-model",
        symbols,
        questions,
        top_k=1,
        encoder_factory=lambda _model: (_ for _ in ()).throw(RuntimeError("not cached")),
    )

    assert result["status"] == "unavailable"
    assert "not cached" in result["error"]


def test_collect_symbols_reads_python_files(tmp_path):
    module = tmp_path / "payments" / "processor.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "def process_payment():\n    validate_amount(1)\n\ndef validate_amount(x):\n    return x\n",
        encoding="utf-8",
    )
    symbols = collect_symbols(tmp_path)
    names = {symbol.name for symbol in symbols}

    assert {"process_payment", "validate_amount"} <= names
    assert all(symbol.code for symbol in symbols)


def test_load_questions_reads_yaml_list(tmp_path):
    questions_path = tmp_path / "questions.yaml"
    questions_path.write_text(
        """
- id: q001
  symbol: process_payment
  question: "How does payment validation work?"
  expected_symbols: [process_payment, validate_amount]
  difficulty: easy
  intent: trace_dependency
""".strip(),
        encoding="utf-8",
    )
    questions = load_questions(questions_path)

    assert len(questions) == 1
    assert questions[0].id == "q001"
    assert "process_payment" in questions[0].expected_symbols
