"""Tests for the message serialization rules the DeepSeek API enforces.

Run: .venv/bin/python -m pytest tests/ -q
"""

from agent_server.compaction import group_messages, split_for_compaction
from agent_server.conversation import (
    build_messages,
    normalize_tool_calls,
    parse_arguments,
    pending_tool_calls,
    sanitize,
)


def row(id, role, content="", **kw):
    base = {
        "id": id, "role": role, "content": content, "tool_calls": None,
        "tool_call_id": None, "reasoning_content": None, "tool_name": None,
        "token_count": 0,
    }
    base.update(kw)
    return base


def call(cid, name="read", args='{"filePath":"/tmp/x"}'):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


# ── normalize_tool_calls ────────────────────────────────────────────────────

def test_legacy_flat_shape_is_upgraded():
    """The old {id,name,arguments} rows must still load; the API rejects them
    with 'missing field `type`'."""
    out = normalize_tool_calls([{"id": "c1", "name": "read", "arguments": '{"a":1}'}])
    assert out == [{"id": "c1", "type": "function",
                    "function": {"name": "read", "arguments": '{"a":1}'}}]


def test_canonical_shape_round_trips():
    assert normalize_tool_calls([call("c1")]) == [call("c1")]


def test_accepts_json_string_and_dict_arguments():
    assert normalize_tool_calls('[{"id":"c1","name":"read","arguments":"{}"}]')[0]["id"] == "c1"
    out = normalize_tool_calls([{"id": "c1", "name": "read", "arguments": {"filePath": "/x"}}])
    assert out[0]["function"]["arguments"] == '{"filePath": "/x"}'


def test_garbage_is_dropped():
    assert normalize_tool_calls(None) == []
    assert normalize_tool_calls("not json") == []
    assert normalize_tool_calls([{"id": "c1"}]) == []  # no name


def test_parse_arguments_survives_malformed_json():
    assert parse_arguments(call("c1", args="{bad")) == {}
    assert parse_arguments(call("c1", args='{"filePath":"/tmp/x"}')) == {"filePath": "/tmp/x"}


# ── pending_tool_calls ──────────────────────────────────────────────────────

def test_pending_finds_unanswered_calls():
    rows = [
        row(1, "user", "go"),
        row(2, "assistant", tool_calls=[call("c1"), call("c2")]),
        row(3, "tool", "done", tool_call_id="c1"),
    ]
    assistant, pending = pending_tool_calls(rows)
    assert assistant["id"] == 2
    assert [p["id"] for p in pending] == ["c2"]


def test_nothing_pending_when_all_answered():
    rows = [
        row(1, "user", "go"),
        row(2, "assistant", tool_calls=[call("c1")]),
        row(3, "tool", "done", tool_call_id="c1"),
    ]
    assert pending_tool_calls(rows)[1] == []


def test_plain_assistant_turn_has_nothing_pending():
    assert pending_tool_calls([row(1, "user", "hi"), row(2, "assistant", "hello")])[1] == []


# ── sanitize ────────────────────────────────────────────────────────────────

def test_dangling_tool_calls_are_stripped():
    """An interrupted run leaves tool_calls with no results; sending them is a 400."""
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "working", "tool_calls": [call("c1")]},
    ]
    out = sanitize(msgs)
    assert not any(m.get("tool_calls") for m in out)
    assert out[-1] == {"role": "assistant", "content": "working"}


def test_orphaned_tool_result_is_dropped():
    out = sanitize([
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "ghost", "content": "x"},
    ])
    assert all(m["role"] != "tool" for m in out)


def test_complete_tool_turn_is_preserved():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "reasoning_content": "think",
         "tool_calls": [call("c1"), call("c2")]},
        {"role": "tool", "tool_call_id": "c1", "content": "a"},
        {"role": "tool", "tool_call_id": "c2", "content": "b"},
        {"role": "assistant", "content": "answer"},
    ]
    assert sanitize(msgs) == msgs


def test_empty_assistant_message_is_removed():
    out = sanitize([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "  "}])
    assert out == [{"role": "user", "content": "hi"}]


# ── build_messages ──────────────────────────────────────────────────────────

def test_build_emits_wire_format_with_reasoning():
    """reasoning_content must survive on tool turns: DeepSeek 400s without it."""
    rows = [
        row(1, "user", "read it"),
        row(2, "assistant", "", reasoning_content="I should read",
            tool_calls=[{"id": "c1", "name": "read", "arguments": "{}"}]),
        row(3, "tool", "contents", tool_call_id="c1"),
    ]
    msgs = build_messages("SYSTEM", [], rows)
    assistant = msgs[2]
    assert assistant["tool_calls"][0]["type"] == "function"
    assert assistant["reasoning_content"] == "I should read"
    assert msgs[3] == {"role": "tool", "content": "contents", "tool_call_id": "c1"}


def test_reasoning_dropped_on_turn_ending_messages():
    """Assistant turns without tool calls do not need reasoning echoed back.
    Dropping it shrinks context, and keying on tool_calls keeps the prefix stable
    so the prompt cache still hits."""
    rows = [
        row(1, "user", "hi"),
        row(2, "assistant", "hello", reasoning_content="a long private monologue"),
    ]
    assert "reasoning_content" not in build_messages("S", [], rows)[2]


def test_compaction_summaries_precede_history():
    msgs = build_messages("SYS", [{"summary_text": "earlier"}], [row(1, "user", "hi")])
    assert msgs[0]["role"] == "system"
    assert "earlier" in msgs[1]["content"]
    assert msgs[2]["content"] == "hi"


# ── compaction grouping ─────────────────────────────────────────────────────

def test_a_tool_call_stays_with_its_results():
    """The one hard constraint: split these and every later request 400s."""
    rows = [
        row(1, "user", "a"),
        row(2, "assistant", tool_calls=[call("c1")]),
        row(3, "tool", "r", tool_call_id="c1"),
        row(4, "assistant", "done"),
    ]
    # One unit per round, not one per turn -- a long agent run has to remain
    # divisible or it can never be compacted.
    assert [len(g) for g in group_messages(rows)] == [1, 2, 1]
    call_unit = group_messages(rows)[1]
    assert call_unit[0]["role"] == "assistant" and call_unit[1]["role"] == "tool"


def _assert_wire_valid(tail):
    """No dangling tool call, no orphaned result -- either is a hard 400."""
    msgs = sanitize(build_messages("S", [], tail))
    open_ids = set()
    for m in msgs:
        if m["role"] == "assistant":
            open_ids = {c["id"] for c in m.get("tool_calls") or []}
        elif m["role"] == "tool":
            assert m["tool_call_id"] in open_ids, "orphaned tool result"
            open_ids.discard(m["tool_call_id"])
        else:
            assert not open_ids, "tool call left unanswered"
    assert not open_ids, "tool call left unanswered at the end"


def test_split_never_cuts_through_a_tool_round():
    rows = []
    for i in range(6):
        base = i * 4
        rows += [
            row(base + 1, "user", f"q{i}"),
            row(base + 2, "assistant", tool_calls=[call(f"c{i}")]),
            row(base + 3, "tool", "r", tool_call_id=f"c{i}"),
            row(base + 4, "assistant", f"a{i}"),
        ]
    head, tail = split_for_compaction(rows)
    assert head and tail
    _assert_wire_valid(tail)


def test_a_single_long_turn_can_still_be_compacted():
    """The failure this grouping exists to fix.

    One user message and 30 tool rounds used to be one atomic unit, so a real
    session sat at 74,000 tokens and compaction summarised nothing at all.
    """
    rows = [row(1, "user", "go")]
    for i in range(30):
        rows += [
            row(2 + i * 2, "assistant", tool_calls=[call(f"c{i}")], token_count=200),
            row(3 + i * 2, "tool", "r" * 40, tool_call_id=f"c{i}", token_count=3_000),
        ]
    head, tail = split_for_compaction(rows)
    assert head, "a long single turn must still be compactable"
    kept = sum(r.get("token_count") or 0 for r in tail)
    assert kept <= 24_000, f"tail should respect the budget, got {kept:,}"
    _assert_wire_valid(tail)


def test_short_conversations_are_not_compacted():
    assert split_for_compaction([row(1, "user", "a"), row(2, "assistant", "b")])[0] == []


# ── Compaction keeps a real tail, not just a summary ─────────────────────────

def _turn(i, tokens):
    return [
        {"id": i * 2, "role": "user", "content": "q", "tool_calls": None, "token_count": 20},
        {"id": i * 2 + 1, "role": "assistant", "content": "a", "tool_calls": None,
         "token_count": tokens},
    ]


def test_tail_window_grows_for_cheap_turns():
    """A summary alone would throw away context that still fits comfortably."""
    rows = [m for i in range(20) for m in _turn(i, 50)]
    head, tail = split_for_compaction(rows)
    assert len(tail) > 8, "cheap turns should keep far more than the floor"
    assert head, "something must still be summarised"


def test_expensive_turns_shrink_the_window_to_the_minimum():
    """The budget decides, not a count of turns.

    A floor measured in turns is what disabled compaction once turns got long:
    four of them came to 74,000 tokens, far past any budget worth keeping.
    """
    rows = [m for i in range(20) for m in _turn(i, 20_000)]
    head, tail = split_for_compaction(rows)
    assert len(tail) == 2, "one expensive answer plus the message that asked for it"
    assert len(head) == len(rows) - len(tail)


def test_compaction_never_loses_or_reorders_messages():
    rows = [m for i in range(20) for m in _turn(i, 3_000)]
    head, tail = split_for_compaction(rows)
    ids = [m["id"] for m in head] + [m["id"] for m in tail]
    assert ids == sorted(ids) == [m["id"] for m in rows]


def test_a_tool_call_with_no_id_is_dropped_rather_than_repeated_forever():
    """Results are matched back to calls by id. A call with no id is answered,
    and the answer is recorded, but `pending_tool_calls` can never see it as
    answered -- so it is handed back as outstanding work on every later message
    and run again, and again, for the life of the session. Silent and expensive
    in a session nobody is watching.

    Dropping it leaves an assistant turn that made no tool call, which the loop
    already knows how to finish."""
    calls = normalize_tool_calls([
        {"id": "", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        {"type": "function", "function": {"name": "grep", "arguments": "{}"}},
        {"id": "c3", "type": "function", "function": {"name": "glob", "arguments": "{}"}},
    ])
    assert [c["id"] for c in calls] == ["c3"]


def test_an_answered_call_is_not_handed_back_as_pending():
    rows = [
        row(1, "user", "go"),
        row(2, "assistant", tool_calls=[call("c1")]),
        row(3, "tool", "done", tool_call_id="c1"),
    ]
    _assistant, pending = pending_tool_calls(rows)
    assert pending == []
