"""TodoWrite tool — lets the AI manage a task list during coding sessions."""

# In-memory todos per session (cleared on server restart — good enough for now)
_session_todos: dict[str, list[dict]] = {}


async def todowrite(*, todos: list[dict], session_id: str = "") -> str:
    """Create/update a structured task list. Each todo has: content, status (pending/in_progress/completed/cancelled), priority (high/medium/low)."""
    _session_todos[session_id] = todos

    lines = ["Tasks:"]
    for t in todos:
        status_icon = {"pending": "○", "in_progress": "●", "completed": "✓", "cancelled": "✗"}.get(t.get("status", "pending"), "?")
        lines.append(f"  {status_icon} [{t.get('priority', 'medium')}] {t.get('content', '?')}")
    return "\n".join(lines)
