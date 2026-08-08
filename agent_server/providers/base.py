from abc import ABC, abstractmethod
from typing import AsyncIterator


class Provider(ABC):
    """Abstract base for LLM providers."""

    @abstractmethod
    def supports_vision(self) -> bool:
        ...

    @abstractmethod
    def count_tokens(self, messages: list[dict]) -> int:
        ...

    @abstractmethod
    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        temperature: float = 0.0,
        extra: dict | None = None,
    ) -> AsyncIterator[dict]:
        """Yield streaming chunks. Last chunk must have `finish_reason` set."""
        ...
