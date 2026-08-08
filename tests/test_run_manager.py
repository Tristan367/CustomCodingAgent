"""Runs are owned by the server, not by the request that started them.

These cover the invariants behind that: a late subscriber still sees the whole
turn, no event is delivered twice or dropped in the gap between snapshotting
the backlog and subscribing, and a message can only be queued onto a live run.
"""

import asyncio

import pytest

from agent_server import agent


@pytest.fixture(autouse=True)
def clean_runs():
    agent._runs.clear()
    agent._queued.clear()
    yield
    agent._runs.clear()
    agent._queued.clear()


def collect(session_id, replay=True):
    async def go():
        return [e async for e in agent.subscribe(session_id, replay=replay)]
    return go


def test_subscriber_that_joins_late_still_sees_the_whole_turn():
    async def go():
        handle = agent._Run()
        agent._runs["s"] = handle
        for i in range(3):
            agent._publish(handle, {"type": "content", "text": str(i)})
        agent._publish(handle, {"type": "stream_end"})
        handle.done.set()
        return [e async for e in agent.subscribe("s")]

    events = asyncio.run(go())
    assert [e.get("text") for e in events if e["type"] == "content"] == ["0", "1", "2"]


def test_no_event_is_duplicated_or_lost_across_the_subscribe_boundary():
    async def go():
        handle = agent._Run()
        agent._runs["s"] = handle
        agent._publish(handle, {"type": "content", "text": "before"})

        seen = []

        async def reader():
            async for event in agent.subscribe("s"):
                seen.append(event)

        task = asyncio.create_task(reader())
        await asyncio.sleep(0)  # let the reader subscribe
        agent._publish(handle, {"type": "content", "text": "after"})
        agent._publish(handle, {"type": "stream_end"})
        handle.done.set()
        await asyncio.wait_for(task, timeout=2)
        return seen

    seen = asyncio.run(go())
    texts = [e.get("text") for e in seen if e["type"] == "content"]
    assert texts == ["before", "after"], texts


def test_two_sessions_run_independently():
    async def go():
        for name in ("a", "b"):
            handle = agent._Run()
            agent._runs[name] = handle
            agent._publish(handle, {"type": "content", "text": name})
            agent._publish(handle, {"type": "stream_end"})
            handle.done.set()
        a = [e async for e in agent.subscribe("a")]
        b = [e async for e in agent.subscribe("b")]
        return a, b

    a, b = asyncio.run(go())
    assert [e.get("text") for e in a if e["type"] == "content"] == ["a"]
    assert [e.get("text") for e in b if e["type"] == "content"] == ["b"]


def test_inflight_tracks_calls_that_have_not_finished():
    handle = agent._Run()
    agent._runs["s"] = handle
    agent._publish(handle, {"type": "tool_start", "tool_call_id": "1", "name": "task"})
    agent._publish(handle, {"type": "tool_start", "tool_call_id": "2", "name": "task"})
    assert set(handle.inflight) == {"1", "2"}
    agent._publish(handle, {"type": "tool_end", "tool_call_id": "1"})
    assert set(handle.inflight) == {"2"}


def test_queueing_requires_a_live_run():
    assert agent.queue_message("nothing-running", "hi") is False
    handle = agent._Run()
    agent._runs["s"] = handle
    assert agent.queue_message("s", "hi") is True
    assert agent._queued["s"] == ["hi"]
    handle.done.set()
    assert agent.queue_message("s", "later") is False


def test_attaching_to_a_finished_run_terminates_immediately():
    async def go():
        handle = agent._Run()
        agent._runs["s"] = handle
        handle.done.set()
        return [e async for e in agent.subscribe("s", replay=False)]

    events = asyncio.run(go())
    assert events[-1]["type"] == "stream_end"
