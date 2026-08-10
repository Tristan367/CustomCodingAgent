"""Provider implementations for OpenAI-compatible APIs.

DeepSeek extends this with thinking-mode support; OpenRouter and custom
OpenAI-compatible endpoints use the base class directly.
"""

import asyncio
import os
from collections.abc import AsyncIterator

import openai
from openai import AsyncOpenAI

from agent_server.providers.base import Provider, StreamEvent, estimate_tokens


class OpenAICompatibleProvider(Provider):
    """Base for any OpenAI-compatible API (DeepSeek, OpenRouter, custom, etc.)."""

    base_url: str = ""          # set by subclass
    env_key: str = ""           # DEEPSEEK_API_KEY, OPENROUTER_API_KEY, etc.
    settings_key: str = ""      # DB settings row key

    def __init__(self):
        self._client: AsyncOpenAI | None = None
        self._client_key: str = ""

    # ── credentials ────────────────────────────────────────────────────────
    def api_key(self) -> str:
        key = os.getenv(self.env_key, "").strip() if self.env_key else ""
        if key:
            return key
        return _cached_db_key(self.settings_key)

    def has_credentials(self) -> bool:
        return bool(self.api_key())

    def _get_client(self) -> AsyncOpenAI:
        key = self.api_key()
        if self._client is None or self._client_key != key:
            self._client = AsyncOpenAI(api_key=key, base_url=self.base_url, max_retries=2, timeout=600.0)
            self._client_key = key
        return self._client

    # ── capabilities ───────────────────────────────────────────────────────
    def supports_vision(self) -> bool:
        return False

    def settings_fields(self) -> list[dict]:
        return []

    def invalidate_key_cache(self):
        _key_cache.pop(self.settings_key, None)

    def count_tokens(self, messages: list[dict]) -> int:
        return estimate_tokens(messages)

    # ── streaming ──────────────────────────────────────────────────────────
    def _build_kwargs(self, messages: list[dict], tools: list[dict], model: str,
                      thinking_effort: str | None = None) -> dict:
        """Build the kwargs for the chat completion call. Override to add
        provider-specific params (e.g. thinking mode for DeepSeek)."""
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        thinking_effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        kwargs = self._build_kwargs(messages, tools, model, thinking_effort)

        try:
            stream = await self._get_client().chat.completions.create(**kwargs)
        except openai.APIStatusError as e:
            yield {"type": "error", "message": _describe(e, self.name)}
            return
        except Exception as e:
            yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
            return

        finish_reason = None
        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    u = chunk.usage
                    details = getattr(u, "prompt_tokens_details", None)
                    completion_details = getattr(u, "completion_tokens_details", None)
                    yield {
                        "type": "usage",
                        "usage": {
                            "prompt_tokens": u.prompt_tokens or 0,
                            "completion_tokens": u.completion_tokens or 0,
                            "total_tokens": u.total_tokens or 0,
                            "cached_tokens": getattr(details, "cached_tokens", 0) or 0,
                            "reasoning_tokens": getattr(completion_details, "reasoning_tokens", 0) or 0,
                        },
                    }

                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if delta is not None:
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield {"type": "reasoning", "text": reasoning}
                    if delta.content:
                        yield {"type": "content", "text": delta.content}
                    if delta.tool_calls:
                        yield {
                            "type": "tool_calls",
                            "deltas": [
                                {
                                    "index": tc.index,
                                    "id": tc.id,
                                    "name": tc.function.name if tc.function else None,
                                    "arguments": tc.function.arguments if tc.function else None,
                                }
                                for tc in delta.tool_calls
                            ],
                        }

                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        except asyncio.CancelledError:
            await _aclose(stream)
            raise
        except openai.APIStatusError as e:
            yield {"type": "error", "message": _describe(e, self.name)}
            return
        except Exception as e:
            yield {"type": "error", "message": f"Stream failed: {type(e).__name__}: {e}"}
            return

        yield {"type": "finish", "reason": finish_reason or "stop"}


async def _aclose(stream) -> None:
    try:
        await stream.close()
    except Exception:
        pass


def _describe(e: openai.APIStatusError, name: str = "API") -> str:
    detail = ""
    try:
        body = e.response.json()
        detail = body.get("error", {}).get("message", "")
    except Exception:
        detail = (getattr(e, "message", "") or str(e))[:400]
    return f"{name} API error {e.status_code}: {detail or 'unknown error'}"


_key_cache: dict[str, str] = {}


def _cached_db_key(settings_key: str) -> str:
    if settings_key in _key_cache:
        return _key_cache[settings_key]
    value = ""
    try:
        import sqlite3

        from agent_server.config import DB_PATH

        if DB_PATH.exists():
            conn = sqlite3.connect(str(DB_PATH))
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (settings_key,)
                ).fetchone()
                value = (row[0] if row else "").strip()
            finally:
                conn.close()
    except Exception:
        value = ""
    _key_cache[settings_key] = value
    return value
