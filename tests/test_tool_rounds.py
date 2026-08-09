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
    """Asks for one `read` per round for `rounds`, then answers."""

    def __init__(self, rounds: int, args: str = '{"file_path": "x.txt"}'):
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
                    "arguments": self.args,
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
