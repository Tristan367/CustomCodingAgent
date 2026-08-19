"""Named prompts: storage, migration off the old settings keys, and propagation."""

import hashlib

import pytest

from agent_server import database as db
from agent_server import system_prompt as sp
from agent_server.system_prompt import (
    _SHIPPED_HASHES,
    COMPACTION,
    DEFAULT_PROMPT,
    MINIMAL_PROMPT,
    PROTECTED_PROMPT,
    SYSTEM,
    build_system_prompt,
    get_compact_prompt,
    list_prompt_names,
    migrate_prompts,
    prompt_body,
)


@pytest.fixture
async def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.close()
    await db.init_db()
    yield
    await db.close()


async def test_migration_seeds_starters_on_a_clean_install(fresh):
    await migrate_prompts()
    assert await list_prompt_names() == sorted(sp.STARTER_PROMPTS)
    assert await prompt_body("default") == DEFAULT_PROMPT.strip()
    assert await prompt_body("minimal") == MINIMAL_PROMPT.strip()


async def test_migration_turns_user_prefs_into_its_own_prompt(fresh):
    """The preferences box was appended to every profile, so alone it is the prompt."""
    await db.set_setting("user_prefs", "Always write Rust.\r\nRun clippy.")
    await migrate_prompts()

    assert "visual-verify" in await list_prompt_names()
    body = await prompt_body("visual-verify")
    assert body == "Always write Rust.\nRun clippy."  # CRLF normalised
    assert await db.get_setting("user_prefs", "") == ""


async def test_migration_keeps_a_genuinely_edited_profile(fresh):
    await db.set_setting("profile_default", "My own words.")
    await migrate_prompts()
    assert await prompt_body("default") == "My own words."


async def test_migration_discards_a_profile_that_is_just_old_shipped_text(fresh):
    """Saving an unedited profile wrote the then-current text back into settings.

    Carrying that forward would pin the user to wording they never chose.
    """
    shipped_before = (
        "You are a coding agent working in the user's local codebase.\n\n"
        "Answer what was asked, nothing more. Be concise. Read files before editing them "
        "and follow the conventions already present. Never claim something works unless "
        "you ran it."
    )
    await db.set_setting("profile_minimal", shipped_before)
    await migrate_prompts()
    assert await prompt_body("minimal") == MINIMAL_PROMPT.strip()


async def test_migration_runs_only_once(fresh):
    await migrate_prompts()
    await db.save_prompt("default", "edited since")
    await migrate_prompts()
    assert await prompt_body("default") == "edited since"


async def test_unknown_prompt_falls_back_to_default(fresh):
    await migrate_prompts()
    assert await prompt_body("deleted-one") == DEFAULT_PROMPT.strip()


async def test_default_prompt_is_protected_from_deletion(fresh):
    await migrate_prompts()
    # The route guards this; the guard is the constant, so assert on it directly.
    assert PROTECTED_PROMPT == "default"
    assert await db.get_prompt(PROTECTED_PROMPT) is not None


async def test_crlf_is_normalised_so_a_round_trip_is_not_an_edit(fresh):
    """Textareas submit CRLF; storing it would make an untouched save look changed."""
    await db.save_prompt("p", "line one\r\nline two\r\n")
    assert (await db.get_prompt("p"))["body"] == "line one\nline two"


async def test_prompt_body_is_the_whole_prompt_plus_environment(fresh, tmp_path):
    await migrate_prompts()
    await db.save_prompt("terse", "Be terse.")
    built = await build_system_prompt("terse", str(tmp_path), "sid")
    assert built.startswith("Be terse.")
    assert "Working directory" in built
    assert "Platform:" in built
    # No preferences block appended any more.
    assert "# User preferences" not in built


async def test_editing_a_prompt_does_not_disturb_a_live_session(fresh, tmp_path):
    """A session freezes its text; a shared edit must not re-bill its cached prefix."""
    await migrate_prompts()
    s = await db.create_session(name="s", project_dir=str(tmp_path), prompt_profile="default")
    frozen = await build_system_prompt("default", str(tmp_path), s["id"])
    await db.update_session(s["id"], system_prompt=frozen)

    await db.save_prompt("default", "Totally new instructions.")
    row = await db.get_session(s["id"])
    assert row["system_prompt"] == frozen


async def test_an_improved_builtin_reaches_an_install_that_never_edited_it(fresh):
    """Seeding once froze a built-in at whatever shipped the day the db was made."""
    await migrate_prompts()
    stale = "You are a coding agent working in the user's local codebase.\n\nOld wording."
    await db.save_prompt("default", stale)
    _SHIPPED_HASHES.add(hashlib.sha256(stale.encode()).hexdigest())

    await migrate_prompts()
    assert await prompt_body("default") == DEFAULT_PROMPT.strip()


async def test_refresh_leaves_a_prompt_the_user_wrote_alone(fresh):
    await migrate_prompts()
    await db.save_prompt("default", "My own wording, do not touch.")
    await migrate_prompts()
    assert await prompt_body("default") == "My own wording, do not touch."


async def test_a_corrupt_builtin_body_is_refreshed(fresh):
    """A stray edit once left `default` as a bare "x"; the marker treats an
    unrecognised body as the user's and preserved it. Too-short-to-be-real is
    corruption, so it is refreshed instead of stuck forever."""
    await migrate_prompts()
    await db.save_prompt("default", "x")
    await migrate_prompts()
    assert await prompt_body("default") == DEFAULT_PROMPT.strip()


async def test_refresh_does_not_touch_prompts_the_user_created(fresh):
    await migrate_prompts()
    await db.save_prompt("deepseek-minimal", "Mine.")
    await migrate_prompts()
    assert await prompt_body("deepseek-minimal") == "Mine."


async def test_summarising_prompts_are_their_own_kind(fresh):
    """Both kinds need a 'default', so the key is (kind, name), not name."""
    await migrate_prompts()
    await db.save_prompt("default", "SYSTEM TEXT", SYSTEM)
    await db.save_prompt("default", "SUMMARY TEXT", COMPACTION)
    assert await prompt_body("default", SYSTEM) == "SYSTEM TEXT"
    assert await prompt_body("default", COMPACTION) == "SUMMARY TEXT"


async def test_migration_moves_the_shared_compact_setting_into_a_prompt(fresh):
    await db.set_setting("compact_prompt", "My summariser.")
    await migrate_prompts()
    assert await prompt_body("default", COMPACTION) == "My summariser."
    assert await db.get_setting("compact_prompt", "") == ""


async def test_a_session_summarises_with_its_profile_prompt(fresh, tmp_path):
    """Compaction prompt is now tied to the profile, not independently chosen."""
    await migrate_prompts()
    await db.save_prompt("strict", "Be strict.", SYSTEM)
    await db.save_prompt("strict", "Five bullets.", COMPACTION)
    s = await db.create_session(
        name="s", project_dir=str(tmp_path), prompt_profile="strict"
    )
    assert await get_compact_prompt(await db.get_session(s["id"])) == "Five bullets."


async def test_compaction_falls_back_when_profile_has_no_compaction(fresh, tmp_path):
    await migrate_prompts()
    s = await db.create_session(
        name="s", project_dir=str(tmp_path), prompt_profile="minimal"
    )
    # minimal has no compaction prompt row → falls back to default
    body = await get_compact_prompt(await db.get_session(s["id"]))
    assert body == await prompt_body(PROTECTED_PROMPT, COMPACTION)


async def test_a_session_falls_back_when_its_summariser_is_deleted(fresh, tmp_path):
    await migrate_prompts()
    await db.save_prompt("terse", "Five bullets.", COMPACTION)
    s = await db.create_session(
        name="s", project_dir=str(tmp_path), prompt_profile="terse"
    )
    await db.delete_prompt("terse", COMPACTION)
    body = await get_compact_prompt(await db.get_session(s["id"]))
    assert body == await prompt_body(PROTECTED_PROMPT, COMPACTION)


async def test_system_and_compaction_are_tied_to_profile(fresh, tmp_path):
    """Editing a system prompt is editing its profile, which bundles the compaction prompt."""
    await migrate_prompts()
    await db.save_prompt("boss", "You are the boss.", SYSTEM)
    await db.save_prompt("boss", "Summarise like a boss.", COMPACTION)
    s = await db.create_session(
        name="s", project_dir=str(tmp_path), prompt_profile="boss",
        compact_profile="boss",
    )
    row = await db.get_session(s["id"])
    assert (await build_system_prompt(row["prompt_profile"], str(tmp_path))).startswith("You are the boss.")
    assert await get_compact_prompt(row) == "Summarise like a boss."


# ── the local profile ───────────────────────────────────────────────────────

async def test_the_local_profile_cannot_spawn_subagents(fresh):
    """For a model with no subagents to delegate to. Telling one to "fan the
    work out to `task` subagents" when `task` is not in its tool list is an
    instruction it cannot follow, and it will either try anyway or narrate doing
    it -- so the delegation section comes out of the prompt as well as the tool
    coming out of the schema."""
    from agent_server.system_prompt import disabled_tools, migrate_prompts
    from agent_server.tools.registry import tool_schemas

    await migrate_prompts()

    off = await disabled_tools({"prompt_profile": "local"})
    assert off == {"task"}
    offered = {s["function"]["name"] for s in tool_schemas(exclude=off)}
    assert "task" not in offered
    assert "read" in offered and "bash" in offered

    body = (await db.get_prompt("local"))["body"]
    assert "# Delegation" not in body
    assert "`task` subagent" not in body
    assert "subagents" not in body


async def test_the_local_profile_reaches_an_existing_install(fresh):
    """A new starter profile is of no use if only fresh databases get it."""
    from agent_server.system_prompt import disabled_tools, list_prompt_names, migrate_prompts

    await db.save_prompt("default", "a prompt this user already had")
    await migrate_prompts()

    assert "local" in await list_prompt_names()
    assert await disabled_tools({"prompt_profile": "local"}) == {"task"}


async def test_the_default_profile_still_delegates(fresh):
    """The local variant is a copy with a section removed; the original must be
    untouched by its existence."""
    from agent_server.system_prompt import disabled_tools, migrate_prompts

    await migrate_prompts()
    assert await disabled_tools({"prompt_profile": "default"}) == set()
    assert "# Delegation" in (await db.get_prompt("default"))["body"]


async def test_a_readonly_profile_cannot_keep_a_stale_setting(fresh):
    """Its body is already refreshed from the file on every start, for the same
    reason: nobody can edit a read-only profile through the UI, so a value that
    is not the shipped one is not a decision -- it is something an experiment
    left behind, and there is no way to correct it from the app.

    The real install had `default` holding a master spawn limit of 2 and a stray
    tier-2 entry with an empty body, both rendered as settings the user could
    see and not change.
    """
    import json

    from agent_server.system_prompt import (
        max_concurrent_subagents,
        migrate_prompts,
        subagent_parallel_cap,
    )

    await db.save_prompt("default", "a prompt this user already had")
    await db._execute(
        "UPDATE prompts SET master_spawn_limit = 2, subagent_parallel_cap = -1,"
        " subagent_tiers = ? WHERE name = 'default'",
        (json.dumps([{"body": "", "disabled_tools": "", "parallel_cap": 7}]),),
    )

    await migrate_prompts()

    assert await subagent_parallel_cap("default", 0) == 6
    assert await max_concurrent_subagents("default") == 6
    assert await subagent_parallel_cap("default", 1) == 3
    assert (await db.get_prompt("default"))["subagent_tiers"] is None


async def test_an_editable_profile_keeps_the_number_the_user_chose(fresh):
    """The reset is only safe because a read-only profile has no user choice to
    lose. Anywhere else it would be overwriting one."""
    from agent_server.system_prompt import migrate_prompts, subagent_parallel_cap

    await db.save_prompt("mine", "my own profile")
    await db._execute("UPDATE prompts SET master_spawn_limit = 2 WHERE name = 'mine'", ())

    await migrate_prompts()

    assert await subagent_parallel_cap("mine", 0) == 2


async def test_the_local_profile_is_built_in_and_keeps_its_tool_selection(fresh):
    """It is read-only like `default`, so it resets to the shipped values on
    every start -- and the shipped value for `local` is `task` off, which is the
    entire reason it exists. Blanking the selection along with the rest would
    have quietly turned it back into a copy of `default`."""
    from agent_server.system_prompt import (
        READONLY_PROMPTS,
        disabled_tools,
        migrate_prompts,
    )

    assert "local" in READONLY_PROMPTS

    await migrate_prompts()
    await db._execute("UPDATE prompts SET disabled_tools = '' WHERE name = 'local'", ())
    await migrate_prompts()

    assert await disabled_tools({"prompt_profile": "local"}) == {"task"}
