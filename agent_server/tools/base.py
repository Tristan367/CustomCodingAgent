"""Shared types for tool implementations."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ToolContext:
    """Per-invocation environment handed to every tool."""
    session_id: str
    project_dir: str
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
    transcript, so the UI does not have to re-derive it from raw arguments.
    """
    output: str
    is_error: bool = False
    title: str = ""

    @classmethod
    def error(cls, message: str, title: str = "") -> "ToolResult":
        return cls(output=f"Error: {message}", is_error=True, title=title or "error")


def truncate(text: str, limit: int, note: str = "output") -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n... [{note} truncated at {limit:,} characters]"
