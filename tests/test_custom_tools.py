"""Custom tools may not stand in for built-in ones, and one bad row may not
take the rest down with it.

Registration is a dict assignment, so a custom tool named `browser` replaced the
built-in outright -- and deleting the custom tool then removed the built-in too,
for the life of the process. An old seeder created exactly that situation on
every fresh install, with two tools whose shell scripts had unbalanced quotes
and could never run.
"""

import json

import pytest

from agent_server import database as db
from agent_server.tools import custom
from agent_server.tools.registry import BUILT_IN_NAMES, TOOLS, Tool, register_custom

# asyncio_mode is "auto": async tests need no marker, and a module-level
# asyncio mark would be applied to the synchronous ones too.


@pytest.fixture
async def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.close()
    await db.init_db()
    yield tmp_path
    await db.close()
    custom.unregister_custom(custom._custom_tool_names.copy())


def noop_tool(name, parameters=None):
    async def handler(ctx, **kwargs):
        return "ok"
    return Tool(
        name=name,
        description="",
        parameters=parameters or {"type": "object", "properties": {}},
        handler=handler,
    )


def test_the_built_in_names_are_discovered_not_listed():
    """The hardcoded list had gone stale -- it was missing websearch, explore
    and every browser tool, all of which were therefore shadowable."""
    for name in ("read", "edit", "write", "bash", "grep", "glob", "webfetch",
                 "websearch", "task", "explore", "capture", "browser"):
        assert name in BUILT_IN_NAMES, name


def test_a_custom_tool_cannot_take_a_built_in_name():
    original = TOOLS["browser"]
    error = register_custom(noop_tool("browser"))
    assert "built-in" in error
    assert TOOLS["browser"] is original, "the built-in was replaced"


def test_unregistering_never_removes_a_built_in():
    custom.unregister_custom({"read", "capture", "browser"})
    assert "read" in TOOLS
    assert "capture" in TOOLS
    assert "browser" in TOOLS


async def test_the_seeded_shadow_tools_are_removed_on_startup(clean_db):
    """They are the exact rows the old seeder wrote, recognisable by the
    import line in the script that never parsed."""
    await db.save_custom_tool(
        "vision", "seeded", "{}",
        'python3 -c "from agent_server.vision import analyze" "$TOOL_ARG_PATHS',
        True, True,
    )
    await db.init_db()
    assert [t["name"] for t in await db.list_custom_tools()] == []


async def test_a_users_own_tool_of_that_name_is_not_deleted(clean_db):
    """The migration deletes the old seeder's rows, not anything of the user's.

    `vision` is no longer a built-in, so a tool of that name is now perfectly
    legal -- which is the point of shipping it as a custom tool. It must
    survive startup and register.
    """
    await db.save_custom_tool("vision", "mine", "{}", "echo hi", True, True)
    await db.init_db()

    assert [t["name"] for t in await db.list_custom_tools()] == ["vision"]
    problems = await custom.load_custom_tools()
    assert not [p for p in problems if "built-in" in p]


async def test_one_unparseable_row_does_not_disable_the_others(clean_db):
    """Loading deregisters everything first and then parses, so one row whose
    parameters will not parse used to leave every custom tool gone and the next
    startup failing before a page could be served to fix it."""
    await db.save_custom_tool("broken", "", "{not json", "echo broken", True, False)
    await db.save_custom_tool(
        "fine", "", json.dumps({"type": "object", "properties": {}}),
        "echo fine", True, False,
    )

    problems = await custom.load_custom_tools()

    assert "fine" in TOOLS, "a good tool was lost to a bad one"
    assert any("broken" in p for p in problems)
    assert "broken" not in TOOLS


async def test_an_empty_parameters_field_means_no_parameters(clean_db):
    """It used to be stored as "" and crash json.loads at load time, because
    the save path skipped validation when the field was blank."""
    await db.save_custom_tool("bare", "", "", "echo hi", True, False)
    assert await custom.load_custom_tools() == []
    assert TOOLS["bare"].parameters == {}


async def test_every_custom_tool_registers(clean_db):
    """There is no global on/off any more.

    A tool used to carry an `enabled` flag *and* be selectable per prompt
    profile, so a tool could read as "on" and still be missing from the session
    that needed it. Availability is the profile's business alone now.
    """
    await db.save_custom_tool("mine", "d", "{}", "echo hi", True, False)
    assert await custom.load_custom_tools() == []
    assert "mine" in TOOLS


async def test_reloading_drops_tools_that_were_deleted(clean_db):
    await db.save_custom_tool("temp", "", "{}", "echo", True, False)
    await custom.load_custom_tools()
    assert "temp" in TOOLS

    await db.delete_custom_tool("temp")
    await custom.load_custom_tools()
    assert "temp" not in TOOLS


def test_secret_redirect_is_restricted_to_local_paths():
    """`back` is a form field, so a crafted value must not redirect off-box."""
    from agent_server.routes.custom_tools import _safe_back

    assert _safe_back("/") == "/"
    assert _safe_back("/?script=foo") == "/?script=foo"
    assert _safe_back("//evil.example") == "/tools"
    assert _safe_back("https://evil.example") == "/tools"
    assert _safe_back("") == "/tools"
