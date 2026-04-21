"""AI Engine — unified interface for both Ollama and Anthropic SDK with prompt caching."""

import os

import ollama
from anthropic import Anthropic


class ModelRouter:
    """Route queries to appropriate model based on context size and intent."""

    # Token thresholds for model routing
    SMALL_THRESHOLD = 2000  # < 2k tokens → Ollama (unless complex intent)
    LARGE_THRESHOLD = 2000  # >= 2k tokens → Claude (better reasoning, prompt caching)

    @staticmethod
    def should_use_claude(token_count: int, intent: str) -> bool:
        """
        Decide whether to use Claude (vs Ollama).

        Strategy:
        - Large contexts (>= 2k tokens) → Claude (better reasoning, prompt caching)
        - Design/architecture/exploration/refactor questions → Claude (more sophisticated)
        - Small/simple navigation/debugging/new_feature queries → Ollama (fast, cheap)
        """
        if token_count >= ModelRouter.LARGE_THRESHOLD:
            return True
        if intent in ("design_question", "exploration", "refactor"):
            return True
        return False


class AIEngine:
    """Unified AI interface supporting Ollama and Anthropic SDK with model routing."""

    def __init__(self, model_preference: str = "claude"):
        """
        Initialize AI engine.

        Args:
            model_preference: "claude" (default), "ollama", or "auto" (route by context size/intent)
        """
        self.model_preference = model_preference
        self.claude_model = "claude-sonnet-4-20250514"  # Latest Sonnet
        self.ollama_model = os.getenv("OLLAMA_MODEL", "llama3")

        # Initialize Anthropic client if using Claude
        self.anthropic: Anthropic | None = None
        if model_preference in ("claude", "auto"):
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key and model_preference == "claude":
                raise ValueError(
                    "ANTHROPIC_API_KEY not set. Either set it or use OLLAMA_MODEL with model_preference='ollama'"
                )
            if api_key:
                self.anthropic = Anthropic(api_key=api_key)
        self.last_route = self.route(0, "exploration")

    def chat(
        self,
        system_prompt: str,
        user_message: str,
        token_count: int = 0,
        intent: str = "exploration",
    ) -> str:
        """
        Chat with LLM, routing to Claude or Ollama based on preference and context.

        Args:
            system_prompt: System message with context
            user_message: User's question
            token_count: Estimated token count of context (for routing)
            intent: Query intent (for routing)

        Returns:
            LLM response text
        """
        # Determine which model to use
        use_claude = self._should_use_claude(token_count, intent)
        self.last_route = self.route(token_count, intent)

        if use_claude and self.anthropic:
            return self._chat_claude(system_prompt, user_message, token_count)
        else:
            return self._chat_ollama(system_prompt, user_message)

    def route(self, token_count: int = 0, intent: str = "exploration") -> dict:
        """Return the effective model route without making a model request."""
        wants_claude = self._should_use_claude(token_count, intent)
        if wants_claude and self.anthropic:
            return {
                "provider": "claude",
                "model": self.claude_model,
                "preference": self.model_preference,
                "reason": "router_selected_claude",
            }
        if wants_claude and not self.anthropic:
            return {
                "provider": "ollama",
                "model": self.ollama_model,
                "preference": self.model_preference,
                "reason": "claude_unavailable_fallback",
            }
        return {
            "provider": "ollama",
            "model": self.ollama_model,
            "preference": self.model_preference,
            "reason": "router_selected_ollama",
        }

    def _should_use_claude(self, token_count: int, intent: str) -> bool:
        """Determine which model to use."""
        if self.model_preference == "claude":
            return True
        elif self.model_preference == "ollama":
            return False
        else:  # "auto"
            return ModelRouter.should_use_claude(token_count, intent)

    def _chat_claude(self, system_prompt: str, user_message: str, token_count: int) -> str:
        """Chat using Claude with prompt caching on graph_context."""
        try:
            # Detect where graph context starts in system_prompt
            # Graph context is the section between "--- DEPENDENCIES ---" and "--- DOCUMENTATION ---"
            cache_control: dict[str, str] | None = None
            if "--- DEPENDENCIES ---" in system_prompt:
                # Enable cache control for the graph context block
                # This saves costs on repeated large context queries
                cache_control = {"type": "ephemeral"}

            assert self.anthropic is not None, "anthropic client must be initialized"
            message = self.anthropic.messages.create(
                model=self.claude_model,
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": cache_control,  # type: ignore[typeddict-item]
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            )

            text_blocks = [block.text for block in message.content if hasattr(block, "text")]
            return text_blocks[0] if text_blocks else ""
        except Exception as e:
            # Fallback to Ollama if Claude fails
            import logging

            logging.warning(f"Claude request failed: {e}. Falling back to Ollama.")
            self.last_route = {
                "provider": "ollama",
                "model": self.ollama_model,
                "preference": self.model_preference,
                "reason": "claude_error_fallback",
            }
            return self._chat_ollama(system_prompt, user_message)

    def _chat_ollama(self, system_prompt: str, user_message: str) -> str:
        """Chat using Ollama."""
        try:
            response = ollama.chat(
                model=self.ollama_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
            return str(response["message"]["content"])
        except Exception as e:
            raise RuntimeError(f"Ollama request failed: {e}") from e

    def stream_chat(
        self,
        system_prompt: str,
        user_message: str,
        token_count: int = 0,
        intent: str = "exploration",
    ):
        """
        Stream chat response, yielding chunks.

        Args:
            system_prompt: System message with context
            user_message: User's question
            token_count: Estimated token count of context (for routing)
            intent: Query intent (for routing)

        Yields:
            Text chunks from LLM response
        """
        use_claude = self._should_use_claude(token_count, intent)
        self.last_route = self.route(token_count, intent)

        if use_claude and self.anthropic:
            yield from self._stream_claude(system_prompt, user_message, token_count)
        else:
            yield from self._stream_ollama(system_prompt, user_message)

    def _stream_claude(self, system_prompt: str, user_message: str, token_count: int):
        """Stream Claude response."""
        try:
            cache_control: dict[str, str] | None = None
            if "--- DEPENDENCIES ---" in system_prompt:
                cache_control = {"type": "ephemeral"}

            assert self.anthropic is not None, "anthropic client must be initialized"
            with self.anthropic.messages.stream(
                model=self.claude_model,
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": cache_control,  # type: ignore[typeddict-item]
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                for text in stream.text_stream:
                    yield from text
        except Exception as e:
            import logging

            logging.warning(f"Claude streaming failed: {e}. Falling back to Ollama.")
            self.last_route = {
                "provider": "ollama",
                "model": self.ollama_model,
                "preference": self.model_preference,
                "reason": "claude_error_fallback",
            }
            yield from self._stream_ollama(system_prompt, user_message)

    def _stream_ollama(self, system_prompt: str, user_message: str):
        """Stream Ollama response."""
        try:
            response = ollama.chat(
                model=self.ollama_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                stream=True,
            )
            for chunk in response:
                if "message" in chunk and "content" in chunk["message"]:
                    yield chunk["message"]["content"]
        except Exception as e:
            raise RuntimeError(f"Ollama streaming failed: {e}") from e
