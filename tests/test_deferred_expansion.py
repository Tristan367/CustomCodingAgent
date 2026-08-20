"""A long transcript is painted collapsed and expanded a frame later.

Switching into a session with `edit` set to auto-expand meant laying out every
diff in the history before the page could respond -- 124,000px of them on a
session of 250 edits, and paid again on every tab switch, because switching is
an htmx swap that re-renders the whole transcript.

The server therefore *marks* the blocks the user wants open instead of opening
them, and app.js opens them after the first frame. Measured on that session:
430ms of blocked main thread per tab switch became 217ms, and
DOMContentLoaded went from 569ms to 367ms.

This pins the server half. The browser half -- that they do end up open, and
that opening them moves nothing -- is in test_transcript_anchoring.py.
"""

import re

import pytest

from agent_server.templating import templates


def _render(messages, expand_tools):
    return templates.env.get_template("chat_messages.html").render(
        messages=messages, compactions=[], tool_inputs={},
        expand_tools=expand_tools, pending=None,
    )


def _edit(i=1):
    return {
        "id": i, "role": "tool", "tool_name": "edit", "tool_call_id": f"c{i}",
        "tool_title": f"edit module_{i}.py", "content": "edited",
        "diff": "@@ -1,2 +1,2 @@\n-old\n+new\n", "lang": "python",
        "is_error": False, "duration_ms": 10, "file_path": f"/tmp/module_{i}.py",
        "created_at": "2026-08-19T10:00:00+00:00", "code": "", "code_start": 1,
    }


def _read(i=2):
    return {
        "id": i, "role": "tool", "tool_name": "read", "tool_call_id": f"c{i}",
        "tool_title": f"read module_{i}.py", "content": "line\nline\n",
        "diff": "", "lang": "", "is_error": False, "duration_ms": 5,
        "file_path": "", "created_at": "2026-08-19T10:00:00+00:00",
        "code": "", "code_start": 1,
    }


def test_an_auto_expanded_block_is_marked_not_opened():
    """`open` in the markup is what put the diff in the first layout pass."""
    html = _render([_edit()], ["edit"])
    assert 'data-expand="1"' in html
    assert not re.search(r"<details[^>]*\sopen[\s>]", html), (
        "a block shipped open, which is what the deferral exists to avoid"
    )


def test_a_block_the_user_does_not_auto_expand_is_not_marked():
    html = _render([_read()], ["edit"])
    assert "data-expand" not in html


def test_the_marking_follows_the_setting():
    """Whatever the user chose to auto-expand is what gets marked, so the
    deferral cannot quietly change which blocks end up open."""
    both = _render([_edit(1), _read(2)], ["edit", "read"])
    assert both.count('data-expand="1"') == 2
    neither = _render([_edit(1), _read(2)], [])
    assert "data-expand" not in neither


def test_a_thinking_block_obeys_the_same_rule():
    message = {
        "id": 3, "role": "assistant", "content": "done",
        "reasoning_content": "thinking " * 50, "tool_calls": None,
        "created_at": "2026-08-19T10:00:00+00:00", "mail_from": None,
    }
    marked = _render([message], ["reasoning"])
    assert 'data-expand="1"' in marked
    assert not re.search(r"<details[^>]*\sopen[\s>]", marked)


@pytest.mark.parametrize("count", [1, 5, 25])
def test_every_marked_block_is_still_fully_rendered(count):
    """Deferring the *opening* must not defer the content: the diff is in the
    DOM either way, which is what keeps find-in-page and the anchoring work
    intact. Only the layout of it is postponed."""
    html = _render([_edit(i) for i in range(count)], ["edit"])
    assert html.count('data-expand="1"') == count
    assert html.count("diff-block") == count
    # The added and removed lines, which the diff filter renders as classed
    # rows rather than as leading +/- characters.
    assert html.count('class="row diff-add"') == count
    assert html.count('class="row diff-del"') == count
