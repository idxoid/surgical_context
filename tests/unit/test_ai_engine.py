"""Unit tests for AIEngine with model routing."""

import os
from unittest.mock import patch

import pytest

from sidecar.ai.engine import (
    _MIN_CACHE_TOKENS,
    AIEngine,
    ModelRouter,
    _build_system_blocks,
    cloud_llm_enabled,
)


class TestBuildSystemBlocks:
    """Unit tests for prompt-caching block splitter."""

    def test_no_markers_returns_single_block(self):
        prompt = "You are a code assistant."
        blocks = _build_system_blocks(prompt)
        assert len(blocks) == 1
        assert blocks[0]["text"] == prompt
        assert "cache_control" not in blocks[0]

    def test_large_graph_block_gets_cache_control(self):
        # Construct a prompt where the graph block exceeds _MIN_CACHE_TOKENS chars * 4
        big_code = "x" * (_MIN_CACHE_TOKENS * 4 + 100)
        prompt = f"You are a code assistant.\n--- TARGET SYMBOL: foo ---\n{big_code}"
        blocks = _build_system_blocks(prompt)
        cached = [b for b in blocks if b.get("cache_control")]
        assert len(cached) == 1
        assert cached[0]["cache_control"] == {"type": "ephemeral"}
        assert "TARGET SYMBOL" in cached[0]["text"]

    def test_small_graph_block_no_cache_control(self):
        prompt = "Preamble\n--- TARGET SYMBOL: foo ---\nsmall code"
        blocks = _build_system_blocks(prompt)
        cached = [b for b in blocks if b.get("cache_control")]
        assert cached == []

    def test_doc_block_never_cached(self):
        big_code = "x" * (_MIN_CACHE_TOKENS * 4 + 100)
        doc = "Some documentation text."
        prompt = f"Preamble\n--- TARGET SYMBOL: foo ---\n{big_code}\n--- DOCUMENTATION ---\n{doc}"
        blocks = _build_system_blocks(prompt)
        cached = [b for b in blocks if b.get("cache_control")]
        assert len(cached) == 1
        assert "DOCUMENTATION" not in cached[0]["text"]
        # doc block exists and is not cached
        doc_block = [
            b for b in blocks if "DOCUMENTATION" in b.get("text", "") or doc in b.get("text", "")
        ]
        assert doc_block
        assert "cache_control" not in doc_block[-1]

    def test_preamble_is_separate_uncached_block(self):
        preamble = "You are a Surgical Code Assistant."
        big_code = "y" * (_MIN_CACHE_TOKENS * 4 + 100)
        prompt = f"{preamble}\n--- TARGET SYMBOL: bar ---\n{big_code}"
        blocks = _build_system_blocks(prompt)
        assert blocks[0]["text"].startswith("You are")
        assert "cache_control" not in blocks[0]

    def test_empty_prompt_returns_single_block(self):
        blocks = _build_system_blocks("")
        assert len(blocks) == 1
        assert blocks[0]["text"] == ""


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

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ALLOW_CLOUD_LLM": "true"})
    def test_init_claude_preference(self):
        """Initialize with Claude preference."""
        engine = AIEngine(model_preference="claude")
        assert engine.model_preference == "claude"
        assert engine.claude_model is not None

    def test_init_ollama_preference(self):
        """Initialize with Ollama preference."""
        engine = AIEngine(model_preference="ollama")
        assert engine.model_preference == "ollama"

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ALLOW_CLOUD_LLM": "true"})
    def test_init_auto_preference(self):
        """Initialize with auto preference."""
        engine = AIEngine(model_preference="auto")
        assert engine.model_preference == "auto"

    def test_init_default_is_ollama_local_first(self):
        """Default preference is ollama (local-first)."""
        engine = AIEngine()
        assert engine.model_preference == "ollama"
        assert engine.allow_cloud_llm is False

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ALLOW_CLOUD_LLM": "false"})
    def test_claude_preference_requires_cloud_opt_in(self):
        with pytest.raises(ValueError, match="ALLOW_CLOUD_LLM"):
            AIEngine(model_preference="claude")


class TestAIEngineRouting:
    """Test AIEngine routing decisions."""

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ALLOW_CLOUD_LLM": "true"})
    def test_should_use_claude_with_claude_preference(self):
        """Claude preference always uses Claude."""
        engine = AIEngine(model_preference="claude")
        assert engine._should_use_claude(token_count=100, intent="navigation") is True

    def test_should_use_claude_with_ollama_preference(self):
        """Ollama preference never uses Claude."""
        engine = AIEngine(model_preference="ollama")
        assert engine._should_use_claude(token_count=5000, intent="design_question") is False

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ALLOW_CLOUD_LLM": "true"})
    def test_should_use_claude_with_auto_preference_large(self):
        """Auto preference uses Claude for large contexts."""
        engine = AIEngine(model_preference="auto")
        assert engine._should_use_claude(token_count=2500, intent="navigation") is True

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ALLOW_CLOUD_LLM": "true"})
    def test_should_use_claude_with_auto_preference_small_simple(self):
        """Auto preference uses Ollama for small simple queries."""
        engine = AIEngine(model_preference="auto")
        assert engine._should_use_claude(token_count=500, intent="debugging") is False

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ALLOW_CLOUD_LLM": "true"})
    def test_should_use_claude_with_auto_preference_small_complex(self):
        """Auto preference uses Claude for small but complex queries."""
        engine = AIEngine(model_preference="auto")
        assert engine._should_use_claude(token_count=500, intent="design_question") is True

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ALLOW_CLOUD_LLM": "false"})
    def test_auto_with_api_key_stays_local_without_cloud_opt_in(self):
        engine = AIEngine(model_preference="auto")
        assert engine.anthropic is None
        assert engine._should_use_claude(token_count=5000, intent="exploration") is False
        assert engine._should_use_claude(token_count=500, intent="design_question") is False

    @patch.dict(os.environ, {"ALLOW_CLOUD_LLM": "false"}, clear=False)
    def test_cloud_llm_enabled_default_false(self):
        assert cloud_llm_enabled() is False


class TestAIEngineModels:
    """Test AIEngine model configuration."""

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ALLOW_CLOUD_LLM": "true"})
    def test_claude_model_defaults_to_sonnet_4_6(self):
        """Default Claude model is Sonnet 4.6 (not retired claude-sonnet-4-20250514)."""
        from sidecar.ai.engine import DEFAULT_CLAUDE_MODEL

        engine = AIEngine(model_preference="claude")
        assert engine.claude_model == DEFAULT_CLAUDE_MODEL
        assert engine.claude_model == "claude-sonnet-4-6"
        assert "20250514" not in engine.claude_model

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "test-key",
            "ALLOW_CLOUD_LLM": "true",
            "ANTHROPIC_MODEL": "claude-sonnet-4-20250514",
        },
    )
    def test_claude_model_respects_anthropic_model_env(self):
        """ANTHROPIC_MODEL overrides the default when explicitly set."""
        engine = AIEngine(model_preference="claude")
        assert engine.claude_model == "claude-sonnet-4-20250514"

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
