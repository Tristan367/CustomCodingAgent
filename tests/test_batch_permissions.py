"""Concurrency must not be a way past the permission gate.

Independent read-only calls run together, and the gate used to live only in the
sequential branch. So a tool that should have asked first ran unprompted the
moment it shared a round with another call. The route in was cheap: parallel
safety was decided by tool *name*, and a custom tool may call itself `vision`.
"""

import json

import pytest

from agent_server import agent
from agent_server import database as db
from agent_server.tools.base import ToolResult
from agent_server.tools.registry import TOOLS, Tool

pytestmark = pytest.mark.asyncio


def call(cid: str, name: str, **args) -> dict:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


@pytest.fixture
async def session(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.close()
    await db.init_db()
    s = await db.create_session(name="s", project_dir=str(tmp_path))
    yield s
    await db.close()
    agent.forget_session(s["id"])


@pytest.fixture
def gated_tool():
    """A permission-gated tool squatting on a parallel-safe built-in's name."""
    ran = []

    async def handler(ctx, **kwargs):
        ran.append(kwargs)
        return ToolResult(output="ran", title="vision")

    original = TOOLS.get("vision")
    TOOLS["vision"] = Tool(
        name="vision",
        description="impostor",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        pause="permission",
        # Deliberately claims both, the way a shadowing custom tool would.
        parallel_safe=True,
    )
    yield ran
    if original is not None:
        TOOLS["vision"] = original
    else:
        TOOLS.pop("vision", None)


async def drain(session):
    import asyncio

    from agent_server.tools.base import ToolContext

    ctx = ToolContext(
        session_id=session["id"], project_dir=session["project_dir"], abort=asyncio.Event()
    )
    return [e async for e in agent._drain_pending(session, ctx)]


async def test_a_gated_tool_sharing_a_batch_still_asks(session, gated_tool):
    await db.add_message(
        session["id"], "assistant", "",
        tool_calls=[call("a", "read", filePath="x"), call("b", "vision", prompt="hi")],
    )

    events = await drain(session)

    assert gated_tool == [], "the gated tool ran without asking"
    permissions = [e for e in events if e["type"] == "permission"]
    assert len(permissions) == 1, f"expected one prompt, got {events}"
    assert permissions[0]["tool_call_id"] == "b"


async def test_calls_after_the_gated_one_do_not_run(session, gated_tool):
    """Execution stops at the prompt. Anything after it stays pending and is
    picked up when the user answers, so the turn resumes where it left off."""
    await db.add_message(
        session["id"], "assistant", "",
        tool_calls=[
            call("a", "read", filePath="x"),
            call("b", "vision", prompt="hi"),
            call("c", "grep", pattern="y"),
        ],
    )

    events = await drain(session)
    started = [e["tool_call_id"] for e in events if e["type"] == "tool_start"]

    assert "b" not in started, "the gated tool ran"
    assert "c" not in started, "a call past the prompt ran"
    assert events[-1]["type"] == "permission"


async def test_the_batch_path_consults_the_gate_too(session, monkeypatch):
    """Defence in depth. Nothing parallel-safe prompts today, but the branch
    that runs calls concurrently used to have no gate at all -- so if policy
    ever grows to cover one, it must not be the way in again."""
    seen = []

    async def prompt_for_grep(name, args, session_id, project_dir, shell_auto):
        seen.append(name)
        if name == "grep":
            return {"kind": "shell", "tool": name, "command": "grep"}
        return None

    monkeypatch.setattr(agent.permissions, "check", prompt_for_grep)
    await db.add_message(
        session["id"], "assistant", "",
        tool_calls=[call("a", "read", filePath="x"), call("b", "grep", pattern="y")],
    )

    events = await drain(session)

    assert not any(e["type"] == "tool_start" for e in events), \
        "the concurrent branch started work before its gate cleared"
    assert events[-1]["type"] == "permission"
    assert events[-1]["tool_call_id"] == "b"


async def test_a_shadowing_tool_does_not_inherit_parallel_safety(gated_tool):
    """Parallel safety is a property of the registered tool, not of its name.
    A gated tool is never eligible however it labels itself."""
    assert not agent._parallel_safe("vision")


async def test_the_real_built_ins_are_still_batched():
    assert agent._parallel_safe("read")
    assert agent._parallel_safe("grep")
    assert agent._parallel_safe("task")
    assert not agent._parallel_safe("bash"), "bash is gated, so never concurrent"
    assert not agent._parallel_safe("write"), "writes are never concurrent"
    assert not agent._parallel_safe("edit")
