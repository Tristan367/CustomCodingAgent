"""What a page gets back when it reattaches to a running turn.

Two things lived only in the browser and were lost on refresh, even though the
server had both the whole time:

  * how long each running call had been running -- the elapsed figure was
    counted from when the row was drawn, so after a reload three subagents that
    had been working for minutes all read "5.0s" five seconds later;
  * a message typed mid-run. It is held in `_queued` on the server until the
    turn can take it, but only the page drew it, so a refresh appeared to throw
    away a message that was still going to be delivered.

Nothing the UI shows should be derived from the UI.
"""

import asyncio

import pytest

from agent_server import agent


@pytest.fixture
def run(monkeypatch):
    """A live run, without starting a real turn."""
    handle = agent._Run()
    monkeypatch.setitem(agent._runs, "s1", handle)
    monkeypatch.setattr(agent, "_queued", {})
    yield handle
    handle.done.set()


async def _attached_event(session_id="s1"):
    async for event in agent.subscribe(session_id, replay=False):
        if event["type"] == "attached":
            return event
    raise AssertionError("no attached event")


async def test_a_running_call_reports_how_long_it_has_been_running(run):
    agent._publish(run, {"type": "tool_start", "tool_call_id": "t1",
                         "name": "task", "args": {"description": "corpora"}})
    await asyncio.sleep(0.25)
    event = await _attached_event()

    call = next(c for c in event["inflight"] if c["tool_call_id"] == "t1")
    assert call["elapsed_ms"] >= 200, (
        f"the server reported {call['elapsed_ms']}ms for a call a quarter of a "
        "second old -- a reloaded page would restart its clock")
    assert call["elapsed_ms"] < 5000
    assert call["name"] == "task", "the rest of the event must survive"


async def test_the_internal_timestamp_never_reaches_the_client(run):
    """It is a monotonic reading. It means nothing anywhere else."""
    agent._publish(run, {"type": "tool_start", "tool_call_id": "t2", "name": "bash"})
    event = await _attached_event()
    assert all("_started" not in call for call in event["inflight"])


async def test_each_parallel_call_gets_its_own_elapsed(run):
    agent._publish(run, {"type": "tool_start", "tool_call_id": "a", "name": "task"})
    await asyncio.sleep(0.3)
    agent._publish(run, {"type": "tool_start", "tool_call_id": "b", "name": "task"})
    event = await _attached_event()

    by_id = {c["tool_call_id"]: c["elapsed_ms"] for c in event["inflight"]}
    assert by_id["a"] > by_id["b"] + 150, (
        f"calls started 300ms apart reported {by_id} -- they are sharing a clock")


async def test_a_finished_call_is_not_reported_as_running(run):
    agent._publish(run, {"type": "tool_start", "tool_call_id": "c", "name": "bash"})
    agent._publish(run, {"type": "tool_end", "tool_call_id": "c"})
    event = await _attached_event()
    assert event["inflight"] == []


async def test_a_message_queued_mid_run_survives_a_reload(run):
    queue_id = agent.queue_message("s1", "Also make sure they are public domain.")
    assert queue_id, "queueing should work while a run is live"

    event = await _attached_event()
    assert len(event["queued"]) == 1
    assert event["queued"][0]["content"] == "Also make sure they are public domain."
    assert event["queued"][0]["id"] == queue_id, (
        "without the id the restored bubble cannot be taken back")


async def test_several_queued_messages_come_back_in_order(run):
    for text in ("first", "second", "third"):
        agent.queue_message("s1", text)
    event = await _attached_event()
    assert [q["content"] for q in event["queued"]] == ["first", "second", "third"]


async def test_a_message_taken_back_does_not_come_back(run):
    keep = agent.queue_message("s1", "keep this")
    drop = agent.queue_message("s1", "changed my mind")
    assert agent.unqueue_message("s1", drop) == "changed my mind"

    event = await _attached_event()
    assert [q["id"] for q in event["queued"]] == [keep]


async def test_an_idle_session_reports_nothing_queued(run):
    event = await _attached_event()
    assert event["queued"] == []
    assert event["inflight"] == []
