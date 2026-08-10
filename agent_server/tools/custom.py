import json
from typing import Any

from agent_server import database as db
from agent_server.tools.base import ToolContext, ToolResult
from agent_server.tools.bash import run_bash
from agent_server.tools.registry import (
    Tool,
    _custom_tool_names,
    register,
    unregister_custom,
)

BUILT_IN_NAMES = frozenset({
    "read", "edit", "write", "bash", "grep", "glob",
    "webfetch", "task", "vision", "screenshot",
})


def _make_handler(script: str):
    async def _run(ctx: ToolContext, **kwargs: Any) -> ToolResult:
        env_vars = {f"TOOL_ARG_{k.upper()}": json.dumps(v) for k, v in kwargs.items()}
        secrets = await db.load_secrets_dict()
        env_vars.update(secrets)
        return await run_bash(ctx, command=script, env=env_vars)
    return _run


async def load_custom_tools():
    """Register every enabled custom tool from the database."""
    unregister_custom(_custom_tool_names.copy())
    _custom_tool_names.clear()
    for row in await db.list_custom_tools():
        if not row["enabled"]:
            continue
        name = row["name"]
        handler = _make_handler(row["script"])
        pause = "permission" if row["ask_permission"] else None
        register(Tool(
            name=name,
            description=row["description"],
            parameters=json.loads(row["parameters"]),
            handler=handler,
            pause=pause,
        ))
        _custom_tool_names.add(name)


async def reload_custom_tools():
    """Re-register after a save/deletion."""
    await load_custom_tools()
