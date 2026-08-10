"""Anthropic adapter via the official SDK."""

import os
from collections.abc import AsyncIterator

import anthropic
from anthropic import AsyncAnthropic

from agent_server.conversation import normalize_tool_calls
from agent_server.providers.base import Provider, StreamEvent, estimate_tokens


class AnthropicProvider(Provider):
    name = "Anthropic"
    env_key = "ANTHROPIC_API_KEY"
    settings_key = "anthropic_api_key"

    def __init__(self):
        self._client: AsyncAnthropic | None = None
        self._client_key: str = ""

    # ── credentials ────────────────────────────────────────────────────────
    def api_key(self) -> str:
        key = os.getenv(self.env_key, "").strip() if self.env_key else ""
        if key:
            return key
        return _cached_db_key(self.settings_key)

    def has_credentials(self) -> bool:
        return bool(self.api_key())

    def _get_client(self) -> AsyncAnthropic:
        key = self.api_key()
        if self._client is None or self._client_key != key:
            self._client = AsyncAnthropic(api_key=key, max_retries=2, timeout=600.0)
            self._client_key = key
        return self._client

    def settings_fields(self) -> list[dict]:
        return [{"key": self.settings_key, "label": "API Key", "kind": "password"}]

    def invalidate_key_cache(self):
        _key_cache.pop(self.settings_key, None)

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
        system, system_message = _extract_system(messages)
        converted = _convert_messages(messages)

        kwargs: dict = {
            "model": model,
            "messages": converted,
            "max_tokens": 8192,
            "stream": True,
        }
        if system:
            kwargs["system"] = system
        elif system_message:
            kwargs["system"] = system_message["content"]

        if tools:
            kwargs["tools"] = [{
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
            } for t in tools]

        try:
            async with self._get_client().messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        if event.content_block.type == "tool_use":
                            yield {
                                "type": "tool_calls",
                                "deltas": [{
                                    "index": event.index,
                                    "id": event.content_block.id,
                                    "name": event.content_block.name,
                                    "arguments": "",
                                }],
                            }
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield {"type": "content", "text": event.delta.text}
                        elif event.delta.type == "input_json_delta":
                            yield {
                                "type": "tool_calls",
                                "deltas": [{
                                    "index": event.index,
                                    "id": None,
                                    "name": None,
                                    "arguments": event.delta.partial_json,
                                }],
                            }
                    elif event.type == "message_delta":
                        yield {"type": "finish", "reason": event.delta.stop_reason or "stop"}
                        if event.usage:
                            yield {
                                "type": "usage",
                                "usage": {
                                    "prompt_tokens": event.usage.input_tokens or 0,
                                    "completion_tokens": event.usage.output_tokens or 0,
                                    "total_tokens": (event.usage.input_tokens or 0) + (event.usage.output_tokens or 0),
                                    "cached_tokens": getattr(event.usage, "cache_read_input_tokens", 0) or 0,
                                    "reasoning_tokens": 0,
                                },
                            }
        except anthropic.APIStatusError as e:
            yield {"type": "error", "message": f"Anthropic API error {e.status_code}: {e.message}"}
            return
        except Exception as e:
            yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
            return


def _extract_system(messages: list[dict]) -> tuple[str | None, dict | None]:
    """Extract system prompt and tool results from the OpenAI-format message array."""
    system = None
    system_message = None
    for m in messages:
        if m["role"] == "system":
            if system is None:
                system = m.get("content", "")
                system_message = m
            else:
                # Concatenate multiple system messages
                system += "\n\n" + (m.get("content", "") or "")
    return system, system_message


def _convert_messages(messages: list[dict]) -> list[dict]:
    """Convert OpenAI-format messages to Anthropic format."""
    out = []
    pending_tool_results: dict[str, list[dict]] = {}

    for m in messages:
        role = m.get("role", "")

        if role == "system":
            continue  # handled by _extract_system

        if role == "tool":
            call_id = m.get("tool_call_id", "unknown")
            content = m.get("content", "")
            if content:
                pending_tool_results.setdefault("_", []).append({
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                })

        elif role == "assistant":
            content = m.get("content", "")
            tool_calls = normalize_tool_calls(m.get("tool_calls"))

            if tool_calls:
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": fn.get("name", ""),
                        "input": fn.get("arguments", "{}") if isinstance(fn.get("arguments"), dict)
                                else _safe_json_parse(fn.get("arguments", "{}")),
                    })
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "assistant", "content": content or ""})

        elif role == "user":
            # Flush pending tool results
            if pending_tool_results:
                for key, results in pending_tool_results.items():
                    out.append({"role": "user", "content": results})
                pending_tool_results.clear()

            content = m.get("content", "")
            # If content is a list (multimodal), keep as-is; Anthropic supports content arrays
            out.append({"role": "user", "content": content or ""})

    # Flush any remaining tool results
    if pending_tool_results:
        for results in pending_tool_results.values():
            out.append({"role": "user", "content": results})

    return out


def _safe_json_parse(s: str) -> dict:
    import json
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}


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
                row = conn.execute("SELECT value FROM settings WHERE key = ?", (settings_key,)).fetchone()
                value = (row[0] if row else "").strip()
            finally:
                conn.close()
    except Exception:
        value = ""
    _key_cache[settings_key] = value
    return value
