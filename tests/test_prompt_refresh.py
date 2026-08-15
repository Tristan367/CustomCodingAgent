"""Built-in prompts must keep improving on installs that never edited them.

The old rule recognised an unedited prompt by hashing it against a hardcoded
list of everything the app had ever shipped. Anything unrecognised was assumed
to belong to the user and frozen forever -- so a database whose prompt predated
the list stopped receiving updates permanently, without a word. The prompt in
this repository's own database was still describing a `screenshot` tool two
rewrites after that tool was deleted.
"""

import pytest

from agent_server import database as db
from agent_server import system_prompt as sp


@pytest.fixture
async def clean_prompts(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.init_db()
    yield
    await db.close()


async def test_unedited_prompt_receives_a_later_rewrite(clean_prompts, monkeypatch):
    monkeypatch.setitem(sp.STARTER_PROMPTS, "default", "version one")
    await sp._refresh_one("default", "version one")
    assert (await db.get_prompt("default"))["body"] == "version one"

    # The app ships new wording. Nobody has touched the prompt.
    await sp._refresh_one("default", "version two")
    assert (await db.get_prompt("default"))["body"] == "version two"


async def test_edited_prompt_is_never_overwritten(clean_prompts):
    await sp._refresh_one("default", "version one")
    await db.save_prompt("default", "my own careful wording")

    await sp._refresh_one("default", "version two")
    assert (await db.get_prompt("default"))["body"] == "my own careful wording"


async def test_edit_then_revert_still_tracks_upstream(clean_prompts):
    """Editing back to exactly what shipped is not an edit."""
    await sp._refresh_one("default", "version one")
    await db.save_prompt("default", "scratch")
    await db.save_prompt("default", "version one")

    await sp._refresh_one("default", "version two")
    assert (await db.get_prompt("default"))["body"] == "version two"


async def test_legacy_database_without_a_marker_is_adopted(clean_prompts, monkeypatch):
    """A body matching a historically shipped hash predates markers."""
    await db.save_prompt("default", "old shipped text")
    monkeypatch.setattr(sp, "_SHIPPED_HASHES", {sp._digest("old shipped text")})

    await sp._refresh_one("default", "version two")
    assert (await db.get_prompt("default"))["body"] == "version two"


async def test_unrecognised_legacy_body_is_left_alone(clean_prompts, monkeypatch):
    await db.save_prompt("default", "something nobody recognises")
    monkeypatch.setattr(sp, "_SHIPPED_HASHES", set())

    await sp._refresh_one("default", "version two")
    assert (await db.get_prompt("default"))["body"] == "something nobody recognises"


def test_drift_names_the_replacement_for_a_removed_tool():
    warnings = sp.prompt_drift("Call `screenshot` to see the page.")
    assert len(warnings) == 1
    assert "`screenshot` no longer exists" in warnings[0]
    assert "capture" in warnings[0]


def test_drift_ignores_tools_that_still_exist():
    assert sp.prompt_drift("Use `read` then `edit`, and `browser` to check.") == []


async def test_disabled_tools_survive_a_body_edit(clean_prompts):
    """Saving the text must not clear the profile's tool selection.

    save_prompt defaulted disabled_tools to "" and the upsert wrote it every
    time, so editing the prompt -- or the startup refresh touching it -- reset
    the selection to "everything on" without saying so.
    """
    await db.save_prompt("default", "body one", "system", "capture,websearch")
    await db.save_prompt("default", "body two")

    row = await db.get_prompt("default")
    assert row["body"] == "body two"
    assert row["disabled_tools"] == "capture,websearch"


async def test_disabled_tools_reaches_the_schema_list(clean_prompts):
    from agent_server.tools.registry import tool_schemas

    await db.save_prompt("default", "b", "system", "capture,websearch")
    off = await sp.disabled_tools({"id": "s", "prompt_profile": "default", "prompt_custom": 0})

    names = {s["function"]["name"] for s in tool_schemas(exclude=off)}
    assert "capture" not in names
    assert "websearch" not in names
    assert "read" in names


async def test_a_session_with_its_own_prompt_keeps_every_tool(clean_prompts):
    await db.save_prompt("default", "b", "system", "capture")
    off = await sp.disabled_tools({"id": "s", "prompt_profile": "default", "prompt_custom": 1})
    assert off == set()


def test_default_prompt_names_no_tool_that_does_not_exist():
    """The prompt is copied from another harness with a bigger tool set.

    Guidance for a tool we do not have is worse than no guidance: it tells the
    model to call something that will fail, and there is no way to notice
    except by watching the agent behave worse.
    """
    import re

    from agent_server.tools.registry import BUILT_IN_NAMES

    # Backticked words that are shell commands or prose, not tool names.
    prose = {
        "cd", "ls", "fd", "rg", "awk", "grep", "head", "tail", "cwd",
        "parallel", "parallelize", "expect", "plan",
    }
    named = set(re.findall(r"`([a-z][a-z0-9_-]*)`", sp.DEFAULT_PROMPT))
    unknown = named - set(BUILT_IN_NAMES) - prose
    assert not unknown, f"prompt references tools that do not exist: {sorted(unknown)}"


def test_default_prompt_has_core_sections():
    assert "Engineering Principles" in sp.DEFAULT_PROMPT
    assert "Execution Workflow" in sp.DEFAULT_PROMPT
    assert "Delivery Contract" in sp.DEFAULT_PROMPT
