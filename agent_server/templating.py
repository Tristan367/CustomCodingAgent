"""The Jinja environment and the filters registered on it.

This lives apart from main.py so that a route module can render a template
without importing the app that will import the route module. main.py owns the
FastAPI object and the route modules own the handlers; both need `templates`,
which makes it neither's property.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from agent_server.conversation import normalize_tool_calls

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "web_ui" / "templates"
STATIC_DIR = BASE_DIR / "web_ui" / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


# ── Template filters ────────────────────────────────────────────────────────

def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone()


def humantime(value: str) -> str:
    """Relative for recent timestamps, absolute once it stops being useful."""
    dt = _parse(value)
    if dt is None:
        return value
    delta = datetime.now(UTC) - dt.astimezone(UTC)
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 604800:
        return f"{int(secs // 86400)}d ago"
    return dt.strftime("%b %-d, %Y")


def clocktime(value: str) -> str:
    dt = _parse(value)
    if dt is None:
        return value
    return dt.strftime("%-I:%M %p").lower().replace("am", "AM").replace("pm", "PM")


def tildepath(value: str) -> str:
    """Render /home/you/projects/x as ~/projects/x."""
    if not value:
        return value
    home = str(Path.home())
    if value == home:
        return "~"
    if value.startswith(home + "/"):
        return "~" + value[len(home):]
    return value


templates.env.filters["humantime"] = humantime
_ATTACHMENT_RE = re.compile(r"^\[Image attached: (?P<path>.+?) \((?P<meta>[^)]*)\)\]$", re.M)
_ATTACHMENT_HINT = re.compile(r"^Use the `vision` tool on th(?:is path|ese paths) to see the images?\.$", re.M)


def extract_attachments(content: str) -> list[dict]:
    """Attachment paths recorded in a user message, for rendering as thumbnails."""
    return [
        {"path": m.group("path"), "meta": m.group("meta")}
        for m in _ATTACHMENT_RE.finditer(content or "")
    ]


def strip_attachments(content: str) -> str:
    """The message without the plumbing the model needs but the user does not."""
    text = _ATTACHMENT_RE.sub("", content or "")
    text = _ATTACHMENT_HINT.sub("", text)
    return text.strip()


def difflines(diff: str) -> list[tuple[str, str]]:
    """Tag each diff line with a CSS class, matching renderDiff() in app.js so a
    reloaded transcript looks identical to the streamed one."""
    out = []
    for line in (diff or "").rstrip("\n").split("\n"):
        if line.startswith("@@"):
            cls = "diff-hunk"
        elif line.startswith(("+++", "---")):
            cls = "diff-meta"
        elif line.startswith("+"):
            cls = "diff-add"
        elif line.startswith("-"):
            cls = "diff-del"
        else:
            cls = "diff-ctx"
        out.append((cls, line))
    return out


def diffstat_counts(diff: str) -> tuple[int, int]:
    added = sum(1 for ln in (diff or "").splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in (diff or "").splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return added, removed


def duration_label(ms: int | None) -> str:
    """Only worth showing once a call is slow enough to have been noticed."""
    if not ms or ms < 1000:
        return ""
    return f"{ms / 1000:.1f}s"


templates.env.filters["clocktime"] = clocktime
templates.env.filters["tildepath"] = tildepath
templates.env.filters["attachments"] = extract_attachments
templates.env.filters["withoutattachments"] = strip_attachments
templates.env.filters["toolcalls"] = normalize_tool_calls
templates.env.filters["difflines"] = difflines
templates.env.filters["diffstat"] = diffstat_counts
templates.env.filters["duration"] = duration_label


