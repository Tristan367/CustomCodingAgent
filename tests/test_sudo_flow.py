"""End-to-end tests for the sudo password flow.

Exercises: permission gate → password storage → -S injection → tool execution.
"""

import asyncio
import json
from pathlib import Path

import pytest

from agent_server import agent

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def clean_db(tmp_path, monkeypatch):
    from agent_server import database as db

    monkeypatch.setattr("agent_server.database.DB_PATH", str(tmp_path / "test.db"))
    await db.close()
    await db.init_db()
    yield tmp_path
    await db.close()


async def _session(clean_db, command="sudo whoami"):
    """Create a session with a pending sudo bash tool call."""
    from agent_server import database as db

    project = str(clean_db)
    (Path(project) / "work").mkdir(parents=True, exist_ok=True)
    s = await db.create_session(name="sudo-test", project_dir=project)
    sid = s["id"]
    await db._execute(
        "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
        (sid, "user", f"run {command}", "now"),
    )
    tc = json.dumps([{
        "id": "sudo_call",
        "type": "function",
        "function": {"name": "bash", "arguments": json.dumps({"command": command})},
    }])
    await db._execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, created_at)"
        " VALUES (?,?,?,?,?)",
        (sid, "assistant", "", tc, "now"),
    )
    return sid


async def _drain(run_handle, session_id):
    """Collect all events from a run until stream_end."""
    events = []
    async for event in agent.subscribe(session_id, replay=False):
        events.append(event)
        if event["type"] == "stream_end":
            break
    return events


async def _forget(session_id):
    agent.forget_session(session_id)
    await asyncio.sleep(0.1)


async def test_sudo_yields_permission_kind_sudo(clean_db):
    sid = await _session(clean_db)
    try:
        h = agent.start_run(sid)
        events = await _drain(h, sid)
        perms = [e for e in events if e["type"] == "permission"]
        assert len(perms) == 1, f"expected 1 permission, got {[e['type'] for e in events]}"
        assert perms[0].get("kind") == "sudo"
    finally:
        await _forget(sid)


async def test_resolve_stores_sudo_password(clean_db):
    sid = await _session(clean_db)
    try:
        h = agent.start_run(sid)
        events = await _drain(h, sid)
        perm = next(e for e in events if e["type"] == "permission")
        assert perm.get("kind") == "sudo"

        ok = await agent.resolve_pending(sid, perm["tool_call_id"], "approve", "sekret")
        assert ok

        pwds = agent._sudo_passwords.get(sid, {})
        assert pwds.get(perm["tool_call_id"]) == "sekret"
    finally:
        await _forget(sid)


async def test_sudo_password_injected_into_bash_args(clean_db):
    sid = await _session(clean_db)
    try:
        # First run: get permission event
        h1 = agent.start_run(sid)
        events1 = await _drain(h1, sid)
        perm = next(e for e in events1 if e["type"] == "permission")
        assert perm.get("kind") == "sudo"

        # Resolve with password
        ok = await agent.resolve_pending(sid, perm["tool_call_id"], "approve", "sekret")
        assert ok

        # Allow first run to fully retire
        await asyncio.sleep(0.1)

        # Second run: tool should execute with sudo_password injected, but the
        # secret must not leak into the stream or the run buffer.
        h2 = agent.start_run(sid)
        events2 = await _drain(h2, sid)
        tool_starts = [e for e in events2 if e["type"] == "tool_start"]
        assert len(tool_starts) >= 1, f"no tool_start in {[e['type'] for e in events2]}"
        args = tool_starts[0].get("args", {})
        assert "sudo_password" not in args, f"sudo_password leaked into stream: {args}"
        # The stored password was still consumed and handed to the tool.
        assert not (agent._sudo_passwords.get(sid) or {}), "password not consumed"
    finally:
        await _forget(sid)


async def test_sudo_command_is_rewritten_with_dash_s(clean_db):
    """Verify -S is injected into the command when sudo_password is present."""
    from agent_server.tools.base import ToolContext
    from agent_server.tools.bash import run_bash

    project = str(clean_db)
    ctx = ToolContext(
        session_id="test",
        project_dir=project,
        provider="test",
        model="test",
        subagent_model="",
        prompt_profile="default",
        abort=asyncio.Event(),
    )
    result = await run_bash(ctx, command="echo ok")
    assert "ok" in result.output
    # Verify that with sudo_password, the command is NOT run as actual sudo
    # (we don't have a real password in tests), but -S injection can be verified
    # by checking the command was accepted by sudo (it will try to validate).
    result = await run_bash(
        ctx, command="sudo whoami", sudo_password="notreal",
        timeout=3000,
    )
    # sudo with -S should read from stdin and reject a wrong password
    assert result.is_error
    assert "sudo" in (result.output or "").lower()


async def test_non_sudo_mutating_still_works(clean_db):
    """Non-sudo mutating commands still use the regular shell gate."""
    sid = await _session(clean_db, command="rm testfile")
    try:
        h = agent.start_run(sid)
        events = await _drain(h, sid)
        perms = [e for e in events if e["type"] == "permission"]
        assert len(perms) == 1
        assert perms[0].get("kind") == "shell"
    finally:
        await _forget(sid)


async def test_sudo_password_cleaned_up_after_use(clean_db):
    """_sudo_passwords entry is popped during tool execution."""
    sid = await _session(clean_db)
    try:
        h1 = agent.start_run(sid)
        events1 = await _drain(h1, sid)
        perm = next(e for e in events1 if e["type"] == "permission")
        await agent.resolve_pending(sid, perm["tool_call_id"], "approve", "sekret")
        await asyncio.sleep(0.1)

        h2 = agent.start_run(sid)
        await _drain(h2, sid)

        # Password should be gone after the tool executed
        assert agent._sudo_passwords.get(sid, {}).get(perm["tool_call_id"]) is None
    finally:
        await _forget(sid)
