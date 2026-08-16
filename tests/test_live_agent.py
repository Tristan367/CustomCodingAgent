"""Live end-to-end checks against the real DeepSeek API.

Skipped unless a key is configured. These exercise the exact paths that used to
fail: a plain greeting, and a multi-round tool loop.

These bill a real account, so they are marked `live` and excluded by default.

Run: .venv/bin/python -m pytest -m live tests/test_live_agent.py -q -s
"""

import shutil
import tempfile
from pathlib import Path

import pytest

from agent_server import agent
from agent_server import database as db
from agent_server.conversation import build_messages
from agent_server.providers import get_provider

pytestmark = [pytest.mark.asyncio, pytest.mark.live]


@pytest.fixture
async def workspace(tmp_path_factory, monkeypatch):
    """Isolated database + project directory per test."""
    tmp = Path(tempfile.mkdtemp(prefix="codeagent-test-"))
    monkeypatch.setattr("agent_server.database.DB_PATH", tmp / "test.db")
    await db.close()
    await db.init_db()
    (tmp / "hello.txt").write_text("line one\nline two\nline three\n")
    yield tmp
    await db.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _skip_without_key():
    if not get_provider("deepseek").has_credentials():
        pytest.skip("no DeepSeek API key configured")


async def _collect(session_id) -> list[dict]:
    return [e async for e in agent.run(session_id)]


async def test_greeting_gets_a_greeting(workspace):
    """Regression: the user message used to be dropped, leaving the model with
    only a system prompt. It then invented a task and hallucinated file paths."""
    _skip_without_key()
    session = await db.create_session("greet", str(workspace), thinking_effort="minimal")
    await db.add_message(session["id"], "user", "Hello")

    events = await _collect(session["id"])
    types = [e["type"] for e in events]
    assert "error" not in types, [e for e in events if e["type"] == "error"]
    assert types[-1] == "done"

    text = "".join(e["text"] for e in events if e["type"] == "content")
    print(f"\n  reply: {text[:200]}")
    assert text.strip(), "model produced no answer"
    # It must not have wandered off inventing work.
    assert "tool_start" not in types, "greeting should not trigger tool calls"

    rows = await db.get_messages(session["id"])
    assert [r["role"] for r in rows] == ["user", "assistant"]


async def test_multi_round_tool_loop(workspace):
    """Regression: round 2 died with 'missing field `type`' because stored
    tool_calls were replayed in the wrong wire format."""
    _skip_without_key()
    session = await db.create_session("tools", str(workspace), thinking_effort="low")
    await db.add_message(
        session["id"], "user",
        "Read hello.txt in the working directory and tell me the exact text of its second line.",
    )

    events = await _collect(session["id"])
    types = [e["type"] for e in events]
    errors = [e["message"] for e in events if e["type"] == "error"]
    assert not errors, errors
    assert "tool_start" in types, "expected the model to call a tool"
    assert types[-1] == "done"

    text = "".join(e["text"] for e in events if e["type"] == "content")
    print(f"\n  reply: {text[:200]}")
    assert "line two" in text.lower()

    # The stored transcript must be replayable: this is what round 2 sent.
    rows = await db.get_messages(session["id"])
    wire = build_messages("system", [], rows)
    assistant = next(m for m in wire if m.get("tool_calls"))
    assert assistant["tool_calls"][0]["type"] == "function"
    assert "reasoning_content" in assistant, "DeepSeek 400s without it on tool turns"


async def test_shell_approval_pauses_and_resumes(workspace):
    """A write command must pause for approval, then resume with the result."""
    _skip_without_key()
    session = await db.create_session("approve", str(workspace), thinking_effort="low")
    await db.add_message(
        session["id"], "user",
        "Run exactly this shell command and report its output: touch approved.txt && echo created",
    )

    events = await _collect(session["id"])
    pause = next((e for e in events if e["type"] == "permission"), None)
    assert pause is not None, f"expected a permission pause, got {[e['type'] for e in events]}"
    print(f"\n  paused on: {pause['command']}")

    assert await agent.resolve_pending(session["id"], pause["tool_call_id"], "approve")
    resumed = await _collect(session["id"])
    errors = [e["message"] for e in resumed if e["type"] == "error"]
    assert not errors, errors
    assert (workspace / "approved.txt").exists(), "approved command did not run"


async def test_rejection_keeps_the_session_usable(workspace):
    """Rejecting used to leave an unanswered tool call, permanently 400ing the
    session. The refusal must come back as a normal tool result instead."""
    _skip_without_key()
    session = await db.create_session("reject", str(workspace), thinking_effort="low")
    await db.add_message(
        session["id"], "user", "Run this shell command: rm -rf /tmp/codeagent-does-not-exist",
    )

    events = await _collect(session["id"])
    pause = next((e for e in events if e["type"] == "permission"), None)
    if pause is None:
        pytest.skip("model declined to call bash")

    assert await agent.resolve_pending(session["id"], pause["tool_call_id"], "reject")
    resumed = await _collect(session["id"])
    errors = [e["message"] for e in resumed if e["type"] == "error"]
    assert not errors, f"session broken after rejection: {errors}"
    assert resumed[-1]["type"] == "done"
    print(f"\n  recovered: {''.join(e['text'] for e in resumed if e['type'] == 'content')[:160]}")
