"""Doom-loop detection distinguishes a loop from a fan-out.

A loop is the model asking for the same thing, being answered, and asking again.
A fan-out is several identical calls issued *at once* -- three subagents sharing
one prompt, which is exactly what `task`'s `count` parameter is for. The first
must be refused; the second must not be, and used to be: the detector counted
each call in a round separately, so a three-way fan-out tripped it on its own
first round and was killed before any of the three ran.
"""

import json

import pytest

from agent_server import agent


@pytest.fixture(autouse=True)
def clean_history():
    agent._doom_history.clear()
    agent._doom_recorded.clear()
    yield
    agent._doom_history.clear()
    agent._doom_recorded.clear()


def call(name: str, **args) -> dict:
    return {
        "id": f"c{abs(hash((name, json.dumps(args, sort_keys=True)))) % 100000}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_a_fan_out_of_identical_calls_is_not_a_loop():
    """Ten subagents with one prompt is one round, not ten repetitions."""
    fan_out = [call("task", prompt="find the bug") for _ in range(10)]
    refuse, fatal = agent._doom_round("s", fan_out, "m1")
    assert refuse == set(), "a single round can never be a loop"
    assert not fatal


def test_a_fan_out_repeated_every_round_is_still_a_loop():
    """Fanning out identically over and over is the same mistake, at scale."""
    fan_out = [call("task", prompt="find the bug") for _ in range(3)]
    assert agent._doom_round("s", fan_out, "m1")[0] == set()
    assert agent._doom_round("s", fan_out, "m2")[0] == set()
    refuse, _ = agent._doom_round("s", fan_out, "m3")
    assert refuse == {("task", '{"prompt": "find the bug"}')}


def test_the_same_call_three_rounds_running_is_refused():
    key = ("read", '{"filePath": "a.py"}')
    for i in range(agent.DOOM_ROUNDS - 1):
        assert agent._doom_round("s", [call("read", filePath="a.py")], f"m{i}")[0] == set()
    refuse, fatal = agent._doom_round("s", [call("read", filePath="a.py")], f"m{agent.DOOM_ROUNDS - 1}")
    assert refuse == {key}
    assert not fatal, "refusing comes first; the model gets a chance to adapt"


def test_a_repeat_that_survives_the_refusal_ends_the_turn():
    """The refusal is fed back. Ignoring it costs one request per round, so
    there has to be a point where the harness stops paying for it."""
    fatal = False
    for i in range(agent.DOOM_ABORT_ROUNDS):
        _, fatal = agent._doom_round("s", [call("read", filePath="a.py")], f"m{i}")
    assert fatal


def test_interleaved_work_is_not_a_loop():
    """Reading one file repeatedly across a turn that is otherwise progressing
    is normal. Only an unbroken run of rounds counts."""
    agent._doom_round("s", [call("read", filePath="a.py")], "m1")
    agent._doom_round("s", [call("grep", pattern="x")], "m2")
    agent._doom_round("s", [call("read", filePath="a.py")], "m3")
    refuse, fatal = agent._doom_round("s", [call("read", filePath="a.py")], "m4")
    assert refuse == set()
    assert not fatal


def test_resuming_a_paused_round_does_not_recount():
    """A permission pause re-enters _drain_pending for the same assistant
    message; the round must not be recorded a second time."""
    key = ("read", '{"filePath": "a.py"}')
    for i in range(agent.DOOM_ROUNDS - 1):
        agent._doom_round("s", [call("read", filePath="a.py")], f"m{i}")
    # Re-enter with the same assistant id (a resume): nothing new recorded.
    refuse, fatal = agent._doom_round("s", [call("read", filePath="a.py")], f"m{agent.DOOM_ROUNDS - 2}")
    assert refuse == set(), "a resume of the same round must not count twice"
    assert not fatal
    # A genuinely new turn with the same call now completes the loop.
    refuse, _ = agent._doom_round("s", [call("read", filePath="a.py")], f"m{agent.DOOM_ROUNDS - 1}")
    assert refuse == {key}


def test_different_arguments_are_different_calls():
    for i in range(agent.DOOM_ABORT_ROUNDS):
        refuse, fatal = agent._doom_round("s", [call("read", filePath=f"{i}.py")], f"m{i}")
        assert refuse == set()
        assert not fatal


def test_history_does_not_leak_between_sessions():
    for i in range(agent.DOOM_ABORT_ROUNDS):
        agent._doom_round("a", [call("read", filePath="x.py")], f"m{i}")
    refuse, fatal = agent._doom_round("b", [call("read", filePath="x.py")], "m0")
    assert refuse == set()
    assert not fatal


def test_forget_session_drops_the_history():
    agent._doom_round("s", [call("read", filePath="x.py")], "m1")
    assert "s" in agent._doom_history
    agent.forget_session("s")
    assert "s" not in agent._doom_history
    assert "s" not in agent._doom_recorded


def test_loop_notice_quotes_the_result_the_model_ignored():
    """A looping model has usually stopped reading the result, so show it."""
    from agent_server.agent import _doom_message

    msg = _doom_message("grep", "no matches found in src/")
    assert "no matches found in src/" in msg
    # And it must say who is speaking: an unattributed correction arriving
    # mid-turn looks like an injected instruction.
    assert "not a prompt injection" in msg
    assert "system-interrupt" in msg


def test_loop_notice_survives_having_no_previous_output():
    from agent_server.agent import _doom_message

    assert "grep" in _doom_message("grep", "")


def test_last_output_for_picks_the_most_recent():
    from agent_server.agent import _last_output_for

    rows = [
        {"role": "tool", "tool_name": "grep", "content": "old"},
        {"role": "assistant", "content": "thinking"},
        {"role": "tool", "tool_name": "read", "content": "other tool"},
        {"role": "tool", "tool_name": "grep", "content": "newest"},
    ]
    assert _last_output_for(rows, "grep") == "newest"
    assert _last_output_for(rows, "bash") == ""
