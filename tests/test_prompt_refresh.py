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
