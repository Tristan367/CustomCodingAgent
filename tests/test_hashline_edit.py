"""Hashline edits must land on the line that was read.

The hash is of a line's content alone, so every blank line in a file shares
one, as does every `}` and every `    return`. Resolution returned the *first*
match, so an anchor on a repeated line silently rewrote the first occurrence in
the file instead of the one the model had in front of it -- and a `hashEnd`
that resolved above `hashStart` had the bounds swapped, deleting a span nobody
had named. Both are silent data loss in the primary editing path.
"""

import asyncio

import pytest

from agent_server.tools.base import ToolContext
from agent_server.tools.file_ops import _hash_line, edit_file, read_file

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
    ctx = ToolContext(
        session_id="s", project_dir=str(tmp_path), abort=asyncio.Event()
    )
    return ctx, path


async def read(ctx, path):
    return await read_file(ctx, filePath=str(path))


def test_identical_lines_share_a_hash():
    """The premise. Without this the rest of the file is testing nothing."""
    assert _hash_line("") == _hash_line("")
    assert _hash_line("    return 1") != _hash_line("    return 2")
    blanks = [i for i, line in enumerate(SAMPLE.splitlines()) if not line]
    assert len(blanks) > 1


async def test_an_ambiguous_hash_is_refused(workspace):
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path), hashStart=_hash_line(""), newText="# here"
    )

    assert result.is_error
    assert "ambiguous" in result.output or "identical lines" in result.output
    assert path.read_text() == SAMPLE, "the file was modified by a refused edit"


async def test_a_line_number_resolves_the_ambiguity(workspace):
    """Line 3 and line 6 are both blank. Naming one must edit that one."""
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path),
        hashStart=_hash_line(""), startLine=6,
        newText="# between two and three",
    )

    assert not result.is_error, result.output
    lines = path.read_text().splitlines()
    assert lines[2] == "", "the wrong blank line was edited"
    assert lines[5] == "# between two and three"


async def test_a_unique_hash_still_needs_no_line_number(workspace):
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path),
        hashStart=_hash_line("    return 2"), newText="    return 22",
    )

    assert not result.is_error, result.output
    assert "    return 22" in path.read_text()
    assert "    return 1" in path.read_text()


async def test_a_reversed_range_is_refused_not_swapped(workspace):
    """Swapping meant deleting a span the caller never named."""
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path),
        hashStart=_hash_line("    return 3"), startLine=8,
        hashEnd=_hash_line("    return 1"), endLine=2,
        newText="gone",
    )

    assert result.is_error
    assert "order" in result.output
    assert path.read_text() == SAMPLE


async def test_a_hash_that_no_longer_matches_is_refused(workspace):
    ctx, path = workspace
    await read(ctx, path)
    path.write_text(SAMPLE.replace("    return 2", "    return 999"))

    result = await edit_file(
        ctx, filePath=str(path),
        hashStart=_hash_line("    return 2"), startLine=5, newText="x",
    )

    assert result.is_error
    assert "read it again" in result.output


async def test_a_line_number_shifted_by_an_earlier_edit_still_resolves(workspace):
    """Inserting above moves everything down. Re-reading the whole file to
    recover four characters that have not changed is pure waste, so a nearby
    match within the drift window is accepted."""
    ctx, path = workspace
    await read(ctx, path)
    path.write_text("# a new header line\n" + SAMPLE)

    result = await edit_file(
        ctx, filePath=str(path),
        hashStart=_hash_line("    return 3"), startLine=8, newText="    return 33",
    )

    assert not result.is_error, result.output
    assert "    return 33" in path.read_text()
    assert "# a new header line" in path.read_text()


async def test_a_range_replacement_covers_exactly_the_named_lines(workspace):
    ctx, path = workspace
    await read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path),
        hashStart=_hash_line("def two():"), startLine=4,
        hashEnd=_hash_line("    return 2"), endLine=5,
        newText="def two():\n    return 'two'",
    )

    assert not result.is_error, result.output
    text = path.read_text()
    assert "    return 'two'" in text
    assert "def one():" in text and "def three():" in text


async def test_read_prints_the_line_number_and_hash_together(workspace):
    """The model has to be able to read both off one line of output."""
    ctx, path = workspace
    result = await read(ctx, path)
    first = result.output.splitlines()[0]
    assert first.startswith(f"1|{_hash_line('def one():')}| ")
