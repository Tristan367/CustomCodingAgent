"""The Anthropic wire format, which differs from OpenAI's in ways that 400.

Each of these covers a rule the previous adapter broke. The tool-result one is
the important one: a normal agent transcript converted to two consecutive
assistant turns followed by all the results at the end, which the API rejects
outright, so multi-round tool use on Anthropic could never work.
"""

import json

import pytest

from agent_server.providers import anthropic as ap
from agent_server.providers.base import normalize_finish


def assistant(content="", tool_calls=None):
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def tool_call(cid, name, **args):
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def tool_result(cid, output):
    return {"role": "tool", "tool_call_id": cid, "content": output}


# ── Message conversion ──────────────────────────────────────────────────────

def test_a_tool_result_follows_its_tool_use_immediately():
    """Anthropic requires the tool_result in a user turn directly after the
    assistant turn that asked. Buffering until the next real user message
    produced two assistant turns in a row and both results after the fact."""
    converted = ap._convert_messages([
        {"role": "user", "content": "go"},
        assistant(tool_calls=[tool_call("a", "read", filePath="x")]),
        tool_result("a", "contents of x"),
        assistant(tool_calls=[tool_call("b", "read", filePath="y")]),
        tool_result("b", "contents of y"),
        assistant("done"),
    ])

    roles = [m["role"] for m in converted]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]

    # And each result sits with the call it answers.
    assert converted[2]["content"][0]["tool_use_id"] == "a"
    assert converted[4]["content"][0]["tool_use_id"] == "b"


def test_no_two_turns_of_the_same_role():
    """Rejected by the API. Two assistant turns in a row was the symptom of
    the buffering bug; merging is what makes the fix safe for any input."""
    converted = ap._convert_messages([
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
        assistant("a"),
        assistant("b"),
    ])
    roles = [m["role"] for m in converted]
    assert roles == ["user", "assistant"], roles
    assert not any(a == b for a, b in zip(roles, roles[1:], strict=False))
    # Merged, not dropped.
    assert [b["text"] for b in converted[0]["content"]] == ["one", "two"]
    assert [b["text"] for b in converted[1]["content"]] == ["a", "b"]


def test_parallel_results_share_one_user_turn():
    """A fan-out answers several calls at once; all the results belong to the
    single user turn that follows."""
    converted = ap._convert_messages([
        {"role": "user", "content": "go"},
        assistant(tool_calls=[
            tool_call("a", "read", filePath="x"),
            tool_call("b", "read", filePath="y"),
        ]),
        tool_result("a", "x"),
        tool_result("b", "y"),
    ])
    results = converted[2]["content"]
    assert converted[2]["role"] == "user"
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_an_empty_assistant_turn_is_dropped():
    """Anthropic rejects an assistant turn with no content."""
    converted = ap._convert_messages([
        {"role": "user", "content": "go"},
        assistant(""),
        {"role": "user", "content": "still there?"},
    ])
    assert all(m["content"] for m in converted)


def test_the_conversation_starts_with_a_user_turn():
    converted = ap._convert_messages([
        assistant("leftover from a compaction"),
        {"role": "user", "content": "go"},
    ])
    assert converted[0]["role"] == "user"


def test_system_messages_are_hoisted_not_dropped():
    """Compaction summaries arrive as extra system messages."""
    messages = [
        {"role": "system", "content": "base prompt"},
        {"role": "system", "content": "summary of earlier work"},
        {"role": "user", "content": "go"},
    ]
    assert ap._extract_system(messages) == "base prompt\n\nsummary of earlier work"
    assert all(m["role"] != "system" for m in ap._convert_messages(messages))


def test_a_failed_tool_result_is_marked_as_an_error():
    converted = ap._convert_messages([
        {"role": "user", "content": "go"},
        assistant(tool_calls=[tool_call("a", "bash", command="false")]),
        {"role": "tool", "tool_call_id": "a", "content": "exit 1", "is_error": True},
    ])
    assert converted[2]["content"][0]["is_error"] is True


def test_an_empty_tool_result_still_gets_content():
    """Anthropic rejects an empty tool_result body."""
    converted = ap._convert_messages([
        {"role": "user", "content": "go"},
        assistant(tool_calls=[tool_call("a", "bash", command="true")]),
        tool_result("a", ""),
    ])
    assert converted[2]["content"][0]["content"]


# ── Finish reasons ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("tool_use", "tool_calls"),
    ("max_tokens", "length"),
    ("end_turn", "stop"),
    ("stop_sequence", "stop"),
    ("refusal", "content_filter"),
    (None, "stop"),
    ("tool_calls", "tool_calls"),
    ("length", "length"),
])
def test_stop_reasons_are_translated(raw, expected):
    """Consumers match on the OpenAI vocabulary. Anthropic's `tool_use` meant
    subagents concluded no tool had been requested and returned nothing, and
    `max_tokens` meant the output-limit guard never fired."""
    assert normalize_finish(raw) == expected


# ── Usage ───────────────────────────────────────────────────────────────────

class FakeUsage:
    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)


def test_prompt_tokens_include_the_cached_part():
    """Anthropic reports the input side in three pieces and `input_tokens` is
    only the uncached one. The shared cost function subtracts cached from
    prompt, so left as-is it clamped to zero and billed cache reads as free."""
    usage = ap.blank_usage()
    ap._merge_usage(usage, FakeUsage(
        input_tokens=1_000,
        cache_read_input_tokens=20_000,
        cache_creation_input_tokens=500,
        output_tokens=0,
    ))
    assert usage["prompt_tokens"] == 21_500
    assert usage["cached_tokens"] == 20_000
    assert usage["cache_write_tokens"] == 500


def test_input_tokens_survive_the_later_output_report():
    """input_tokens arrives on message_start and output_tokens on
    message_delta. Reading only the second left prompt_tokens at zero on every
    request, so nothing anchored the cache prediction."""
    usage = ap.blank_usage()
    ap._merge_usage(usage, FakeUsage(input_tokens=5_000, output_tokens=0))
    ap._merge_usage(usage, FakeUsage(input_tokens=0, output_tokens=300))
    assert usage["prompt_tokens"] == 5_000
    assert usage["completion_tokens"] == 300


# ── Thinking and effort ─────────────────────────────────────────────────────

def test_adaptive_models_never_get_a_token_budget():
    """Opus 5 and Sonnet 5 reject `thinking: {type: enabled}` outright."""
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5"):
        kwargs = ap._thinking_kwargs(model, "high", 128_000)
        assert kwargs["thinking"]["type"] == "adaptive"
        assert "budget_tokens" not in kwargs["thinking"]


def test_effort_is_nested_under_output_config():
    """Sent at the top level it is silently ignored."""
    kwargs = ap._thinking_kwargs("claude-opus-5", "xhigh", 128_000)
    assert kwargs["output_config"] == {"effort": "xhigh"}
    assert "effort" not in kwargs


def test_haiku_gets_the_manual_form():
    """It is the one current model that takes extended thinking."""
    kwargs = ap._thinking_kwargs("claude-haiku-4-5", "high", 64_000)
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 20_000}
    assert "output_config" not in kwargs


def test_a_budget_always_leaves_room_for_the_answer():
    kwargs = ap._thinking_kwargs("claude-haiku-4-5", "max", 8_192)
    assert kwargs["thinking"]["budget_tokens"] < 8_192


def test_thinking_is_not_disabled_where_that_is_a_400():
    """Opus 5 rejects `thinking: disabled` at xhigh and max; Fable rejects it
    at any effort."""
    assert ap._thinking_kwargs("claude-opus-5", "none", 128_000)["thinking"]["type"] == "disabled"
    assert ap._thinking_kwargs("claude-fable-5", "none", 128_000)["thinking"]["type"] == "adaptive"


def test_an_unknown_anthropic_model_gets_no_thinking_parameters():
    """Guessing at a parameter a model may reject turns an unknown model into
    a hard failure instead of a working request with default behaviour."""
    assert ap._thinking_kwargs("claude-something-7", "high", 8_192) == {}


# ── Model data ──────────────────────────────────────────────────────────────

def test_output_ceilings_match_the_published_limits():
    """8192 was hardcoded for every model, cutting Opus off at a sixteenth of
    what it can produce -- silently, because `max_tokens` was not translated."""
    from agent_server.config import model_info

    assert model_info("claude-opus-5")["max_output"] == 128_000
    assert model_info("claude-haiku-4-5")["max_output"] == 64_000


def test_a_cache_read_is_cheaper_than_a_miss():
    from agent_server.config import MODELS

    for model in MODELS:
        assert model["price_in_hit"] < model["price_in_miss"], model["id"]
