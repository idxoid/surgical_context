"""Unit tests for AIEngine with model routing."""

import os
import pytest
from unittest.mock import patch

from sidecar.ai.engine import AIEngine, ModelRouter


class TestModelRouter:
    """Test model routing logic."""

    def test_small_context_uses_ollama(self):
        """Small contexts (< 2k tokens) → Ollama."""
        result = ModelRouter.should_use_claude(token_count=1000, intent="navigation")
        assert result is False

    def test_large_context_uses_claude(self):
        """Large contexts (>= 2k tokens) → Claude."""
        result = ModelRouter.should_use_claude(token_count=2000, intent="debugging")
        assert result is True

    def test_very_large_context_uses_claude(self):
        """Very large contexts → Claude."""
        result = ModelRouter.should_use_claude(token_count=5000, intent="navigation")
        assert result is True

    def test_design_question_uses_claude(self):
        """Design questions → Claude (even if small)."""
        result = ModelRouter.should_use_claude(token_count=500, intent="design_question")
        assert result is True

    def test_exploration_uses_claude(self):
        """Exploration queries → Claude (even if small)."""
        result = ModelRouter.should_use_claude(token_count=1000, intent="exploration")
        assert result is True

    def test_refactor_uses_claude(self):
        """Refactoring queries → Claude (even if small)."""
        result = ModelRouter.should_use_claude(token_count=1500, intent="refactor")
        assert result is True

    def test_navigation_small_uses_ollama(self):
        """Small navigation queries → Ollama."""
        result = ModelRouter.should_use_claude(token_count=800, intent="navigation")
        assert result is False

    def test_debugging_small_uses_ollama(self):
        """Small debugging queries → Ollama."""
        result = ModelRouter.should_use_claude(token_count=1500, intent="debugging")
        assert result is False

    def test_new_feature_small_uses_ollama(self):
        """Small new_feature queries → Ollama."""
        result = ModelRouter.should_use_claude(token_count=1000, intent="new_feature")
        assert result is False


class TestAIEngineInitialization:
    """Test AIEngine initialization."""

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_init_claude_preference(self):
        """Initialize with Claude preference."""
        engine = AIEngine(model_preference="claude")
        assert engine.model_preference == "claude"
        assert engine.claude_model is not None

    def test_init_ollama_preference(self):
        """Initialize with Ollama preference."""
        engine = AIEngine(model_preference="ollama")
        assert engine.model_preference == "ollama"

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_init_auto_preference(self):
        """Initialize with auto preference."""
        engine = AIEngine(model_preference="auto")
        assert engine.model_preference == "auto"

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_init_default_is_claude(self):
        """Default preference is claude (requires ANTHROPIC_API_KEY)."""
        engine = AIEngine()
        assert engine.model_preference == "claude"


class TestAIEngineRouting:
    """Test AIEngine routing decisions."""

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_should_use_claude_with_claude_preference(self):
        """Claude preference always uses Claude."""
        engine = AIEngine(model_preference="claude")
        assert engine._should_use_claude(token_count=100, intent="navigation") is True

    def test_should_use_claude_with_ollama_preference(self):
        """Ollama preference never uses Claude."""
        engine = AIEngine(model_preference="ollama")
        assert engine._should_use_claude(token_count=5000, intent="design_question") is False

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_should_use_claude_with_auto_preference_large(self):
        """Auto preference uses Claude for large contexts."""
        engine = AIEngine(model_preference="auto")
        assert engine._should_use_claude(token_count=2500, intent="navigation") is True

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_should_use_claude_with_auto_preference_small_simple(self):
        """Auto preference uses Ollama for small simple queries."""
        engine = AIEngine(model_preference="auto")
        assert engine._should_use_claude(token_count=500, intent="debugging") is False

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_should_use_claude_with_auto_preference_small_complex(self):
        """Auto preference uses Claude for small but complex queries."""
        engine = AIEngine(model_preference="auto")
        assert engine._should_use_claude(token_count=500, intent="design_question") is True


class TestAIEngineModels:
    """Test AIEngine model configuration."""

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_claude_model_is_sonnet(self):
        """Claude model should be Sonnet."""
        engine = AIEngine(model_preference="claude")
        assert "sonnet" in engine.claude_model.lower()

    def test_ollama_model_from_env(self):
        """Ollama model can be overridden via env var."""
        import os

        # Save current value
        old_value = os.environ.get("OLLAMA_MODEL")

        try:
            os.environ["OLLAMA_MODEL"] = "mistral"
            engine = AIEngine(model_preference="ollama")
            assert engine.ollama_model == "mistral"
        finally:
            # Restore
            if old_value is not None:
                os.environ["OLLAMA_MODEL"] = old_value
            else:
                os.environ.pop("OLLAMA_MODEL", None)

    def test_ollama_model_default(self):
        """Ollama defaults to llama3."""
        import os

        old_value = os.environ.get("OLLAMA_MODEL")
        try:
            os.environ.pop("OLLAMA_MODEL", None)
            engine = AIEngine(model_preference="ollama")
            assert engine.ollama_model == "llama3"
        finally:
            if old_value is not None:
                os.environ["OLLAMA_MODEL"] = old_value
