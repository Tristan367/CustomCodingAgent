"""The agent loop runs until the work is done, not until a counter runs out.

A cap on tool rounds ends a turn that is going fine, mid-task, with no way to
resume beyond asking again. Stopping is the user's call, so these pin the exits
that are allowed: the model stops asking for tools, it pauses for the user, or
the user aborts.
"""

import pytest

from agent_server import agent
from agent_server import database as db


class ScriptedProvider:
    """Asks for one `read` per round for `rounds`, then answers.

    Each round reads a different path. Repeating one identical call is a doom
    loop and is stopped on purpose, which is a different property from the one
    under test here -- see test_doom_loop.py.
    """

    def __init__(self, rounds: int, args: str | None = None):
        self.rounds = rounds
        self.args = args
        self.calls = 0

    def has_credentials(self):
        return True

    def supports_vision(self):
        return True

    def count_tokens(self, messages):
        return 1

    async def chat_completion(self, messages, tools, model, thinking_effort=None):
        self.calls += 1
        if self.calls <= self.rounds:
            yield {
                "type": "tool_calls",
                "deltas": [{
                    "index": 0,
                    "id": f"call_{self.calls}",
                    "name": "read",
                    "arguments": self.args or f'{{"filePath": "x{self.calls}.txt"}}',
                }],
            }
            yield {"type": "finish", "reason": "tool_calls"}
        else:
            yield {"type": "content", "text": "done"}
            yield {"type": "finish", "reason": "stop"}


@pytest.fixture
async def session(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.close()
    await db.init_db()
    (tmp_path / "x.txt").write_text("hello")
    s = await db.create_session(name="s", project_dir=str(tmp_path))
    await db.add_message(s["id"], "user", "go")
    yield s
    await db.close()


async def test_a_turn_runs_past_the_old_forty_round_cap(session, monkeypatch):
    provider = ScriptedProvider(rounds=60)
    monkeypatch.setattr(agent, "get_provider", lambda _p: provider)

    events = [e async for e in agent.run(session["id"])]

    assert provider.calls == 61, f"the loop stopped after {provider.calls} rounds"
    assert not any(
        e["type"] == "error" and "round" in e.get("message", "").lower() for e in events
    ), "a round cap ended the turn"
    assert events[-1]["type"] == "done"


async def test_a_model_stuck_on_one_call_is_stopped(session, monkeypatch):
    """Unbounded rounds means the exit has to come from somewhere. A model that
    asks for the identical thing every round is not going to produce one, and
    each round is a billed request, so the harness ends the turn itself."""
    provider = ScriptedProvider(rounds=10_000, args='{"filePath": "x.txt"}')
    monkeypatch.setattr(agent, "get_provider", lambda _p: provider)

    events = [e async for e in agent.run(session["id"])]

    assert provider.calls <= agent.DOOM_ABORT_ROUNDS + 1, \
        f"billed {provider.calls} requests for a loop that never changed"
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "repeated the same tool call" in errors[-1]["message"]


async def test_the_user_can_still_stop_it(session, monkeypatch):
    """Unbounded only means the exit is the user's, not that there is no exit."""
    provider = ScriptedProvider(rounds=10_000)
    monkeypatch.setattr(agent, "get_provider", lambda _p: provider)

    events = []
    async for event in agent.run(session["id"]):
        events.append(event)
        if provider.calls >= 5:
            agent.request_abort(session["id"])

    assert any(e["type"] == "aborted" for e in events)
    assert provider.calls < 20, "abort did not end the loop promptly"


class FlakyProvider:
    """Drops the connection on the first `failures` calls, then answers."""

    def __init__(self, failures: int, retryable: bool = True):
        self.failures = failures
        self.retryable = retryable
        self.calls = 0

    def has_credentials(self):
        return True

    def supports_vision(self):
        return True

    def count_tokens(self, messages):
        return 1

    async def chat_completion(self, messages, tools, model, thinking_effort=None):
        self.calls += 1
        if self.calls <= self.failures:
            yield {
                "type": "error",
                "message": "Stream failed: httpx.ReadError: connection reset",
                "retryable": self.retryable,
            }
        else:
            yield {"type": "content", "text": "done"}
            yield {"type": "finish", "reason": "stop"}


async def test_a_dropped_connection_retries_instead_of_ending_the_run(session, monkeypatch):
    """A transient provider failure must not end an autonomous run."""
    from agent_server.providers import base as provider_base

    monkeypatch.setattr(provider_base, "MODEL_RETRY_DELAYS", (0.0, 0.0))
    provider = FlakyProvider(failures=1)
    monkeypatch.setattr(agent, "get_provider", lambda _p: provider)

    events = [e async for e in agent.run(session["id"])]

    assert provider.calls == 2, f"expected a retry, got {provider.calls} calls"
    assert any(e["type"] == "retry" for e in events), "no retry event was emitted"
    assert events[-1]["type"] == "done", "the run did not complete after the retry"


async def test_a_bad_request_is_not_retried(session, monkeypatch):
    """A 4xx means the request itself is wrong; retrying repeats the rejection."""
    from agent_server.providers import base as provider_base

    monkeypatch.setattr(provider_base, "MODEL_RETRY_DELAYS", (0.0, 0.0))
    provider = FlakyProvider(failures=1, retryable=False)
    monkeypatch.setattr(agent, "get_provider", lambda _p: provider)

    events = [e async for e in agent.run(session["id"])]

    assert provider.calls == 1, "a non-retryable error should not be retried"
    assert not any(e["type"] == "retry" for e in events)
    assert any(e["type"] == "error" for e in events)


class AnswerProvider:
    """Replies once and stops, recording when it is asked."""

    def __init__(self, order):
        self.order = order

    def has_credentials(self):
        return True

    def supports_vision(self):
        return True

    def count_tokens(self, messages):
        return 1

    async def chat_completion(self, messages, tools, model, thinking_effort=None):
        self.order.append("model")
        yield {"type": "content", "text": "ok"}
        yield {"type": "finish", "reason": "stop"}


async def test_a_message_compacts_first_when_the_session_is_over_threshold(session, monkeypatch):
    """A turn that is over the threshold must summarise before it sends again.

    Compaction runs at a clean turn boundary -- before the next request -- so
    when the user sends a message on an over-threshold session, the summary
    happens first, then the message goes out. This is how a session that ended
    over the limit (say, on a transient error) recovers on its next message
    instead of sending a request that already exceeds the compaction threshold.
    """
    from agent_server import compaction as compaction_mod

    await db.update_session(session["id"], compact_threshold=100)
    await db.add_message(session["id"], "assistant", "did some work", token_count=500)
    await db.add_message(session["id"], "user", "continue")

    order = []

    async def fake_compact(sid, manual_summary="", extra_instructions="", prompt_override=""):
        order.append("compact")
        return {"ok": True, "compacted": 1, "kept": 1, "summary": "summarised"}

    monkeypatch.setattr(compaction_mod, "compact_session", fake_compact)
    provider = AnswerProvider(order)
    monkeypatch.setattr(agent, "get_provider", lambda _p: provider)

    events = [e async for e in agent.run(session["id"])]

    assert order == ["compact", "model"], f"compaction did not precede the model call: {order}"
    assert any(e["type"] == "compacted" for e in events)
    assert events[-1]["type"] == "done"
