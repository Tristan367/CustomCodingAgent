import json
import os
from typing import AsyncIterator
from openai import AsyncOpenAI
from agent_server.providers.base import Provider

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def _get_deepseek_key() -> str:
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if key:
        return key
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "agent.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            row = conn.execute("SELECT value FROM settings WHERE key = 'deepseek_api_key'").fetchone()
            conn.close()
            if row:
                return row[0]
    except Exception:
        pass
    return ""


class DeepSeekProvider(Provider):
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=_get_deepseek_key(),
                base_url=DEEPSEEK_BASE_URL,
            )
        return self._client

    def supports_vision(self) -> bool:
        return False

    def count_tokens(self, messages: list[dict]) -> int:
        total = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total += len(content) // 4
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += len(part.get("text", "")) // 4
        return total

    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        temperature: float = 0.0,
        extra: dict | None = None,
    ) -> AsyncIterator[dict]:
        kwargs = {
            "model": model,
            "messages": messages,
            "stream": True,
        }

        # Thinking mode: enabled by default for v4 models.
        # reasoning_effort from extra is passed as top-level param.
        # thinking toggle goes in extra_body.
        reasoning_effort = (extra or {}).get("reasoning_effort", "high")
        kwargs["reasoning_effort"] = reasoning_effort
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        # temperature has no effect in thinking mode (docs say ignored), but pass for non-thinking compat
        kwargs["temperature"] = temperature

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        stream = await self.client.chat.completions.create(**kwargs)

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            finish = chunk.choices[0].finish_reason if chunk.choices else None

            result = {}
            if delta:
                # Reasoning content (CoT) — stream it as well so user sees thinking
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    result["reasoning"] = reasoning
                if delta.content:
                    result["content"] = delta.content
                if delta.tool_calls:
                    tool_calls = []
                    for tc in delta.tool_calls:
                        tool_calls.append({
                            "index": tc.index,
                            "id": tc.id,
                            "function": {
                                "name": tc.function.name if tc.function else None,
                                "arguments": tc.function.arguments if tc.function else None,
                            },
                        })
                    result["tool_calls"] = tool_calls
            if finish:
                result["finish_reason"] = finish
            if result:
                yield result
