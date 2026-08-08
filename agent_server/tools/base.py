"""Shared types for tool implementations."""

import asyncio
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


def truncate(text: str, limit: int, note: str = "output") -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n... [{note} truncated at {limit:,} characters]"
