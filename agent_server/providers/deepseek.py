"""DeepSeek adapter (OpenAI-compatible endpoint).

Behaviour is pinned to https://api-docs.deepseek.com/guides/thinking_mode :

* Thinking mode is on by default and toggled via ``extra_body={"thinking": ...}``.
* Effort is the top-level ``reasoning_effort`` param, one of
  none/minimal/low/medium/high/xhigh/max.
* ``temperature``/``top_p``/penalties are silently ignored in thinking mode, so
  this adapter does not send them.
"""

import asyncio
import os
from typing import AsyncIterator

import openai
from openai import AsyncOpenAI

from agent_server.config import DEFAULT_THINKING_EFFORT, REASONING_EFFORTS
from agent_server.providers.base import Provider, StreamEvent, estimate_tokens

BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(Provider):
    name = "deepseek"

    def __init__(self):
        self._client: AsyncOpenAI | None = None
        self._client_key: str = ""

    # ── credentials ────────────────────────────────────────────────────────
    def api_key(self) -> str:
        """Env var wins; otherwise fall back to the key saved in the UI."""
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if key:
            return key
        return _cached_db_key()

    def has_credentials(self) -> bool:
        return bool(self.api_key())

    def _get_client(self) -> AsyncOpenAI:
        key = self.api_key()
        # Rebuild when the key changes so saving a key in the UI takes effect
        # immediately instead of requiring a server restart.
        if self._client is None or self._client_key != key:
            self._client = AsyncOpenAI(api_key=key, base_url=BASE_URL, max_retries=2, timeout=600.0)
            self._client_key = key
        return self._client

    # ── capabilities ───────────────────────────────────────────────────────
    def supports_vision(self) -> bool:
        return False

    def count_tokens(self, messages: list[dict]) -> int:
        return estimate_tokens(messages)

    # ── streaming ──────────────────────────────────────────────────────────
    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        thinking_effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        effort = thinking_effort or DEFAULT_THINKING_EFFORT
        if effort not in REASONING_EFFORTS:
            effort = DEFAULT_THINKING_EFFORT

        kwargs: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "reasoning_effort": effort,
            "extra_body": {"thinking": {"type": "disabled" if effort == "none" else "enabled"}},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            stream = await self._get_client().chat.completions.create(**kwargs)
        except openai.APIStatusError as e:
            yield {"type": "error", "message": _describe(e)}
            return
        except Exception as e:  # noqa: BLE001 - surfaced to the user verbatim
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
            yield {"type": "error", "message": _describe(e)}
            return
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "message": f"Stream failed: {type(e).__name__}: {e}"}
            return

        yield {"type": "finish", "reason": finish_reason or "stop"}


async def _aclose(stream) -> None:
    try:
        await stream.close()
    except Exception:  # noqa: BLE001
        pass


def _describe(e: openai.APIStatusError) -> str:
    """Surface the API's own error text; it is far more useful than the wrapper."""
    detail = ""
    try:
        body = e.response.json()
        detail = body.get("error", {}).get("message", "")
    except Exception:  # noqa: BLE001
        detail = (getattr(e, "message", "") or str(e))[:400]
    return f"DeepSeek API error {e.status_code}: {detail or 'unknown error'}"


_key_cache: dict[str, str] = {}


def _cached_db_key() -> str:
    """Synchronous read of the saved key.

    Cached in-process so the hot path never touches sqlite; `invalidate_key_cache`
    is called whenever the key is written through the settings UI.
    """
    if "key" in _key_cache:
        return _key_cache["key"]
    value = ""
    try:
        import sqlite3

        from agent_server.config import DB_PATH

        if DB_PATH.exists():
            conn = sqlite3.connect(str(DB_PATH))
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = 'deepseek_api_key'"
                ).fetchone()
                value = (row[0] if row else "").strip()
            finally:
                conn.close()
    except Exception:  # noqa: BLE001
        value = ""
    _key_cache["key"] = value
    return value


def invalidate_key_cache():
    _key_cache.pop("key", None)
