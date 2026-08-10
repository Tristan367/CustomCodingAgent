"""`edit` and `write` must not silently rewrite a file's encoding.

read_text/write_text normalise a UTF-8 BOM into the string and collapse CRLF to
LF, so editing one line of a CRLF file rewrote every line of it, and an edit
anchored on line 1 of a BOM file ate the BOM. The user sees a one-line diff;
git sees the whole file.
"""
import asyncio

import pytest

from agent_server.tools.base import ToolContext
from agent_server.tools.file_ops import edit_file, read_file, write_file


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(
        session_id="s", project_dir=str(tmp_path), abort=asyncio.Event()
    )


BOM_BYTES = b"\xef\xbb\xbf"


# ── helpers ──

async def _read(ctx, path):
    return await read_file(ctx, filePath=str(path))


async def test_bom_preserved_on_edit(ctx, tmp_path):
    """Editing a line in a BOM file must keep the BOM exactly once."""
    path = tmp_path / "bom.txt"
    path.write_bytes(BOM_BYTES + b"line1\nline2\nline3\n")
    await _read(ctx, path)

    # Editing the FIRST line is the case that breaks: read_text() hands back the
    # BOM as a leading \ufeff, so any edit anchored on line 1 either eats it or
    # matches around it. An edit further down preserves the BOM by accident and
    # proves nothing.
    result = await edit_file(
        ctx, filePath=str(path),
        oldString="line1", newString="LINE-ONE",
    )
    assert not result.is_error, result.output

    raw = path.read_bytes()
    assert raw.startswith(BOM_BYTES), "BOM was dropped"
    assert raw.count(BOM_BYTES) == 1, "BOM appears more than once"
    text = raw.decode("utf-8-sig")
    assert text.startswith("LINE-ONE"), f"first line is {text.splitlines()[0]!r}"


async def test_no_bom_never_gains_one(ctx, tmp_path):
    """A file without a BOM must not gain one after an edit."""
    path = tmp_path / "nobom.txt"
    path.write_text("line1\nline2\nline3\n")
    await _read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path),
        oldString="line2", newString="LINE-TWO",
    )
    assert not result.is_error, result.output

    raw = path.read_bytes()
    assert not raw.startswith(BOM_BYTES), "file gained a BOM"


async def test_write_file_preserves_bom_on_overwrite(ctx, tmp_path):
    """write_file over an existing BOM file must keep the BOM."""
    path = tmp_path / "bom.txt"
    path.write_bytes(BOM_BYTES + b"original\n")
    await _read(ctx, path)

    result = await write_file(ctx, filePath=str(path), content="replacement\n")
    assert not result.is_error, result.output

    raw = path.read_bytes()
    assert raw.startswith(BOM_BYTES), "BOM was dropped by write_file"


# ──────────────────────────────────────────────────────
# Defect 3 — CRLF
# ──────────────────────────────────────────────────────

async def test_crlf_preserved_on_edit(ctx, tmp_path):
    """Editing one line of a CRLF file leaves every other line as CRLF,
    and the byte count changes by only the edited line's delta."""
    path = tmp_path / "crlf.txt"
    original = "line1\r\nline2\r\nline3\r\n"
    path.write_bytes(original.encode("ascii"))
    await _read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path),
        oldString="line2", newString="LINE-TWO",
    )
    assert not result.is_error, result.output

    raw = path.read_bytes()
    text = raw.decode("ascii")
    # Every line that wasn't edited must still end with \r\n.
    assert text.endswith("\r\n"), f"trailing newline wrong: {text!r}"
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if "LINE-TWO" in line:
            continue  # the edited line; its ending is whatever it was
        assert line.endswith("\r\n"), f"line {i} lost CRLF: {line!r}"

    # Byte count delta should be exactly the edit's delta.
    expected = "line1\r\nLINE-TWO\r\nline3\r\n"
    assert raw == expected.encode("ascii"), f"byte-level mismatch: {raw!r}"


async def test_lf_stays_lf(ctx, tmp_path):
    """An LF file must not gain CRLF endings after an edit."""
    path = tmp_path / "lf.txt"
    path.write_text("line1\nline2\nline3\n")
    await _read(ctx, path)

    result = await edit_file(
        ctx, filePath=str(path),
        oldString="line2", newString="LINE-TWO",
    )
    assert not result.is_error, result.output

    raw = path.read_bytes()
    assert b"\r\n" not in raw, "LF file gained CRLF"


async def test_write_file_preserves_crlf_on_overwrite(ctx, tmp_path):
    """write_file over an existing CRLF file must keep CRLF."""
    path = tmp_path / "crlf.txt"
    path.write_bytes("original\r\n".encode("ascii"))
    await _read(ctx, path)

    result = await write_file(ctx, filePath=str(path), content="replacement\n")
    assert not result.is_error, result.output

    raw = path.read_bytes()
    assert raw == b"replacement\r\n", f"CRLF not preserved: {raw!r}"
