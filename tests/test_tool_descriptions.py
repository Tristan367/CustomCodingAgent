"""Editable built-in tool descriptions: override, revert, per-session freeze.

The description is what the model is told a tool does. Editing it must behave
like editing the system prompt: a running session keeps its frozen copy until it
compacts, because the tool schemas sit in front of the conversation and a
changed description re-bills the whole cached prefix.
"""

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


async def test_session_freeze_survives_a_later_override(clean, tmp_path):
    session = await _session(tmp_path)
    frozen = await sp.session_tool_descriptions(session)
    assert frozen["read"] == registry.TOOLS["read"].description

    # A later global edit must not change what this session sends.
    registry.set_description_overrides({"read": "a new description"})
    again = await sp.session_tool_descriptions(await db.get_session(session["id"]))
    assert again["read"] == registry.TOOLS["read"].description

    # Clearing the freeze (what compaction does) lets the new text through.
    await db.update_session(session["id"], tool_descriptions=None)
    refreshed = await sp.session_tool_descriptions(await db.get_session(session["id"]))
    assert refreshed["read"] == "a new description"


async def test_frozen_descriptions_reach_the_schema_list(clean, tmp_path):
    session = await _session(tmp_path)
    frozen = await sp.session_tool_descriptions(session)
    registry.set_description_overrides({"read": "changed"})
    schemas = {s["function"]["name"]: s for s in registry.tool_schemas(descriptions=frozen)}
    assert schemas["read"]["function"]["description"] == registry.TOOLS["read"].description


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
