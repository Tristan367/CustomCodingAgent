"""Provider interface.

Providers translate a normalized message array into a stream of events. They
never raise into the caller's async generator -- transport failures are yielded
as ``error`` events, because an exception thrown after SSE headers are flushed
surfaces in the browser as an opaque "Error in input stream".
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Literal, TypedDict

FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "error"]


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

    @abstractmethod
    def supports_vision(self) -> bool:
        ...

    @abstractmethod
    def has_credentials(self) -> bool:
        ...

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
        return []

    def invalidate_key_cache(self):
        """Called after the API key is saved. Override if the provider caches."""


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
