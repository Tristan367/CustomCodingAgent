"""Compaction has to fit the threshold it is compacting into.

The kept-tail budget was a flat 24,000 tokens, tuned for the default threshold
of 750K where keeping 24K verbatim and summarising the other 726K is obviously
right. But the threshold is a slider the user can drag to 4K, and at any setting
near or below 24K the tail swallowed the entire conversation: compaction
summarised the single oldest round, freed nothing, and -- because the check runs
at every turn boundary -- fired again on the next round, and the next.

Measured against a real session at a 6,600 threshold before the fix: it
compacted 1 message and kept 12, the "summary" was the word `OK.`, the context
went *up* from 6,834 to 7,468, and a fact planted in the first message was
destroyed. After: 11 compacted, 2 kept, 2,617 tokens summarised into 566, the
context fell to 6,131, and the fact survived.
"""

import pytest

from agent_server.compaction import (
    KEEP_TAIL_FLOOR,
    KEEP_TAIL_TOKENS,
    split_for_compaction,
    tail_budget,
)


def _rows(count: int, tokens: int = 500) -> list[dict]:
    """A plain alternating conversation, every message costed."""
    rows = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        rows.append({
            "id": i + 1, "role": role, "content": f"message {i}",
            "tool_calls": None, "token_count": tokens,
        })
    return rows


# ── The budget itself ────────────────────────────────────────────────────────

@pytest.mark.parametrize("threshold,expected", [
    (750_000, KEEP_TAIL_TOKENS),   # the default: unchanged behaviour
    (1_000_000, KEEP_TAIL_TOKENS),
    (60_000, KEEP_TAIL_TOKENS),    # still above the flat cap
    (16_384, 6_553),
    (8_192, 3_276),
    (6_600, 2_640),
])
def test_the_tail_budget_follows_the_threshold(threshold, expected):
    assert tail_budget(threshold) == expected


def test_a_small_threshold_still_keeps_something_verbatim():
    """Below the floor there is not enough recent context for the next request
    to be coherent, so the floor holds rather than going to zero."""
    assert tail_budget(4096) == KEEP_TAIL_FLOOR
    assert tail_budget(1) == KEEP_TAIL_FLOOR


def test_an_unset_threshold_falls_back_to_the_flat_budget():
    assert tail_budget(0) == KEEP_TAIL_TOKENS


def test_the_budget_never_exceeds_the_threshold():
    """A tail bigger than the whole window is what caused this: it left nothing
    above it to summarise."""
    for threshold in (5_000, 8_192, 16_384, 40_000, 100_000, 750_000):
        assert tail_budget(threshold) <= threshold


# ── What the split actually does with it ─────────────────────────────────────

def test_a_small_threshold_summarises_most_of_the_conversation():
    """The failure this exists to stop: 1 message summarised, 12 kept."""
    rows = _rows(26, 500)
    to_compact, kept = split_for_compaction(rows, tail_budget(6_600))
    assert len(to_compact) > len(kept), (
        f"compacted {len(to_compact)} and kept {len(kept)}: the tail ate the conversation"
    )
    assert sum(r["token_count"] for r in kept) <= tail_budget(6_600) + 500


def test_the_default_threshold_behaves_exactly_as_before():
    """The daily-driver path must be untouched by this change."""
    rows = _rows(200, 500)
    before = split_for_compaction(rows, KEEP_TAIL_TOKENS)
    after = split_for_compaction(rows, tail_budget(750_000))
    assert [r["id"] for r in before[0]] == [r["id"] for r in after[0]]
    assert [r["id"] for r in before[1]] == [r["id"] for r in after[1]]


def test_compaction_frees_more_than_it_keeps_at_a_low_threshold():
    rows = _rows(40, 400)
    to_compact, kept = split_for_compaction(rows, tail_budget(8_192))
    freed = sum(r["token_count"] for r in to_compact)
    held = sum(r["token_count"] for r in kept)
    assert freed > held, f"freed {freed} but held {held}"


def test_a_conversation_well_under_the_budget_keeps_almost_everything():
    """The split always leaves one unit to summarise by design, so the check is
    that a short conversation is kept essentially whole rather than untouched.
    In practice it is never reached: compaction only runs once the context is
    over the threshold, which a conversation this size cannot be."""
    rows = _rows(4, 100)
    to_compact, kept = split_for_compaction(rows, tail_budget(750_000))
    assert len(kept) >= len(rows) - 1
    assert len(to_compact) <= 1


def test_a_conversation_of_two_units_is_left_completely_alone():
    rows = _rows(2, 100)
    to_compact, kept = split_for_compaction(rows, tail_budget(750_000))
    assert to_compact == []
    assert kept == rows


def test_the_split_still_lands_on_a_unit_boundary():
    """A kept window must never begin with an orphaned tool result."""
    rows = [
        {"id": 1, "role": "user", "content": "do it", "tool_calls": None, "token_count": 100},
        {"id": 2, "role": "assistant", "content": "", "token_count": 100,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read", "arguments": "{}"}}]},
        {"id": 3, "role": "tool", "content": "result", "tool_calls": None, "token_count": 900},
        {"id": 4, "role": "assistant", "content": "done", "tool_calls": None, "token_count": 100},
        {"id": 5, "role": "user", "content": "again", "tool_calls": None, "token_count": 100},
        {"id": 6, "role": "assistant", "content": "sure", "tool_calls": None, "token_count": 100},
    ]
    _to_compact, kept = split_for_compaction(rows, 300)
    assert not kept or kept[0]["role"] != "tool", "kept window starts on an orphaned tool result"
