"""What stops an edit landing somewhere it should not.

`edit` matches on the text itself. That choice is about which way it fails: a
string that does not match produces a loud error and writes nothing, where a
line number that is wrong writes to the wrong place and says it succeeded. The
first costs a retry; the second costs a corrupted file and an afternoon.

It replaced a scheme where `read` printed a `[path#tag]` fingerprint and `edit`
took that tag with a line range. The tag genuinely proved the file had not
moved -- but it never proved the model was *aiming* at the right lines, which is
the failure that actually happened, and it charged the model a running tax to
use: carry the current tag, respect a window, and work out how far the lines
below its last edit had shifted, all while writing the code.

The one thing that scheme had which bare string matching does not is the
seen-lines guarantee: matching text says where an edit lands, not that anyone
looked at it. That is kept, checked against the span the match covers.
"""

import asyncio

import pytest

from agent_server.tools.base import ToolContext
from agent_server.tools.file_ops import (
    clear_read_cache,
    edit_file,
    fingerprint,
    read_file,
    write_file,
)

SAMPLE = """def one():
    return 1

def two():
    return 2

def three():
    return 3
"""


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text(SAMPLE)
    clear_read_cache()
    ctx = ToolContext(session_id="s", project_dir=str(tmp_path), abort=asyncio.Event())
    return ctx, path


async def read(ctx, path, **kw):
    return await read_file(ctx, filePath=str(path), **kw)


# ── the read format ──────────────────────────────────────────────────────────

async def test_read_prints_plain_numbered_lines(workspace):
    """No header, no per-line hash, nothing to pass back."""
    ctx, path = workspace
    out = (await read(ctx, path)).output

    assert out.splitlines()[0] == "1: def one():"
    assert "#" not in out.splitlines()[0]
    assert "|" not in out


async def test_the_read_format_costs_nothing_per_line(workspace):
    ctx, path = workspace
    big = path.parent / "big.py"
    big.write_text("".join(f"line {i}\n" for i in range(2000)))

    out = (await read(ctx, big)).output
    per_line_equivalent = out + "\n".join("a1b2|" for _ in range(2000))
    assert len(per_line_equivalent) - len(out) > 8000


# ── the file must have been read ─────────────────────────────────────────────

async def test_editing_without_reading_at_all_is_refused(workspace):
    ctx, path = workspace
    result = await edit_file(
        ctx, filePath=str(path), oldString="def one():", newString="def uno():"
    )
    assert result.is_error
    assert "have not read" in result.output
    assert path.read_text() == SAMPLE


async def test_one_read_is_enough_for_repeated_edits(workspace):
    """Re-reading before every edit is a round trip and a full file of tokens."""
    ctx, path = workspace
    await read(ctx, path)

    first = await edit_file(
        ctx, filePath=str(path), oldString="return 1", newString="return 11"
    )
    assert not first.is_error, first.output
    second = await edit_file(
        ctx, filePath=str(path), oldString="return 2", newString="return 22"
    )
    assert not second.is_error, second.output
    third = await write_file(ctx, filePath=str(path), content="print('done')\n")
    assert not third.is_error, third.output


# ── matching ─────────────────────────────────────────────────────────────────

async def test_a_string_that_is_not_there_writes_nothing_and_says_why(workspace):
    """The good failure. Nothing is guessed and nothing is touched."""
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path), oldString="def four():", newString="x"
    )
    assert result.is_error
    assert "not found" in result.output
    assert "indentation" in result.output, "whitespace is the usual cause; say so"
    assert path.read_text() == SAMPLE


async def test_a_stale_reading_is_named_as_such(workspace):
    """Same symptom, different fix: re-read, rather than look harder at the
    text you have."""
    ctx, path = workspace
    await read(ctx, path)
    path.write_text(SAMPLE.replace("def one():", "def uno():"))

    result = await edit_file(
        ctx, filePath=str(path), oldString="def one():", newString="def ONE():"
    )
    assert result.is_error
    assert "changed on disk since you read it" in result.output
    assert "Re-read" in result.output


async def test_an_ambiguous_match_is_refused_rather_than_guessed(workspace):
    ctx, path = workspace
    path.write_text("x = 1\ny = 2\nx = 1\n")
    await read(ctx, path)

    result = await edit_file(ctx, filePath=str(path), oldString="x = 1", newString="x = 9")
    assert result.is_error
    assert "2 occurrences" in result.output
    assert path.read_text() == "x = 1\ny = 2\nx = 1\n"


async def test_replace_all_takes_every_occurrence(workspace):
    ctx, path = workspace
    path.write_text("x = 1\ny = 2\nx = 1\n")
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path), oldString="x = 1", newString="x = 9", replaceAll=True
    )
    assert not result.is_error, result.output
    assert path.read_text() == "x = 9\ny = 2\nx = 9\n"


async def test_an_empty_new_string_deletes(workspace):
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path), oldString="def three():\n    return 3\n", newString=""
    )
    assert not result.is_error, result.output
    assert "def three():" not in path.read_text()
    assert "def one():" in path.read_text()


async def test_a_multi_line_replacement_lands_whole(workspace):
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path),
        oldString="def two():\n    return 2",
        newString="def two():\n    x = 1\n    return x + 1",
    )
    assert not result.is_error, result.output
    assert "return x + 1" in path.read_text()
    assert "def three():" in path.read_text()


# ── the seen-lines guarantee ─────────────────────────────────────────────────

async def test_a_line_that_was_never_displayed_cannot_be_edited(workspace):
    """The guarantee carried over from the tag scheme. Matching text proves
    *where* an edit lands, not that anybody looked at it: reading 50 lines of a
    400-line file and replacing a string that happens to sit at line 300 is
    still editing blind.
    """
    ctx, path = workspace
    big = path.parent / "big.py"
    big.write_text("".join(f"line {i}\n" for i in range(1, 401)))

    await read(ctx, big, limit=50)
    result = await edit_file(
        ctx, filePath=str(big), oldString="line 300", newString="tampered"
    )

    assert result.is_error
    assert "never shown to you" in result.output
    assert "offset=300" in result.output, "say how to fix it"
    assert "line 300" in big.read_text(), "the file must be untouched"


async def test_reading_the_rest_of_the_file_makes_those_lines_editable(workspace):
    ctx, path = workspace
    big = path.parent / "big.py"
    big.write_text("".join(f"line {i}\n" for i in range(1, 401)))

    await read(ctx, big, limit=50)
    await read(ctx, big, offset=280, limit=50)

    result = await edit_file(
        ctx, filePath=str(big), oldString="line 300", newString="changed"
    )
    assert not result.is_error, result.output
    assert "changed" in big.read_text()


async def test_replace_all_checks_every_occurrence_not_just_the_first(workspace):
    """One match inside the window and one outside it is still a blind edit."""
    ctx, path = workspace
    big = path.parent / "big.py"
    lines = [f"line {i}\n" for i in range(1, 401)]
    lines[4] = "TARGET\n"
    lines[299] = "TARGET\n"
    big.write_text("".join(lines))

    await read(ctx, big, limit=50)
    result = await edit_file(
        ctx, filePath=str(big), oldString="TARGET", newString="done", replaceAll=True
    )
    assert result.is_error
    assert "never shown to you" in result.output
    assert big.read_text().count("TARGET") == 2


async def test_a_write_cannot_discard_the_part_of_a_file_that_was_never_shown(workspace):
    ctx, path = workspace
    big = path.parent / "big.py"
    big.write_text("".join(f"line {i}\n" for i in range(1, 401)))

    await read(ctx, big, limit=50)
    result = await write_file(ctx, filePath=str(big), content="tiny\n")

    assert result.is_error
    assert "never displayed" in result.output
    assert "line 400" in big.read_text()


async def test_reading_all_of_a_file_permits_writing_it(workspace):
    ctx, path = workspace
    await read(ctx, path)
    result = await write_file(ctx, filePath=str(path), content="print('new')\n")
    assert not result.is_error, result.output
    assert path.read_text() == "print('new')\n"


async def test_an_empty_file_counts_as_read(workspace):
    ctx, path = workspace
    blank = path.parent / "blank.py"
    blank.write_text("")

    await read(ctx, blank)
    result = await write_file(ctx, filePath=str(blank), content="x = 1\n")
    assert not result.is_error, result.output
    assert blank.read_text() == "x = 1\n"


# ── seeing where it landed ───────────────────────────────────────────────────

async def test_an_edit_shows_where_the_text_actually_landed(workspace):
    """The diff never reaches the model, so without this the model finds out
    what it did on the next read."""
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path), oldString="    return 2", newString="    return 22"
    )
    assert not result.is_error, result.output
    assert "+ 5:     return 22" in result.output
    assert " 4: def two():" in result.output
    assert " 7: def three():" in result.output


async def test_the_echoed_numbers_are_the_ones_after_the_edit(workspace):
    """So a following edit never has to work out how far things shifted."""
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path),
        oldString="    return 1",
        newString="    x = 1\n    y = 2\n    return x + y",
    )
    assert not result.is_error, result.output
    assert "+ 2:     x = 1" in result.output
    assert "+ 4:     return x + y" in result.output
    # The window ends three lines past the change, and those numbers are the
    # post-edit ones: `    return 2` was line 5 and prints as 7.
    assert " 7:     return 2" in result.output
    assert path.read_text().splitlines()[8] == "def three():"


async def test_a_deletion_does_not_claim_the_line_that_moved_up_was_seen(workspace):
    ctx, path = workspace
    big = path.parent / "big.py"
    big.write_text("".join(f"line {i}\n" for i in range(1, 401)))

    await read(ctx, big, limit=50)
    deleted = await edit_file(
        ctx, filePath=str(big),
        oldString="".join(f"line {i}\n" for i in range(1, 41)),
        newString="",
    )
    assert not deleted.is_error, deleted.output

    # Line 41 is line 1 now. Anything past the echoed window is still unread.
    result = await edit_file(
        ctx, filePath=str(big), oldString="line 200", newString="tampered"
    )
    assert result.is_error
    assert "never shown to you" in result.output


# ── plumbing ─────────────────────────────────────────────────────────────────

def test_the_fingerprint_ignores_line_endings_and_trailing_space():
    """Internal only -- the model never sees or passes this. It exists so
    "the file changed underneath you" can be told apart from "your text does
    not match", because the fix differs."""
    assert fingerprint("a\nb\n") == fingerprint("a\r\nb\r\n")
    assert fingerprint("a  \nb\t\n") == fingerprint("a\nb\n")
    assert fingerprint("a\nb\n") != fingerprint("a\nB\n")


async def test_tools_report_the_file_they_acted_on(workspace):
    """The UI opens this path when the block is clicked. It cannot come from the
    title, which is left-truncated for display once it passes 60 characters."""
    ctx, path = workspace
    assert (await read(ctx, path)).file_path == str(path)

    edited = await edit_file(
        ctx, filePath=str(path), oldString="return 1", newString="return 9"
    )
    assert edited.file_path == str(path)
    assert (await write_file(ctx, filePath=str(path), content="x\n")).file_path == str(path)


async def test_a_deep_path_is_still_reported_in_full(workspace):
    ctx, path = workspace
    deep = path.parent.joinpath(*[f"directory{i}" for i in range(8)]) / "module.py"
    deep.parent.mkdir(parents=True)
    deep.write_text("x = 1\n")

    result = await read(ctx, deep)
    assert len(result.title) < len(str(deep)), "the title is truncated for display"
    assert result.file_path == str(deep)


# ── truncation and token estimation ──────────────────────────────────────────

def test_truncation_says_what_to_do_not_just_what_happened():
    """Told only that output was cut, a model re-runs the call and pays twice."""
    from agent_server.tools.base import truncate

    out = truncate("x" * 5000, 300, "grep", spill=True)
    assert len(out) <= 300
    assert "tool-output" in out, "the spill path must be named"
    assert "task" in out and "grep" in out
    assert "Do not re-run" in out


def test_truncation_without_a_spill_promises_no_file():
    from agent_server.tools.base import truncate

    out = truncate("x" * 5000, 300, "grep", spill=False)
    assert "tool-output" not in out


def test_the_token_estimate_learns_from_real_usage():
    """chars/4 is 25% low on code, and compaction depends on it."""
    from agent_server.providers import base

    base._ratios.clear()
    messages = [{"role": "user", "content": "def f(x):\n    return x * 2\n" * 40}]
    chars = base.message_chars(messages)

    assert base.estimate_tokens(messages, "m") == int(chars / 4.0)

    real = int(chars / 3.0)  # what the provider actually billed
    for _ in range(8):
        base.observe_usage("m", chars, real)

    assert abs(base.estimate_tokens(messages, "m") - real) < real * 0.05
    base._ratios.clear()


def test_a_nonsense_measurement_is_ignored():
    """One odd turn must not move the estimate for every later one."""
    from agent_server.providers import base

    base._ratios.clear()
    base.observe_usage("m", 1000, 0)        # no tokens reported
    base.observe_usage("m", 0, 1000)        # no characters
    base.observe_usage("m", 1000, 1)        # 1000 chars/token: not a tokenizer
    assert base.chars_per_token("m") == 4.0
    base._ratios.clear()


def test_each_model_is_calibrated_separately():
    from agent_server.providers import base

    base._ratios.clear()
    for _ in range(8):
        base.observe_usage("dense", 3000, 1000)
    assert base.chars_per_token("dense") < 3.5
    assert base.chars_per_token("untouched") == 4.0
    base._ratios.clear()


# ── conversations that predate the change ────────────────────────────────────

async def test_an_old_style_call_is_answered_by_name(workspace):
    """A transcript is the strongest few-shot prompt there is. A conversation
    started before this change is full of `edit` calls carrying a tag and a line
    range, and a model reading its own history will keep making them however
    clear the schema is. "oldString is required" is true but reads as though the
    call was malformed, and invites the same call again with a guess bolted on."""
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path), tag="a3f9", startLine=2, newText="    return 111"
    )

    assert result.is_error
    assert "no longer takes" in result.output
    assert "tag" in result.output and "startLine" in result.output and "newText" in result.output
    assert "oldString" in result.output, "say what to do instead"
    assert "ignore them" in result.output, "and that the old calls above were fine"
    assert path.read_text() == SAMPLE, "the file must be untouched"


async def test_a_stray_old_argument_alongside_a_real_one_is_not_hijacked(workspace):
    """A correct call carrying a leftover `tag` should just work. The migration
    notice is for a call that has *only* the old shape to go on."""
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path), oldString="return 1", newString="return 111", tag="a3f9"
    )
    assert not result.is_error, result.output
    assert "return 111" in path.read_text()


async def test_a_genuinely_empty_call_still_says_what_is_missing(workspace):
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(ctx, filePath=str(path))
    assert result.is_error
    assert "oldString is required" in result.output
