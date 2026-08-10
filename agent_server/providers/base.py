"""Provider interface.

Providers translate a normalized message array into a stream of events. They
never raise into the caller's async generator -- transport failures are yielded
as ``error`` events, because an exception thrown after SSE headers are flushed
surfaces in the browser as an opaque "Error in input stream".
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Literal, TypedDict

from agent_server.providers import credentials

FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "error"]

# Every provider spells the end of a turn differently. Consumers match on the
# OpenAI vocabulary, so anything else has to be translated here rather than at
# each call site -- `agent._loop` checks for "length" and `task._run` checks
# for "tool_calls", and Anthropic's "max_tokens"/"tool_use" matched neither, so
# the output-limit guard never fired and subagents returned nothing.
_FINISH_ALIASES: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "content_filter",
    "pause_turn": "stop",
}


def normalize_finish(reason: str | None) -> FinishReason:
    if not reason:
        return "stop"
    return _FINISH_ALIASES.get(reason, reason)  # type: ignore[return-value]


def blank_usage() -> dict:
    """The shape every provider must fill in, so pricing can be one function.

    `prompt_tokens` is inclusive of cached reads, which is the OpenAI
    convention the cost calculation assumes. Anthropic reports them separately
    and its adapter adds them back in; without that the uncached remainder
    clamped to zero and cache reads were billed as free.
    """
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        # Tokens written into a prompt cache. Anthropic bills these above the
        # miss rate; providers that do not charge separately leave it at zero.
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
    }


class ToolCallDelta(TypedDict, total=False):
    index: int
    id: str | None
    name: str | None
    arguments: str | None


class StreamEvent(TypedDict, total=False):
    """One incremental update from a provider.

    type:
      reasoning  -- chain-of-thought delta (`text`)
      content    -- answer delta (`text`)
      tool_calls -- partial tool call fragments (`deltas`)
      usage      -- final token accounting (`usage`)
      finish     -- terminal event (`reason`)
      error      -- transport/API failure (`message`), always terminal
    """
    type: Literal["reasoning", "content", "tool_calls", "usage", "finish", "error"]
    text: str
    deltas: list[ToolCallDelta]
    usage: dict
    reason: FinishReason
    message: str


class Provider(ABC):
    name: str = "unknown"
    env_key: str = ""       # environment variable holding the key, if any
    settings_key: str = ""  # `settings` table row holding the key, if any

    def api_key(self) -> str:
        return credentials.resolve(self.env_key, self.settings_key)

    def invalidate_key_cache(self):
        """Called after the key is saved."""
        credentials.invalidate(self.settings_key)

    @abstractmethod
    def supports_vision(self) -> bool:
        ...

    def has_credentials(self) -> bool:
        return bool(self.api_key())

    @abstractmethod
    def count_tokens(self, messages: list[dict]) -> int:
        ...

    @abstractmethod
    def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        thinking_effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        ...

    def settings_fields(self) -> list[dict]:
        """Return [{key, label, kind}] for the settings page. Override per provider."""
        if not self.settings_key:
            return []
        return [{"key": self.settings_key, "label": "API Key", "kind": "password"}]


def estimate_tokens(messages: list[dict]) -> int:
    """Rough character-based estimate, used only for UI display and compaction
    triggers. Real accounting comes from the provider's `usage` event."""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(part.get("text", ""))
        total += len(m.get("reasoning_content") or "")
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            total += len(fn.get("name", "")) + len(fn.get("arguments", "") or "")
        total += 4  # per-message role/framing overhead
    return total // 4
