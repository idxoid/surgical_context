"""AI Engine — unified interface for both Ollama and Anthropic SDK with prompt caching."""

import logging
import os

import ollama
from anthropic import Anthropic

_log = logging.getLogger(__name__)


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def cloud_llm_enabled() -> bool:
    """Whether outbound cloud LLM calls (Anthropic) are permitted."""
    return _env_flag("ALLOW_CLOUD_LLM", default=False)

# Markers written by PromptContext.to_system_prompt()
_CONTEXT_MARKERS = ("--- TARGET SYMBOL:", "--- DEPENDENCIES ---", "--- DOCUMENTATION ---")

# Minimum tokens to bother caching a context block. The Anthropic cache
# write overhead is only worth it above ~1024 tokens; below that we skip
# cache_control to avoid paying the write fee for negligible savings.
_MIN_CACHE_TOKENS = 1024


def _build_system_blocks(system_prompt: str) -> list[dict]:
    """Split system_prompt into cacheable API blocks.

    Anthropic prompt caching requires the cacheable content to be a separate
    text block with ``cache_control: {type: ephemeral}``. The block must not
    be the last one — the final user turn always re-enters the cache lookup.

    Structure we produce:
      1. Instruction preamble (before any context marker) — not cached,
         changes per intent/mode.
      2. Code + graph context block — cached when large enough; this is the
         expensive part that stays stable across follow-up questions on the
         same symbol.
      3. (Optional) Documentation block — not cached; doc chunks rotate more
         than code and are usually short.

    If there are no context markers the whole prompt goes as a single
    uncached block (e.g. direct-LLM mode).
    """
    # Find the first context marker to split preamble from context body
    split_pos = -1
    for marker in _CONTEXT_MARKERS:
        pos = system_prompt.find(marker)
        if pos != -1 and (split_pos == -1 or pos < split_pos):
            split_pos = pos

    if split_pos == -1:
        # No structured context — single block, no caching overhead
        return [{"type": "text", "text": system_prompt}]

    preamble = system_prompt[:split_pos].rstrip()
    context_body = system_prompt[split_pos:]

    # Split context_body at documentation marker so docs are a separate block
    doc_marker = "\n--- DOCUMENTATION ---"
    doc_pos = context_body.find(doc_marker)
    if doc_pos != -1:
        code_graph_block = context_body[:doc_pos].rstrip()
        doc_block = context_body[doc_pos:].lstrip()
    else:
        code_graph_block = context_body
        doc_block = ""

    # Rough token estimate: 1 token ≈ 4 chars
    code_graph_tokens = len(code_graph_block) // 4

    blocks: list[dict] = []
    if preamble:
        blocks.append({"type": "text", "text": preamble})

    if code_graph_tokens >= _MIN_CACHE_TOKENS:
        blocks.append(
            {
                "type": "text",
                "text": code_graph_block,
                "cache_control": {"type": "ephemeral"},
            }
        )
    elif code_graph_block:
        blocks.append({"type": "text", "text": code_graph_block})

    if doc_block:
        blocks.append({"type": "text", "text": doc_block})

    return blocks if blocks else [{"type": "text", "text": system_prompt}]


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

    def __init__(
        self,
        model_preference: str = "ollama",
        *,
        allow_cloud_llm: bool | None = None,
    ):
        """
        Initialize AI engine.

        Args:
            model_preference: "ollama" (local-first default), "auto", or "claude"
            allow_cloud_llm: When False (default via ALLOW_CLOUD_LLM env), never send
                prompts to Anthropic even if ANTHROPIC_API_KEY is set. "auto" and
                "claude" require allow_cloud_llm=True.
        """
        self.model_preference = model_preference
        self.allow_cloud_llm = (
            cloud_llm_enabled() if allow_cloud_llm is None else allow_cloud_llm
        )
        self.claude_model = "claude-sonnet-4-20250514"  # Latest Sonnet
        self.ollama_model = os.getenv("OLLAMA_MODEL", "llama3")

        if model_preference == "claude" and not self.allow_cloud_llm:
            raise ValueError(
                "MODEL_PREFERENCE=claude requires ALLOW_CLOUD_LLM=true. "
                "Local-first default keeps assembled context on Ollama unless you opt in."
            )

        self.anthropic: Anthropic | None = None
        if self.allow_cloud_llm and model_preference in ("claude", "auto"):
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key and model_preference == "claude":
                raise ValueError(
                    "ANTHROPIC_API_KEY not set. Either set it or use model_preference='ollama'"
                )
            if api_key:
                self.anthropic = Anthropic(api_key=api_key)
        elif model_preference in ("claude", "auto") and os.getenv("ANTHROPIC_API_KEY"):
            _log.info(
                "ANTHROPIC_API_KEY is set but ALLOW_CLOUD_LLM is false; "
                "routing stays on Ollama (local-first default)."
            )
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
        if not self.allow_cloud_llm:
            return False
        if self.model_preference == "claude":
            return True
        if self.model_preference == "ollama":
            return False
        return ModelRouter.should_use_claude(token_count, intent)

    def _chat_claude(self, system_prompt: str, user_message: str, token_count: int) -> str:
        """Chat using Claude with prompt caching on graph_context."""
        try:
            assert self.anthropic is not None, "anthropic client must be initialized"
            message = self.anthropic.messages.create(
                model=self.claude_model,
                max_tokens=2048,
                system=_build_system_blocks(system_prompt),
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
            assert self.anthropic is not None, "anthropic client must be initialized"
            with self.anthropic.messages.stream(
                model=self.claude_model,
                max_tokens=2048,
                system=_build_system_blocks(system_prompt),
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
