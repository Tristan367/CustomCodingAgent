"""Shared types for tool implementations."""

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ToolContext:
    """Per-invocation environment handed to every tool."""
    session_id: str
    project_dir: str
    provider: str = "deepseek"
    model: str = "deepseek-v4-pro"
    abort: asyncio.Event = field(default_factory=asyncio.Event)

    def resolve(self, path: str | None) -> Path:
        """Resolve a possibly-relative path against the session's project dir."""
        if not path:
            return Path(self.project_dir)
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path(self.project_dir) / p
        return p


@dataclass
class ToolResult:
    """Outcome of a tool call.

    `output` is what the model sees. `title` is a one-line human summary for the
    transcript. `diff` is an optional unified diff rendered inline by the UI --
    it is deliberately not sent to the model, which already knows what it wrote.
    """
    output: str
    is_error: bool = False
    title: str = ""
    diff: str = ""
    # Token usage for tools that call a model themselves, so their spend is
    # attributed to the session instead of vanishing.
    usage: dict | None = None

    @classmethod
    def error(cls, message: str, title: str = "", usage: dict | None = None) -> "ToolResult":
        return cls(
            output=f"Error: {message}", is_error=True, title=title or "error",
            usage=usage or None,
        )


def unified_diff(before: str, after: str, path: str, context: int = 3) -> str:
    """Compact unified diff for display. Empty when nothing changed."""
    import difflib

    lines = list(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=path,
        tofile=path,
        n=context,
    ))
    if not lines:
        return ""
    # Drop the ---/+++ header; the UI already shows the filename.
    body = lines[2:] if len(lines) > 2 and lines[0].startswith("---") else lines
    text = "".join(body)
    if len(text) > 20_000:
        text = text[:20_000] + "\n... [diff truncated]"
    return text


def diff_stats(diff: str) -> tuple[int, int]:
    added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return added, removed


def truncate(text: str, limit: int, note: str = "output", spill: bool = False) -> str:
    """Cut `text` to `limit`, optionally keeping the discarded tail on disk.

    Without a spill the overflow is gone for good, which is how a grep that
    matched slightly too much could hide the one line that mattered. With one,
    the full text is written to a file and the model is told where, so it can go
    and read the part it needs instead of guessing or re-running the tool.

    The result never exceeds `limit`: the marker is measured first and the text
    is cut short to make room. Otherwise a second pass through truncate -- and
    every tool result goes through at least two -- would trim the marker off the
    end and lose the pointer it just wrote.
    """
    if len(text) <= limit:
        return text
    path = _spill(text) if spill else None
    where = (
        f"; the full {len(text):,} characters are at {path} -- read it for the rest"
        if path else ""
    )
    marker = f"\n\n... [{note} truncated at {limit:,} characters{where}]"
    return text[: max(0, limit - len(marker))] + marker


SPILL_DIR = Path.home() / ".codeagent" / "tool-output"
SPILL_MAX_AGE = 2 * 24 * 60 * 60


def _spill(text: str) -> Path | None:
    """Write an over-long tool output somewhere the model can read it.

    Named by content hash, so re-running the same command reuses one file
    instead of littering. Best effort throughout: a full disk or a read-only
    home must degrade to ordinary truncation, never break the tool call.
    """
    try:
        SPILL_DIR.mkdir(parents=True, exist_ok=True)
        path = SPILL_DIR / f"{hashlib.sha1(text.encode()).hexdigest()[:16]}.txt"
        if path.exists():
            # Reusing the file has to count as using it. Without this the clock
            # keeps running from the first write, so output spilled again on day
            # two would be handed to the model and deleted moments later.
            path.touch()
        else:
            path.write_text(text)
        _prune_spills()
        return path
    except OSError:
        return None


def _prune_spills():
    """Delete anything untouched for two days. These exist for the tool call
    that produced them and the few that follow it; nothing reads them later."""
    cutoff = time.time() - SPILL_MAX_AGE
    for old in SPILL_DIR.glob("*.txt"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
