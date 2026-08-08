"""Per-session task list the model maintains while working."""

from agent_server.tools.base import ToolContext, ToolResult

STATUS_ICON = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "cancelled": "[-]"}
VALID_STATUS = set(STATUS_ICON)

# session_id -> todos. Intentionally in-memory: a task list is scoped to one
# working session and should not outlive a server restart.
_todos: dict[str, list[dict]] = {}


def get_todos(session_id: str) -> list[dict]:
    return _todos.get(session_id, [])


def clear_todos(session_id: str):
    _todos.pop(session_id, None)


async def todowrite(ctx: ToolContext, *, todos: list[dict] | None = None, **_) -> ToolResult:
    if todos is None or not isinstance(todos, list):
        return ToolResult.error("`todos` must be a list", "todowrite")

    cleaned: list[dict] = []
    for item in todos:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        status = str(item.get("status", "pending")).lower()
        cleaned.append({
            "content": content,
            "status": status if status in VALID_STATUS else "pending",
            "priority": str(item.get("priority", "medium")).lower(),
        })

    if not cleaned:
        return ToolResult.error("no valid todo items provided", "todowrite")

    active = [t for t in cleaned if t["status"] == "in_progress"]
    if len(active) > 1:
        return ToolResult.error(
            f"only one todo may be in_progress at a time (got {len(active)})", "todowrite"
        )

    _todos[ctx.session_id] = cleaned
    done = sum(1 for t in cleaned if t["status"] == "completed")
    lines = [f"{STATUS_ICON[t['status']]} {t['content']}" for t in cleaned]
    return ToolResult(
        output="\n".join(lines),
        title=f"todos ({done}/{len(cleaned)} done)",
    )
