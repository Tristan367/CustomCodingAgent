"""Tests for the message serialization rules the DeepSeek API enforces.

Run: .venv/bin/python -m pytest tests/ -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_server.compaction import group_messages, split_for_compaction  # noqa: E402
from agent_server.conversation import (  # noqa: E402
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
    """reasoning_content must survive: DeepSeek 400s without it on tool turns."""
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


def test_compaction_summaries_precede_history():
    msgs = build_messages("SYS", [{"summary_text": "earlier"}], [row(1, "user", "hi")])
    assert msgs[0]["role"] == "system"
    assert "earlier" in msgs[1]["content"]
    assert msgs[2]["content"] == "hi"


# ── compaction grouping ─────────────────────────────────────────────────────

def test_tool_group_stays_atomic():
    rows = [
        row(1, "user", "a"),
        row(2, "assistant", tool_calls=[call("c1")]),
        row(3, "tool", "r", tool_call_id="c1"),
        row(4, "assistant", "done"),
    ]
    assert [len(g) for g in group_messages(rows)] == [4]


def test_split_never_cuts_through_a_tool_group():
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
    assert len(head) % 4 == 0          # cut landed on a boundary
    assert tail[0]["role"] == "user"   # kept window opens cleanly
    # The surviving payload must still be valid on the wire.
    assert sanitize(build_messages("S", [], tail))[-1]["role"] == "assistant"


def test_short_conversations_are_not_compacted():
    assert split_for_compaction([row(1, "user", "a"), row(2, "assistant", "b")])[0] == []
