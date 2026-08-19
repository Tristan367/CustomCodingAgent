"""Inter-session mail: send, deliver, and subagent exclusion."""

import pytest

from agent_server import database as db
from agent_server.tools.base import ToolContext
from agent_server.tools.send_message import send_message
from agent_server.tools.task import TOP_LEVEL_ONLY

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_server.database.DB_PATH", tmp_path / "mail.db")
    await db.close()
    await db.init_db()
    yield tmp_path
    await db.close()


async def _session(name):
    return await db.create_session(name=name, project_dir="/tmp")


def _ctx(sid):
    return ToolContext(
        session_id=sid, project_dir="/tmp", provider="deepseek",
        model="deepseek-v4-pro", subagent_model="", prompt_profile="default",
    )


async def test_lookup_by_name(clean_db):
    a = await _session("agent-a")
    b = await _session("agent-b")
    assert (await db.get_session_by_name("agent-a"))["id"] == a["id"]
    assert (await db.get_session_by_name("agent-b"))["id"] == b["id"]
    assert await db.get_session_by_name("nope") is None


async def test_send_and_drain_round_trip(clean_db):
    a = await _session("agent-a")
    b = await _session("agent-b")
    await db.send_mail(b["id"], a["id"], "agent-a", "hello there")
    mail = await db.drain_mail(b["id"])
    assert len(mail) == 1
    assert mail[0]["from_name"] == "agent-a"
    assert mail[0]["body"] == "hello there"
    # Drained once means gone.
    assert await db.drain_mail(b["id"]) == []


async def test_mail_only_reaches_target(clean_db):
    a = await _session("agent-a")
    b = await _session("agent-b")
    c = await _session("agent-c")
    await db.send_mail(b["id"], a["id"], "agent-a", "for b")
    assert await db.drain_mail(c["id"]) == []
    assert len(await db.drain_mail(b["id"])) == 1


async def test_tool_rejects_unknown_session(clean_db, monkeypatch):
    a = await _session("agent-a")
    result = await send_message(_ctx(a["id"]), session="ghost", message="hi")
    assert result.is_error
    assert "ghost" in result.output


async def test_tool_rejects_self(clean_db):
    a = await _session("agent-a")
    result = await send_message(_ctx(a["id"]), session="agent-a", message="hi")
    assert result.is_error
    assert "yourself" in result.output


async def test_tool_sends_mail_idle(clean_db, monkeypatch):
    a = await _session("agent-a")
    b = await _session("agent-b")
    started = []
    monkeypatch.setattr("agent_server.agent.is_running", lambda sid: False)
    monkeypatch.setattr("agent_server.agent.start_run", lambda sid: started.append(sid))
    result = await send_message(_ctx(a["id"]), session="agent-b", message="hi there")
    assert not result.is_error
    assert started == [b["id"]]
    # Idle target: persisted immediately as a user message with mail_from set.
    msgs = await db.get_messages(b["id"])
    users = [m for m in msgs if m["role"] == "user"]
    assert len(users) == 1
    assert users[0]["mail_from"] == "agent-a"
    assert "hi there" in users[0]["content"]
    # Nothing left in the mailbox.
    assert await db.drain_mail(b["id"]) == []


async def test_tool_sends_mail_running(clean_db, monkeypatch):
    a = await _session("agent-a")
    b = await _session("agent-b")
    started = []
    monkeypatch.setattr("agent_server.agent.is_running", lambda sid: True)
    monkeypatch.setattr("agent_server.agent.start_run", lambda sid: started.append(sid))
    result = await send_message(_ctx(a["id"]), session="agent-b", message="hi there")
    assert not result.is_error
    # Running target: deferred via the mailbox, not persisted yet.
    assert await db.get_messages(b["id"]) == []
    mail = await db.drain_mail(b["id"])
    assert len(mail) == 1
    assert mail[0]["from_name"] == "agent-a"
    assert mail[0]["body"] == "hi there"


async def test_send_message_is_top_level_only():
    assert "send_message" in TOP_LEVEL_ONLY


async def test_has_mail(clean_db):
    a = await _session("agent-a")
    b = await _session("agent-b")
    assert not await db.has_mail(b["id"])
    await db.send_mail(b["id"], a["id"], "agent-a", "hello")
    assert await db.has_mail(b["id"])
    await db.drain_mail(b["id"])
    assert not await db.has_mail(b["id"])


async def test_round_trip_both_idle(clean_db, monkeypatch):
    a = await _session("agent-a")
    b = await _session("agent-b")
    monkeypatch.setattr("agent_server.agent.is_running", lambda sid: False)
    monkeypatch.setattr("agent_server.agent.start_run", lambda sid: None)

    await send_message(_ctx(a["id"]), session="agent-b", message="hello from a")
    msgs_b = await db.get_messages(b["id"])
    users_b = [m for m in msgs_b if m["role"] == "user"]
    assert len(users_b) == 1 and users_b[0]["mail_from"] == "agent-a"

    await send_message(_ctx(b["id"]), session="agent-a", message="reply from b")
    msgs_a = await db.get_messages(a["id"])
    users_a = [m for m in msgs_a if m["role"] == "user"]
    assert len(users_a) == 1 and users_a[0]["mail_from"] == "agent-b"
    assert "reply from b" in users_a[0]["content"]


async def test_wording_idle_target(clean_db, monkeypatch):
    a = await _session("agent-a")
    await _session("agent-b")
    started = []
    monkeypatch.setattr("agent_server.agent.is_running", lambda sid: False)
    monkeypatch.setattr("agent_server.agent.start_run", lambda sid: started.append(sid))
    result = await send_message(_ctx(a["id"]), session="agent-b", message="hi")
    assert "will reply to you shortly" in result.output
    assert "working now" not in result.output


async def test_wording_running_target(clean_db, monkeypatch):
    a = await _session("agent-a")
    await _session("agent-b")
    monkeypatch.setattr("agent_server.agent.is_running", lambda sid: True)
    monkeypatch.setattr("agent_server.agent.start_run", lambda sid: None)
    result = await send_message(_ctx(a["id"]), session="agent-b", message="hi")
    assert "working now" in result.output


async def test_stop_all_clears_mailbox(clean_db, monkeypatch):
    from agent_server import agent
    a = await _session("agent-a")
    b = await _session("agent-b")
    await db.send_mail(b["id"], a["id"], "agent-a", "hello")
    assert len(await db.drain_mail(b["id"])) == 1
    await db.send_mail(b["id"], a["id"], "agent-a", "again")
    await agent.stop_all()
    assert await db.drain_mail(b["id"]) == []


async def test_broadcast_wakes_idle_and_queues_running(clean_db, monkeypatch):
    from agent_server import agent
    a = await _session("agent-a")
    b = await _session("agent-b")
    started = []
    queued = []
    monkeypatch.setattr(agent, "is_running", lambda sid: sid == a["id"])
    monkeypatch.setattr(agent, "start_run", lambda sid: started.append(sid))
    monkeypatch.setattr(agent, "queue_message", lambda sid, text: queued.append(sid))
    sent = await agent.broadcast([a["id"], b["id"]], "hello everyone")
    assert sent == 2
    assert started == [b["id"]]
    assert queued == [a["id"]]


async def test_a_send_in_flight_when_everything_stops_does_not_wake_the_target(clean_db):
    """Stop-all aborts every run and empties the mailbox. A send that was
    already executing at that moment would otherwise deliver *after* the
    clear-out and start the target up again -- so two sessions messaging each
    other could outlive the one control meant to end everything at once."""
    import asyncio

    from agent_server.tools.base import ToolContext
    from agent_server.tools.send_message import send_message

    a = await db.create_session(name="alpha", project_dir="/tmp")
    await db.create_session(name="beta", project_dir="/tmp")

    abort = asyncio.Event()
    abort.set()
    ctx = ToolContext(session_id=a["id"], project_dir="/tmp", abort=abort)

    result = await send_message(ctx, session="beta", message="keep going")

    assert result.is_error
    assert "cancelled" in result.output
    rows = await db._fetchall("SELECT COUNT(*) AS n FROM mailbox", ())
    assert rows[0]["n"] == 0
