"""Edits must land on the lines that were actually read.

This replaces a per-line hash scheme. That scheme hashed each line's content
alone, so every blank line shared a hash, as did every `}` and every
`    return`, and it answered the wrong question: the risk is not "did line 40
change" but "did the file shift so that line 40 is now something else" -- which
a duplicate line elsewhere satisfies. It also cost about six characters on
every line of every read, roughly 2,500 tokens on a 2,000-line file.

One tag per file replaces it. Any change anywhere invalidates it, which is the
conservative answer, and the error says to re-read.
"""

import asyncio

import pytest

from agent_server.tools.base import ToolContext
from agent_server.tools.file_ops import _snapshots, edit_file, file_tag, read_file

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
    _snapshots.clear()
    ctx = ToolContext(session_id="s", project_dir=str(tmp_path), abort=asyncio.Event())
    return ctx, path


async def read(ctx, path, **kw):
    return await read_file(ctx, filePath=str(path), **kw)


def tag_of(result) -> str:
    return result.output.split("#", 1)[1].split("]", 1)[0]


# ── the format itself ────────────────────────────────────────────────────────

def test_the_tag_ignores_line_endings_and_trailing_space():
    """A CRLF file, or one the reader trimmed, must not read as changed."""
    assert file_tag("a\nb\n") == file_tag("a\r\nb\r\n")
    assert file_tag("a  \nb\t\n") == file_tag("a\nb\n")
    assert file_tag("a\nb\n") != file_tag("a\nB\n")


async def test_read_prints_one_tag_and_plain_line_numbers(workspace):
    ctx, path = workspace
    out = (await read(ctx, path)).output

    assert out.splitlines()[0] == f"[sample.py#{file_tag(SAMPLE)}]"
    assert "1: def one():" in out
    # The old format spent six characters a line on a per-line hash.
    assert "|" not in out


async def test_the_format_is_much_cheaper_than_per_line_hashes(workspace):
    ctx, path = workspace
    big = path.parent / "big.py"
    big.write_text("".join(f"line {i}\n" for i in range(2000)))

    out = (await read(ctx, big)).output
    per_line_equivalent = out + "\n".join("a1b2|" for _ in range(2000))
    saved = len(per_line_equivalent) - len(out)
    assert saved > 8000, f"only {saved} characters saved"


# ── the tag is required, and must be real ────────────────────────────────────

async def test_an_edit_without_a_tag_is_refused(workspace):
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(ctx, filePath=str(path), startLine=2, newText="    return 99")
    assert result.is_error
    assert "no tag given" in result.output
    assert path.read_text() == SAMPLE, "the file must be untouched"


async def test_a_made_up_tag_is_refused_and_says_so(workspace):
    """Distinct from a stale tag: the fix is different, so the message is too."""
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path), tag="ffff", startLine=2, newText="    return 99"
    )
    assert result.is_error
    assert "not a tag this session was given" in result.output
    assert path.read_text() == SAMPLE


async def test_a_tag_from_before_someone_else_edited_is_refused(workspace):
    ctx, path = workspace
    stale = tag_of(await read(ctx, path))
    path.write_text(SAMPLE.replace("def one():", "def uno():"))

    result = await edit_file(
        ctx, filePath=str(path), tag=stale, startLine=2, newText="    return 99"
    )
    assert result.is_error
    assert "since you read it" in result.output
    assert "re-read" in result.output.lower()


async def test_editing_without_reading_at_all_is_refused(workspace):
    ctx, path = workspace
    result = await edit_file(
        ctx, filePath=str(path), tag="abcd", startLine=1, newText="x"
    )
    assert result.is_error
    assert "have not read" in result.output


# ── seen lines ───────────────────────────────────────────────────────────────

async def test_a_line_that_was_never_displayed_cannot_be_edited(workspace):
    """The gap a per-line hash could not have caught.

    Reading the first window of a long file tells the model nothing about line
    900, but nothing stopped it editing there on an inference.
    """
    ctx, path = workspace
    big = path.parent / "big.py"
    big.write_text("".join(f"line {i}\n" for i in range(1, 401)))

    tag = tag_of(await read(ctx, big, limit=50))
    result = await edit_file(
        ctx, filePath=str(big), tag=tag, startLine=300, newText="tampered"
    )

    assert result.is_error
    assert "not shown to you" in result.output
    assert "line 300" in big.read_text(), "the file must be untouched"


async def test_reading_the_rest_of_the_file_makes_those_lines_editable(workspace):
    ctx, path = workspace
    big = path.parent / "big.py"
    big.write_text("".join(f"line {i}\n" for i in range(1, 401)))

    await read(ctx, big, limit=50)
    tag = tag_of(await read(ctx, big, offset=280, limit=50))

    result = await edit_file(
        ctx, filePath=str(big), tag=tag, startLine=300, newText="changed"
    )
    assert not result.is_error, result.output
    assert "changed" in big.read_text()


# ── applying the edit ────────────────────────────────────────────────────────

async def test_a_single_line_is_replaced_in_place(workspace):
    ctx, path = workspace
    tag = tag_of(await read(ctx, path))

    result = await edit_file(
        ctx, filePath=str(path), tag=tag, startLine=2, newText="    return 111"
    )
    assert not result.is_error, result.output
    assert path.read_text() == SAMPLE.replace("    return 1\n", "    return 111\n")


async def test_a_range_covers_exactly_the_named_lines(workspace):
    ctx, path = workspace
    tag = tag_of(await read(ctx, path))

    result = await edit_file(
        ctx, filePath=str(path), tag=tag, startLine=4, endLine=5, newText="def two():\n    return 22"
    )
    assert not result.is_error, result.output
    text = path.read_text()
    assert "return 22" in text
    assert "def one():" in text and "def three():" in text


async def test_a_reversed_range_is_refused_not_swapped(workspace):
    """Swapping them silently deleted a span the caller never named."""
    ctx, path = workspace
    tag = tag_of(await read(ctx, path))

    result = await edit_file(
        ctx, filePath=str(path), tag=tag, startLine=7, endLine=2, newText="x"
    )
    assert result.is_error
    assert "above startLine" in result.output
    assert path.read_text() == SAMPLE


async def test_an_edit_returns_the_new_tag_so_edits_can_chain(workspace):
    """Otherwise every edit costs a re-read of the file it just changed."""
    ctx, path = workspace
    tag = tag_of(await read(ctx, path))

    first = await edit_file(
        ctx, filePath=str(path), tag=tag, startLine=2, newText="    return 111"
    )
    assert not first.is_error
    next_tag = first.output.split("#", 1)[1].split("]", 1)[0]
    assert next_tag == file_tag(path.read_text())
    assert next_tag != tag

    second = await edit_file(
        ctx, filePath=str(path), tag=next_tag, startLine=5, newText="    return 222"
    )
    assert not second.is_error, second.output
    assert "return 111" in path.read_text()
    assert "return 222" in path.read_text()


async def test_lines_shift_correctly_when_a_replacement_is_longer(workspace):
    """The seen set has to move with the text, or the next edit is refused."""
    ctx, path = workspace
    tag = tag_of(await read(ctx, path))

    grown = await edit_file(
        ctx, filePath=str(path), tag=tag, startLine=2, newText="    x = 1\n    y = 2\n    return x + y"
    )
    assert not grown.is_error, grown.output
    assert "+2" in grown.output or "moved by +2" in grown.output

    # `def three():` was line 7, now line 9.
    lines = path.read_text().splitlines()
    assert lines[8] == "def three():"

    follow = await edit_file(
        ctx,
        filePath=str(path),
        tag=grown.output.split("#", 1)[1].split("]", 1)[0],
        startLine=9,
        newText="def tres():",
    )
    assert not follow.is_error, follow.output
    assert "def tres():" in path.read_text()


async def test_a_range_can_be_deleted(workspace):
    ctx, path = workspace
    tag = tag_of(await read(ctx, path))

    result = await edit_file(ctx, filePath=str(path), tag=tag, startLine=1, endLine=3)
    assert not result.is_error, result.output
    assert "def one():" not in path.read_text()
    assert "def two():" in path.read_text()


async def test_string_mode_still_works(workspace):
    """The fallback has to survive; not everything is line-shaped."""
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path), oldString="def three():", newString="def tres():"
    )
    assert not result.is_error, result.output
    assert "def tres():" in path.read_text()


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
