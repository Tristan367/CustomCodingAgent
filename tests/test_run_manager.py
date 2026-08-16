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
    assert agent.queue_message("nothing-running", "hi") is None
    handle = agent._Run()
    agent._runs["s"] = handle
    queue_id = agent.queue_message("s", "hi")
    assert queue_id
    assert [e["text"] for e in agent._queued["s"]] == ["hi"]
    handle.done.set()
    assert agent.queue_message("s", "later") is None


def test_attaching_to_a_finished_run_terminates_immediately():
    async def go():
        handle = agent._Run()
        agent._runs["s"] = handle
        handle.done.set()
        return [e async for e in agent.subscribe("s", replay=False)]

    events = asyncio.run(go())
    assert events[-1]["type"] == "stream_end"


# ── Taking back a queued message ────────────────────────────────────────────

def test_several_queued_messages_are_delivered_as_one():
    async def go():
        handle = agent._Run()
        agent._runs["s"] = handle
        for text in ("first", "second", "third"):
            agent.queue_message("s", text)
        return await agent._flush_queued("s")

    import agent_server.database as db
    sent = {}

    async def fake_add(session_id, role, content="", **kw):
        sent["content"] = content
        return {"id": 1, "content": content}

    real, db.add_message = db.add_message, fake_add
    try:
        rows = asyncio.run(go())
    finally:
        db.add_message = real
    assert len(rows) == 1, "one message, so it costs one turn"
    assert sent["content"] == "first\n\nsecond\n\nthird"


def test_undo_removes_only_the_named_message():
    handle = agent._Run()
    agent._runs["s"] = handle
    first = agent.queue_message("s", "alpha")
    second = agent.queue_message("s", "bravo")
    assert agent.unqueue_message("s", first) == "alpha"
    assert [e["text"] for e in agent._queued["s"]] == ["bravo"]
    assert agent.unqueue_message("s", second) == "bravo"


def test_undoing_twice_is_refused():
    handle = agent._Run()
    agent._runs["s"] = handle
    queue_id = agent.queue_message("s", "alpha")
    assert agent.unqueue_message("s", queue_id) == "alpha"
    assert agent.unqueue_message("s", queue_id) is None


def test_undone_message_is_never_flushed():
    async def go():
        handle = agent._Run()
        agent._runs["s"] = handle
        keep = agent.queue_message("s", "keep me")
        drop = agent.queue_message("s", "drop me")
        agent.unqueue_message("s", drop)
        assert keep
        return await agent._flush_queued("s")

    import agent_server.database as db
    sent = {}

    async def fake_add(session_id, role, content="", **kw):
        sent["content"] = content
        return {"id": 1, "content": content}

    real, db.add_message = db.add_message, fake_add
    try:
        asyncio.run(go())
    finally:
        db.add_message = real
    assert sent["content"] == "keep me"
    assert "drop me" not in sent["content"]


# ── Stopping ────────────────────────────────────────────────────────────────

def test_a_cancelled_batch_still_records_a_result_for_every_call():
    """An unanswered tool call is treated as pending work and re-run on the
    next message, which is how a stopped batch used to resurrect itself."""
    import agent_server.agent as ag
    from agent_server.tools.base import ToolContext

    recorded = []

    async def fake_record(session_id, call, result, duration_ms=0):
        recorded.append((call["id"], result.output))
        return {"id": len(recorded)}

    async def never_finishes(name, args, ctx):
        await asyncio.sleep(3600)

    calls = [
        {"id": f"c{i}", "type": "function",
         "function": {"name": "task", "arguments": "{}"}}
        for i in range(3)
    ]

    async def go():
        ctx = ToolContext(project_dir="/tmp", session_id="s", abort=asyncio.Event())
        events = []

        async def consume():
            async for event in ag._run_batch("s", ctx, calls):
                events.append(event)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        ctx.abort.set()
        ag.request_abort("s")
        for t in list(ag._tool_tasks.get("s", ())):
            t.cancel()
        await asyncio.wait_for(task, timeout=5)
        return events

    real_record, ag._record = ag._record, fake_record
    real_exec, ag.execute_tool = ag.execute_tool, never_finishes
    ag._aborts["s"] = asyncio.Event()
    try:
        asyncio.run(go())
    finally:
        ag._record, ag.execute_tool = real_record, real_exec
        ag._aborts.pop("s", None)
        ag._tool_tasks.pop("s", None)

    assert len(recorded) == 3, f"every call needs a result, got {recorded}"
    assert all("cancelled" in output for _, output in recorded)


def test_a_fast_call_reports_before_a_slow_one_ahead_of_it():
    """A batch used to be awaited in call order, so a grep sharing a batch with
    a subagent held its finished result -- and kept its timer ticking -- until
    the subagent was done. Both then landed on the same duration."""
    import agent_server.agent as ag
    from agent_server.tools.base import ToolContext, ToolResult

    async def fake_record(session_id, call, result, duration_ms=0):
        return {"id": 1}

    async def by_name(name, args, ctx):
        await asyncio.sleep(0.30 if name == "task" else 0.01)
        return ToolResult(output=name, title=name)

    calls = [
        {"id": "slow", "type": "function", "function": {"name": "task", "arguments": "{}"}},
        {"id": "fast", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
    ]

    async def go():
        ctx = ToolContext(project_dir="/tmp", session_id="s", abort=asyncio.Event())
        return [e async for e in ag._run_batch("s", ctx, calls)]

    real_record, ag._record = ag._record, fake_record
    real_exec, ag.execute_tool = ag.execute_tool, by_name
    try:
        events = asyncio.run(go())
    finally:
        ag._record, ag.execute_tool = real_record, real_exec
        ag._tool_tasks.pop("s", None)

    ends = [e for e in events if e["type"] == "tool_end"]
    assert [e["tool_call_id"] for e in ends] == ["fast", "slow"], \
        "the fast call must be reported first, not held behind the slow one"
    # And each carries its own duration rather than the batch wall time.
    fast, slow = ends[0], ends[1]
    assert fast["duration_ms"] < 150, fast["duration_ms"]
    assert slow["duration_ms"] >= 250, slow["duration_ms"]
    # Rows are still created in call order, so a reload matches the stream.
    starts = [e["tool_call_id"] for e in events if e["type"] == "tool_start"]
    assert starts == ["slow", "fast"]
