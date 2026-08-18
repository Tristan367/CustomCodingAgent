"""One live check: the configured provider answers.

Everything else in the suite runs on fabricated responses, so this single test
exists only to catch a broken key or a dead endpoint. It is skipped when no key
is configured and marked `live`, so it never runs in the regular suite.

Run: .venv/bin/python -m pytest -m live tests/test_live_agent.py -q -s
"""

import pytest

from agent_server.providers import get_provider

pytestmark = [pytest.mark.asyncio, pytest.mark.live]


async def test_provider_connection():
    """The cheapest real request that still proves the provider answers."""
    provider = get_provider("deepseek")
    if not provider.has_credentials():
        pytest.skip("no DeepSeek API key configured")

    text = ""
    async for event in provider.chat_completion(
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
        tools=[],
        model="deepseek-v4-flash",
    ):
        if event["type"] == "content":
            text += event["text"]
    assert text.strip(), "provider returned no content"
