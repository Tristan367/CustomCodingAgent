"""Two faults that only a real request could find, pinned so they cannot return.

Both providers were covered by unit tests and both were completely broken. The
tests drove `_build_kwargs` and a fabricated stream *separately*, so nothing ever
put the two together, and nothing ever fed a tool result back for a second
round. That is the shape of gap live traffic finds and fabricated responses
cannot: the faults were in the joins, not in the pieces.

  * Anthropic passed `stream=True` into `messages.stream()`, which is already a
    streaming context manager. Every turn died with a TypeError before a request
    left the machine, so this provider had never once worked.

  * Gemini attaches a `thought_signature` to every function call and rejects the
    *next* request if it does not come back. The app rebuilt tool calls from its
    own canonical shape and dropped it, so the first tool call succeeded and the
    round after it failed with a 400 that named nothing recognisable.
"""

import inspect
import json

from agent_server.conversation import VENDOR_CALL_KEYS, normalize_tool_calls
from agent_server.providers.anthropic import AnthropicProvider

# ── Anthropic: the streaming call is already streaming ───────────────────────

def test_anthropic_does_not_pass_stream_into_the_stream_helper():
    kwargs = AnthropicProvider()._build_kwargs(
        [{"role": "user", "content": "hi"}], [], "claude-haiku-4-5", None
    )
    assert "stream" not in kwargs, (
        "messages.stream() opens the stream itself; the flag is a TypeError"
    )


def test_the_kwargs_are_accepted_by_the_signature_they_are_passed_to():
    """The check the split unit tests could not make: build the arguments and
    hold them against the method that actually receives them."""
    import anthropic

    kwargs = AnthropicProvider()._build_kwargs(
        [{"role": "user", "content": "hi"}], [{"function": {
            "name": "read", "description": "d", "parameters": {"type": "object"},
        }}], "claude-haiku-4-5", None
    )
    signature = inspect.signature(anthropic.AsyncAnthropic().messages.stream)
    allowed = set(signature.parameters)
    unexpected = [k for k in kwargs if k not in allowed]
    assert not unexpected, f"messages.stream() would reject: {unexpected}"


def test_anthropic_still_sends_what_it_needs_to():
    kwargs = AnthropicProvider()._build_kwargs(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        [], "claude-haiku-4-5", None,
    )
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["max_tokens"] > 0
    assert kwargs["messages"], "the conversation went missing"
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}, (
        "the cache breakpoint is what makes cache_read_input_tokens non-zero"
    )


# ── Gemini: the signature has to come back ───────────────────────────────────

SIGNATURE = {"google": {"thought_signature": "EuABCt0BARFNMg8Yjn"}}


def test_a_vendor_field_survives_being_rebuilt():
    """Streaming hands it over as `extra`; the rebuilt call must carry it."""
    calls = normalize_tool_calls([{
        "id": "call_1", "name": "glob", "arguments": '{"pattern":"*"}',
        "extra": {"extra_content": SIGNATURE},
    }])
    assert calls[0]["extra_content"] == SIGNATURE


def test_a_vendor_field_survives_a_round_trip_through_the_database():
    """Stored as JSON and read back, which is what happens between rounds."""
    first = normalize_tool_calls([{
        "id": "call_1", "name": "glob", "arguments": "{}",
        "extra": {"extra_content": SIGNATURE},
    }])
    reloaded = normalize_tool_calls(json.loads(json.dumps(first)))
    assert reloaded[0]["extra_content"] == SIGNATURE
    assert reloaded[0]["function"] == {"name": "glob", "arguments": "{}"}


def test_the_canonical_shape_is_unchanged_without_vendor_fields():
    """Every other provider must see exactly what it saw before."""
    calls = normalize_tool_calls([{
        "id": "call_1", "name": "read", "arguments": '{"path":"a"}',
    }])
    assert calls == [{
        "id": "call_1", "type": "function",
        "function": {"name": "read", "arguments": '{"path":"a"}'},
    }]


def test_only_known_vendor_keys_are_carried():
    """The list is explicit rather than "anything that is not id/type/function",
    so a stray column in an old row cannot be smuggled into a live request."""
    calls = normalize_tool_calls([{
        "id": "call_1", "name": "read", "arguments": "{}",
        "extra_content": SIGNATURE, "_hidden": "junk", "role": "assistant",
    }])
    assert calls[0]["extra_content"] == SIGNATURE
    assert "_hidden" not in calls[0]
    assert "role" not in calls[0]
    assert "extra_content" in VENDOR_CALL_KEYS


def test_a_vendor_field_cannot_overwrite_the_call_itself():
    calls = normalize_tool_calls([{
        "id": "call_1", "name": "read", "arguments": "{}",
        "extra": {"id": "spoofed", "function": {"name": "bash"}, "extra_content": SIGNATURE},
    }])
    assert calls[0]["id"] == "call_1"
    assert calls[0]["function"]["name"] == "read"
    assert calls[0]["extra_content"] == SIGNATURE


# ── Unindexed fragments, which Gemini also sends ─────────────────────────────

def test_two_unindexed_calls_do_not_collapse_into_one():
    """Gemini sends no `index`. Defaulting it to 0 put every call in one slot,
    so a turn asking for two tools ran only the second."""
    from agent_server.agent import _accumulate

    partials: dict = {}
    _accumulate(partials, [{"index": None, "id": "a", "name": "read", "arguments": "{}"}])
    _accumulate(partials, [{"index": None, "id": "b", "name": "glob", "arguments": "{}"}])
    assert len(partials) == 2, "two calls were merged into one"
    assert {p["name"] for p in partials.values()} == {"read", "glob"}


def test_indexed_fragments_still_assemble_normally():
    from agent_server.agent import _accumulate

    partials: dict = {}
    _accumulate(partials, [{"index": 0, "id": "a", "name": "read", "arguments": '{"pa'}])
    _accumulate(partials, [{"index": 0, "arguments": 'th":"x"}'}])
    assert len(partials) == 1
    assert partials[0]["arguments"] == '{"path":"x"}'
