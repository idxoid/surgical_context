"""Integration tests for AIEngine with prepared contexts (cold run)."""

import os
from unittest.mock import MagicMock, patch

import pytest

from sidecar.ai.engine import AIEngine
from sidecar.context.types import DocChunk, PromptContext, SymbolContext


@pytest.fixture
def sample_prompt_context():
    """Prepared PromptContext for cold run testing."""
    primary = SymbolContext(
        symbol="process_payment",
        file_path="src/payment.py",
        relation="PRIMARY",
        direction="self",
        depth=0,
        relevance_score=1.0,
        is_dirty=False,
        code="""def process_payment(order_id, amount):
    '''Process payment for an order.'''
    if not validate_amount(amount):
        raise PaymentError("Invalid amount")

    fee = calculate_fee(amount)
    total = amount + fee

    save_payment(order_id, total)
    log_transaction(order_id, total)
    return {"status": "success", "total": total}
""",
    )

    graph = [
        SymbolContext(
            symbol="validate_amount",
            file_path="src/payment.py",
            relation="CALLS",
            direction="callee",
            depth=1,
            relevance_score=0.9,
            is_dirty=False,
            code="""def validate_amount(amount):
    MIN = 100
    MAX = 1000000
    return MIN <= amount <= MAX
""",
        ),
        SymbolContext(
            symbol="calculate_fee",
            file_path="src/payment.py",
            relation="CALLS",
            direction="callee",
            depth=1,
            relevance_score=0.85,
            is_dirty=False,
            code="""def calculate_fee(amount):
    FEE_PERCENT = 0.03
    return amount * FEE_PERCENT
""",
        ),
    ]

    docs = [
        DocChunk(
            source_file="docs/spec_payment.md",
            chunk_id="spec_1",
            content="Payment processing must validate amounts between 100 and 1,000,000 units.",
        ),
        DocChunk(
            source_file="docs/concept.md",
            chunk_id="concept_1",
            content="Payments are processed synchronously and logged immediately.",
        ),
    ]

    return PromptContext(
        primary_source=primary,
        graph_context=graph,
        documentation=docs,
        budget={"tokens_allocated": 4000, "tokens_remaining": 2500},
        mode="surgical_full",
        intent="debugging",
    )


@pytest.fixture
def sample_system_prompt(sample_prompt_context):
    """Generated system prompt from context."""
    return f"""You are a Surgical Code Assistant. Use ONLY the provided context.

{sample_prompt_context.to_system_prompt()}"""


@pytest.fixture
def sample_questions():
    """Sample questions for different intents."""
    return {
        "navigation": "Where is the payment processing function defined?",
        "debugging": "Why does payment validation fail on amounts less than 100?",
        "refactor": "Rename 'calculate_fee' to 'compute_transaction_fee' everywhere",
        "exploration": "How does the payment processing flow work?",
        "new_feature": "How do I add support for multiple currencies?",
        "design_question": "What pattern should we use for fee calculation?",
    }


class TestAIEngineColdRun:
    """Test AIEngine with prepared contexts (no live services)."""

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @patch("sidecar.ai.engine.Anthropic")
    def test_chat_with_claude_prepared_context(
        self, mock_anthropic_class, sample_system_prompt, sample_questions
    ):
        """Test Claude chat with prepared context."""
        # Mock Claude response
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="Claude analysis of the payment function...")]
        mock_client.messages.create.return_value = mock_message

        engine = AIEngine(model_preference="claude")
        answer = engine.chat(
            system_prompt=sample_system_prompt,
            user_message=sample_questions["debugging"],
            token_count=2500,
            intent="debugging",
        )

        assert "analysis" in answer.lower() or "payment" in answer.lower()
        mock_client.messages.create.assert_called_once()

    @patch("sidecar.ai.engine.ollama")
    def test_chat_with_ollama_prepared_context(
        self, mock_ollama, sample_system_prompt, sample_questions
    ):
        """Test Ollama chat with prepared context."""
        # Mock Ollama response
        mock_ollama.chat.return_value = {
            "message": {"content": "Ollama analysis of the payment function..."}
        }

        engine = AIEngine(model_preference="ollama")
        answer = engine.chat(
            system_prompt=sample_system_prompt,
            user_message=sample_questions["navigation"],
            token_count=500,
            intent="navigation",
        )

        assert "analysis" in answer.lower() or "payment" in answer.lower()
        mock_ollama.chat.assert_called_once()

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @patch("sidecar.ai.engine.Anthropic")
    @patch("sidecar.ai.engine.ollama")
    def test_auto_routing_large_context_uses_claude(
        self, mock_ollama, mock_anthropic_class, sample_system_prompt, sample_questions
    ):
        """Auto mode: large context (2500 tokens) → Claude."""
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="Claude response")]
        mock_client.messages.create.return_value = mock_message

        engine = AIEngine(model_preference="auto")
        answer = engine.chat(
            system_prompt=sample_system_prompt,
            user_message=sample_questions["debugging"],
            token_count=2500,
            intent="debugging",
        )

        # Should call Claude, not Ollama
        mock_client.messages.create.assert_called_once()
        mock_ollama.chat.assert_not_called()

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @patch("sidecar.ai.engine.Anthropic")
    @patch("sidecar.ai.engine.ollama")
    def test_auto_routing_small_simple_uses_ollama(
        self, mock_ollama, mock_anthropic_class, sample_system_prompt, sample_questions
    ):
        """Auto mode: small simple query (500 tokens, navigation) → Ollama."""
        mock_ollama.chat.return_value = {"message": {"content": "Ollama response"}}

        engine = AIEngine(model_preference="auto")
        answer = engine.chat(
            system_prompt=sample_system_prompt,
            user_message=sample_questions["navigation"],
            token_count=500,
            intent="navigation",
        )

        # Should call Ollama, not Claude
        mock_ollama.chat.assert_called_once()
        mock_anthropic_class.return_value.messages.create.assert_not_called()

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @patch("sidecar.ai.engine.Anthropic")
    @patch("sidecar.ai.engine.ollama")
    def test_auto_routing_design_question_uses_claude(
        self, mock_ollama, mock_anthropic_class, sample_system_prompt, sample_questions
    ):
        """Auto mode: complex intent (design_question) → Claude even if small."""
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="Claude design analysis")]
        mock_client.messages.create.return_value = mock_message

        engine = AIEngine(model_preference="auto")
        answer = engine.chat(
            system_prompt=sample_system_prompt,
            user_message=sample_questions["design_question"],
            token_count=500,  # Small context
            intent="design_question",  # But complex intent
        )

        # Should route to Claude because of complex intent
        mock_client.messages.create.assert_called_once()
        mock_ollama.chat.assert_not_called()

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @patch("sidecar.ai.engine.Anthropic")
    @patch("sidecar.ai.engine.ollama")
    def test_claude_fallback_to_ollama_on_error(
        self, mock_ollama, mock_anthropic_class, sample_system_prompt, sample_questions
    ):
        """Claude fails → fallback to Ollama."""
        # Claude raises error
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.side_effect = Exception("Claude API error")

        # Ollama succeeds
        mock_ollama.chat.return_value = {"message": {"content": "Fallback Ollama response"}}

        engine = AIEngine(model_preference="claude")
        answer = engine.chat(
            system_prompt=sample_system_prompt,
            user_message=sample_questions["debugging"],
            token_count=2500,
            intent="debugging",
        )

        # Should have tried Claude, then fallen back to Ollama
        mock_client.messages.create.assert_called_once()
        mock_ollama.chat.assert_called_once()
        assert "fallback" in answer.lower() or "ollama" in answer.lower()

    @patch("sidecar.ai.engine.ollama")
    def test_stream_chat_with_ollama(self, mock_ollama, sample_system_prompt, sample_questions):
        """Test streaming with Ollama."""
        # Mock streaming response
        mock_ollama.chat.return_value = [
            {"message": {"content": "First "}},
            {"message": {"content": "chunk "}},
            {"message": {"content": "of response"}},
        ]

        engine = AIEngine(model_preference="ollama")
        chunks = list(
            engine.stream_chat(
                system_prompt=sample_system_prompt,
                user_message=sample_questions["debugging"],
                token_count=500,
                intent="debugging",
            )
        )

        assert len(chunks) == 3
        assert "".join(chunks) == "First chunk of response"

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @patch("sidecar.ai.engine.Anthropic")
    def test_stream_chat_with_claude(
        self, mock_anthropic_class, sample_system_prompt, sample_questions
    ):
        """Test streaming with Claude."""
        # Mock Claude streaming
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        # Mock stream context manager
        mock_stream = MagicMock()
        mock_stream.text_stream = ["Stream ", "of ", "Claude ", "response"]
        mock_stream.__enter__ = MagicMock(return_value=mock_stream)
        mock_stream.__exit__ = MagicMock(return_value=None)
        mock_client.messages.stream.return_value = mock_stream

        engine = AIEngine(model_preference="claude")
        chunks = list(
            engine.stream_chat(
                system_prompt=sample_system_prompt,
                user_message=sample_questions["debugging"],
                token_count=2500,
                intent="debugging",
            )
        )

        assert len(chunks) == 4
        assert "".join(chunks) == "Stream of Claude response"

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @patch("sidecar.ai.engine.Anthropic")
    def test_prompt_caching_enabled_on_graph_context(
        self, mock_anthropic_class, sample_system_prompt, sample_questions
    ):
        """Verify prompt caching is enabled when graph context present."""
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="Response")]
        mock_client.messages.create.return_value = mock_message

        engine = AIEngine(model_preference="claude")
        answer = engine.chat(
            system_prompt=sample_system_prompt,  # Contains "--- DEPENDENCIES ---"
            user_message="What does process_payment do?",
            token_count=2500,
            intent="exploration",
        )

        # Check that messages.create was called with cache_control
        call_args = mock_client.messages.create.call_args
        system_arg = call_args.kwargs.get("system")
        assert system_arg is not None

        # At least one system block should have cache_control
        if isinstance(system_arg, list):
            has_cache_control = any(
                isinstance(block, dict)
                and "cache_control" in block
                and block["cache_control"] is not None
                for block in system_arg
            )
            assert has_cache_control, "Prompt caching should be enabled on graph_context"
