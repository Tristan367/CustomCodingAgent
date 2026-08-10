"""Saving a session's system prompt.

The failure these pin: every click reported "saved, it takes effect at the next
compaction" even when the text had not changed, so there was no way to tell a
real queued change from a no-op, and the queue was rewritten each time.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from agent_server import database as db
from agent_server.main import app
from agent_server.system_prompt import migrate_prompts, session_system_prompt


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.close()
    await db.init_db()
    await migrate_prompts()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    await db.close()


async def _started_session(tmp_path):
    s = await db.create_session(name="s", project_dir=str(tmp_path))
    await session_system_prompt(await db.get_session(s["id"]))
    await db.add_message(s["id"], "user", "hello")
    return s["id"]


async def post(client, sid, prompt):
    r = await client.post(f"/api/sessions/{sid}/system-prompt", json={"prompt": prompt})
    return r.json()


async def test_saving_the_prompt_already_in_use_is_a_no_op(client, tmp_path):
    sid = await _started_session(tmp_path)
    live = (await client.get(f"/api/sessions/{sid}/system-prompt")).json()["live"]

    for _ in range(3):
        body = await post(client, sid, live)
        assert body["status"] == "unchanged"
        assert not body["deferred"]
    assert (await db.get_session(sid))["pending_system_prompt"] is None


async def test_a_real_change_is_queued_once(client, tmp_path):
    sid = await _started_session(tmp_path)
    assert (await post(client, sid, "NEW TEXT"))["status"] == "queued"
    # Saving the same change again must not report it as newly queued.
    assert (await post(client, sid, "NEW TEXT"))["status"] == "already_queued"
    assert (await db.get_session(sid))["pending_system_prompt"] == "NEW TEXT"


async def test_going_back_to_the_live_prompt_cancels_the_queued_change(client, tmp_path):
    sid = await _started_session(tmp_path)
    live = (await client.get(f"/api/sessions/{sid}/system-prompt")).json()["live"]
    await post(client, sid, "NEW TEXT")

    body = await post(client, sid, live)
    assert body["status"] == "cancelled"
    assert (await db.get_session(sid))["pending_system_prompt"] is None


async def test_the_endpoint_reports_live_and_pending_separately(client, tmp_path):
    """Collapsed into one field, a queued change looked like the running one."""
    sid = await _started_session(tmp_path)
    await post(client, sid, "QUEUED TEXT")

    body = (await client.get(f"/api/sessions/{sid}/system-prompt")).json()
    assert body["pending"] == "QUEUED TEXT"
    assert body["live"] != "QUEUED TEXT"
    # Structural, not a quote of the prompt's opening words: asserting the
    # wording made an unrelated prompt rewrite fail here for no reason.
    assert "Working directory:" in body["live"]
    assert body["profile"] == "default"


async def test_a_queued_change_can_be_discarded(client, tmp_path):
    sid = await _started_session(tmp_path)
    await post(client, sid, "NEW TEXT")

    r = await client.delete(f"/api/sessions/{sid}/system-prompt/pending")
    assert r.json()["ok"]
    body = (await client.get(f"/api/sessions/{sid}/system-prompt")).json()
    assert body["pending"] is None


async def test_before_the_first_message_a_change_applies_immediately(client, tmp_path):
    s = await db.create_session(name="s", project_dir=str(tmp_path))
    await session_system_prompt(await db.get_session(s["id"]))

    body = await post(client, s["id"], "NEW TEXT")
    assert body["status"] == "applied"
    assert (await db.get_session(s["id"]))["system_prompt"] == "NEW TEXT"
