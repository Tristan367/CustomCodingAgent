"""Regression tests for shell execution.

The background-process case is the important one: `communicate()` and `wait()`
both block until the pipes reach EOF, which a backgrounded grandchild holds
open, so starting a dev server used to burn the entire timeout and then get
killed along with the rest of the process group.
"""

import asyncio
import time

import pytest

from agent_server.tools.base import ToolContext
from agent_server.tools.bash import run_bash

ctx = ToolContext(project_dir="/tmp", session_id="test")


def run(**kw):
    return asyncio.run(run_bash(ctx, **kw))


def test_backgrounded_process_returns_immediately():
    began = time.time()
    result = run(command="sleep 30 &\necho started", timeout=10_000)
    elapsed = time.time() - began
    assert elapsed < 5, f"took {elapsed:.1f}s; the shell exited almost at once"
    assert "started" in result.output
    assert not result.is_error


def test_backgrounded_process_is_reported():
    result = run(command="sleep 30 &\necho started", timeout=10_000)
    assert "background process" in result.output


def test_normal_command_still_captures_streams_and_exit_code():
    result = run(command="echo out; echo err >&2; exit 3")
    assert "out" in result.output
    assert "err" in result.output
    assert "[exit code 3]" in result.output
    assert result.is_error


def test_no_background_note_for_ordinary_commands():
    assert "background process" not in run(command="echo hi").output


def test_large_output_does_not_deadlock_on_the_pipe_buffer():
    result = run(command="for i in $(seq 1 5000); do echo 'a line of output'; done")
    assert result.output.count("a line of output") > 1000


def test_timeout_still_applies_to_foreground_commands():
    began = time.time()
    result = run(command="sleep 30", timeout=1500)
    assert time.time() - began < 6
    assert result.is_error and "timed out" in result.output


@pytest.mark.parametrize("command,expected", [
    ("ls -la", True),
    ("git status", True),
    ("cat a | grep b", True),
    ("rm -rf /", False),
    ("echo hi > file", False),
    ("git push", False),
    ("ls && rm x", False),
    ("find . -name '*.py'", True),
    ("find . -delete", False),
    ("find . -exec rm {} +", False),
    ("git branch", True),
    ("git branch -D foo", False),
    ("git remote add origin url", False),
    ("/bin/rm -rf x", False),
])
def test_read_only_classification(command, expected):
    from agent_server.tools.bash import is_read_only
    assert is_read_only(command) is expected


@pytest.mark.parametrize("command,expected", [
    ("rm -rf /", "rm -rf of /"),
    ("/bin/rm -rf /", "rm -rf of /"),
    ("rm -Rf /home", "rm -rf of /home"),
    ("rm --recursive --force /", "rm -rf of /"),
    ("rm --force /", None),
    ("rm -rf build/", None),
    ("git clean -fdx", None),
])
def test_danger_reason(command, expected):
    from agent_server.tools.bash import danger_reason
    assert danger_reason(command) == expected


# ── Oversized output ────────────────────────────────────────────────────────

def test_overflow_is_written_to_a_file_the_model_can_read(tmp_path, monkeypatch):
    """Truncation used to discard the tail permanently, so the one line that
    mattered could vanish with no way to get it back."""
    from agent_server.tools import base

    monkeypatch.setattr(base, "SPILL_DIR", tmp_path / "spill")
    text = "".join(f"line {i}\n" for i in range(5000))
    needle = "line 4999"
    assert needle in text

    out = base.truncate(text, 200, spill=True)

    assert len(out) <= 200, "the marker must fit inside the limit, not extend it"
    assert needle not in out
    written = list((tmp_path / "spill").glob("*.txt"))
    assert len(written) == 1, written
    assert written[0].read_text() == text
    assert str(written[0]) in out, "the model has to be told where the rest went"
    # Truncating again must not eat the pointer it just wrote.
    assert str(written[0]) in base.truncate(out, 200, spill=True)


def test_output_within_the_limit_is_untouched(tmp_path, monkeypatch):
    from agent_server.tools import base

    monkeypatch.setattr(base, "SPILL_DIR", tmp_path / "spill")
    assert base.truncate("short", 200, spill=True) == "short"
    assert not (tmp_path / "spill").exists(), "no file for output that fits"


def test_a_broken_spill_still_returns_truncated_text(tmp_path, monkeypatch):
    """A full disk must degrade to plain truncation, not break the tool call."""
    from agent_server.tools import base

    def boom(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr(base, "SPILL_DIR", tmp_path / "spill")
    monkeypatch.setattr(base.Path, "mkdir", boom)
    out = base.truncate("x" * 5000, 200, spill=True)
    assert len(out) <= 200
    assert "truncated" in out
