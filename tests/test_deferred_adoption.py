"""Updates wait for compaction, and compaction actually takes them.

The system prompt and the tool array both sit at the front of every request, so
changing either moves the first byte of the cached prefix and re-bills the whole
conversation at the miss rate. Both are therefore frozen per session and any
change is *queued*, to be adopted at the next compaction -- where the prefix is
being rewritten regardless and the swap is close to free.

The queueing half was tested. The adopting half was not, which is the half that
matters: a change that queues and is never taken is a change that silently never
happens, and the only symptom is an agent going on using an old prompt or an old
tool schema forever.

Covers the shipped-prompt path too. A built-in moving forward on upgrade is the
case where nobody chose the change and nobody is watching for it.
"""

import pytest

from agent_server import database as db
from agent_server import system_prompt as sp
from agent_server.compaction import adopt_deferred_updates


@pytest.fixture
async def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "agent.db")
    await db.init_db()
    yield
    await db.close()


async def _session(**kwargs):
    row = await db.create_session(
        name="s", project_dir="/tmp", **kwargs)
    return row["id"]


async def _adopt(session_id: str):
    """Exactly what compaction does, by calling the function it calls.

    Not a copy of those lines: a test that reimplements the behaviour it is
    checking passes just as happily when the real code stops doing it.
    """
    await adopt_deferred_updates(session_id, dict(await db.get_session(session_id)))


# ── A running session keeps its prefix ───────────────────────────────────────

async def test_a_queued_prompt_does_not_touch_the_live_one(store):
    sid = await _session()
    await db.update_session(sid, system_prompt="ORIGINAL")
    await db.update_session(sid, pending_system_prompt="UPDATED")

    row = await db.get_session(sid)
    assert row["system_prompt"] == "ORIGINAL", (
        "the live prompt changed, so the cached prefix was thrown away")
    assert row["pending_system_prompt"] == "UPDATED"


async def test_compaction_takes_the_queued_prompt(store):
    sid = await _session()
    await db.update_session(sid, system_prompt="ORIGINAL")
    await db.update_session(sid, pending_system_prompt="UPDATED")

    await _adopt(sid)

    row = await db.get_session(sid)
    assert row["system_prompt"] == "UPDATED", (
        "the queued prompt was never adopted -- it would wait forever")
    assert not row["pending_system_prompt"], (
        "the queue was not cleared, so it would be adopted again every compaction")


async def test_compaction_drops_the_frozen_tools_so_they_refreeze(store):
    sid = await _session()
    await db.update_session(sid, tool_schemas='[{"name": "old"}]')

    await _adopt(sid)

    row = await db.get_session(sid)
    assert not row["tool_schemas"], (
        "the tool array stayed frozen, so an edited tool would never reach the model")


async def test_a_compaction_with_nothing_queued_leaves_the_prompt_alone(store):
    sid = await _session()
    await db.update_session(sid, system_prompt="ORIGINAL")

    await _adopt(sid)

    row = await db.get_session(sid)
    assert row["system_prompt"] == "ORIGINAL", "an empty queue blanked the prompt"


# ── The shipped-prompt path ──────────────────────────────────────────────────

async def test_a_built_in_moving_forward_queues_rather_than_applies(store):
    """The case nobody is watching: an upgrade changes a prompt the user never
    edited, on a conversation already in flight."""
    name = next(iter(sp.STARTER_PROMPTS))
    await db.save_prompt(name, "the old shipped text, long enough to be real", sp.SYSTEM)

    sid = await _session(prompt_profile=name)
    frozen = await sp.build_system_prompt(name, "/tmp", sid)
    await db.update_session(sid, system_prompt=frozen)

    await db.save_prompt(name, "the new shipped text, also long enough", sp.SYSTEM)
    moved = await sp.propagate_prompt(name)

    row = await db.get_session(sid)
    assert moved == 1, "the running session was not considered"
    assert row["system_prompt"] == frozen, (
        "an upgrade rewrote the live prompt of a running conversation")
    assert "the new shipped text" in (row["pending_system_prompt"] or ""), (
        "the new text was not queued, so the session would never receive it")

    await _adopt(sid)
    assert "the new shipped text" in (await db.get_session(sid))["system_prompt"]


async def test_a_session_that_never_ran_takes_it_at_once(store):
    """Nothing is cached, so there is nothing to lose by swapping it in."""
    name = next(iter(sp.STARTER_PROMPTS))
    await db.save_prompt(name, "shipped text, long enough to be real", sp.SYSTEM)
    sid = await _session(prompt_profile=name)          # never froze a prompt

    await db.save_prompt(name, "newer shipped text, long enough", sp.SYSTEM)
    await sp.propagate_prompt(name)

    row = await db.get_session(sid)
    assert "newer shipped text" in (row["system_prompt"] or ""), (
        "a session with no cached prefix should just take the new prompt")
    assert not row["pending_system_prompt"], "nothing should be queued for it"


async def test_a_session_with_its_own_prompt_is_left_alone(store):
    """`prompt_custom` means the user wrote it. Not ours to move."""
    name = next(iter(sp.STARTER_PROMPTS))
    await db.save_prompt(name, "shipped text, long enough to be real", sp.SYSTEM)
    sid = await _session(prompt_profile=name)
    await db.update_session(sid, system_prompt="MINE", prompt_custom=1)

    await db.save_prompt(name, "newer shipped text, long enough", sp.SYSTEM)
    await sp.propagate_prompt(name)

    row = await db.get_session(sid)
    assert row["system_prompt"] == "MINE"
    assert not row["pending_system_prompt"]


async def test_a_session_on_a_different_profile_is_untouched(store):
    name = next(iter(sp.STARTER_PROMPTS))
    other = "some-other-profile"
    await db.save_prompt(name, "shipped text, long enough to be real", sp.SYSTEM)
    await db.save_prompt(other, "a different prompt entirely", sp.SYSTEM)
    sid = await _session(prompt_profile=other)
    await db.update_session(sid, system_prompt="OTHER")

    await db.save_prompt(name, "newer shipped text, long enough", sp.SYSTEM)
    await sp.propagate_prompt(name)

    row = await db.get_session(sid)
    assert row["system_prompt"] == "OTHER"
    assert not row["pending_system_prompt"]


# ── Tools are visible while they wait ────────────────────────────────────────

async def test_an_edited_tool_is_reported_as_waiting(store):
    """So "I changed my tool and the agent is still using the old one" is
    visible rather than puzzling, and adopting it stays a decision."""
    sid = await _session()
    session = dict(await db.get_session(sid))
    live = await sp.live_tool_schemas(session)
    assert live, "the session has no tools to freeze"

    await db.update_session(sid, tool_schemas="[]")
    session = dict(await db.get_session(sid))
    # An empty array is falsy to the freezer, so give it something that differs.
    await db.update_session(sid, tool_schemas='[{"function": {"name": "stale"}}]')
    session = dict(await db.get_session(sid))
    assert await sp.tool_changes_pending(session) is True

    await db.update_session(sid, tool_schemas=None)
    session = dict(await db.get_session(sid))
    assert await sp.tool_changes_pending(session) is False, (
        "a session that has not frozen anything has nothing pending")
