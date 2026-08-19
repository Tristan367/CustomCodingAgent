"""Editable built-in tool descriptions: override, revert, per-session freeze.

The description is what the model is told a tool does. Editing it must behave
like editing the system prompt: a running session keeps its frozen copy until it
compacts, because the tool schemas sit in front of the conversation and a
changed description re-bills the whole cached prefix.
"""

import dataclasses
import json

import pytest
from httpx import ASGITransport, AsyncClient

from agent_server import database as db
from agent_server import system_prompt as sp
from agent_server.tools import registry


@pytest.fixture
async def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.init_db()
    yield
    registry.set_description_overrides({})
    await db.close()


async def _session(tmp_path):
    s = await db.create_session(name="s", project_dir=str(tmp_path))
    return await db.get_session(s["id"])


async def test_override_changes_the_schema(clean):
    registry.set_description_overrides({"read": "A custom read."})
    schemas = {s["function"]["name"]: s for s in registry.tool_schemas()}
    assert schemas["read"]["function"]["description"] == "A custom read."
    assert schemas["edit"]["function"]["description"] == registry.TOOLS["edit"].description


async def test_effective_description_and_revert(clean):
    default = registry.TOOLS["read"].description
    assert registry.effective_description("read") == default
    registry.set_description_override("read", "custom")
    assert registry.effective_description("read") == "custom"
    registry.set_description_override("read", None)
    assert registry.effective_description("read") == default


async def test_a_session_freezes_the_whole_tool_array(clean, tmp_path):
    """Not just the descriptions. Tools sit at the very front of the request, so
    anything about them that changes moves the first byte of the prefix and
    re-bills the whole conversation."""
    session = await _session(tmp_path)
    frozen = await sp.session_tool_schemas(session)
    read = next(s for s in frozen if s["function"]["name"] == "read")
    assert read["function"]["description"] == registry.TOOLS["read"].description

    # A later global edit must not change what this session sends.
    registry.set_description_overrides({"read": "a new description"})
    again = await sp.session_tool_schemas(await db.get_session(session["id"]))
    assert again == frozen

    # Clearing the freeze (what compaction does) lets the new text through.
    await db.update_session(session["id"], tool_schemas=None)
    refreshed = await sp.session_tool_schemas(await db.get_session(session["id"]))
    read = next(s for s in refreshed if s["function"]["name"] == "read")
    assert read["function"]["description"] == "a new description"


async def test_a_parameter_change_is_frozen_too(clean, tmp_path):
    """The half-measure this replaced. Freezing descriptions alone left the
    parameters free to move underneath, so a session could send a tool whose
    frozen description told the model to pass arguments its own live schema no
    longer accepted -- and every call it made was rejected."""
    session = await _session(tmp_path)
    await sp.session_tool_schemas(session)

    original = registry.TOOLS["read"]
    changed = dataclasses.replace(
        original,
        parameters={"type": "object", "properties": {"totallyNew": {"type": "string"}}},
    )
    registry.TOOLS["read"] = changed
    try:
        again = await sp.session_tool_schemas(await db.get_session(session["id"]))
        read = next(s for s in again if s["function"]["name"] == "read")
        assert "filePath" in read["function"]["parameters"]["properties"]
        assert "totallyNew" not in read["function"]["parameters"]["properties"]
    finally:
        registry.TOOLS["read"] = original


async def test_a_changed_tool_is_reported_as_pending(clean, tmp_path):
    """So "I edited my tool and the agent is still using the old one" is visible
    rather than puzzling. Adopting costs a full-context pass, so it is a
    decision rather than something that happens the instant you hit save."""
    session = await _session(tmp_path)
    await sp.session_tool_schemas(session)
    session = await db.get_session(session["id"])
    assert not await sp.tool_changes_pending(session)

    registry.set_description_overrides({"read": "edited while a session was open"})
    assert await sp.tool_changes_pending(session)

    await db.update_session(session["id"], tool_schemas=None)
    session = await db.get_session(session["id"])
    assert not await sp.tool_changes_pending(session), "nothing frozen, nothing pending"


async def test_load_overrides_round_trips_through_settings(clean):
    await db.set_setting("tool_descriptions", json.dumps({"bash": "run commands"}))
    await sp.load_tool_description_overrides()
    assert registry.effective_description("bash") == "run commands"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.close()
    await db.init_db()
    await sp.migrate_prompts()
    from agent_server.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    registry.set_description_overrides({})
    await db.close()


async def test_save_route_persists_and_applies(client):
    r = await client.post(
        "/_save_builtin_description", data={"name": "read", "description": "custom read"}
    )
    assert r.status_code == 303
    assert registry.effective_description("read") == "custom read"
    assert json.loads(await db.get_setting("tool_descriptions"))["read"] == "custom read"


async def test_revert_route_restores_the_default(client):
    await client.post("/_save_builtin_description", data={"name": "read", "description": "custom read"})
    r = await client.post("/_revert_builtin_description", data={"name": "read"})
    assert r.status_code == 303
    assert registry.effective_description("read") == registry.TOOLS["read"].description
    assert "read" not in json.loads(await db.get_setting("tool_descriptions", "{}"))


async def test_saving_the_default_is_a_revert(client):
    default = registry.TOOLS["read"].description
    await client.post("/_save_builtin_description", data={"name": "read", "description": "custom read"})
    r = await client.post("/_save_builtin_description", data={"name": "read", "description": default})
    assert r.status_code == 303
    assert "read" not in json.loads(await db.get_setting("tool_descriptions", "{}"))
