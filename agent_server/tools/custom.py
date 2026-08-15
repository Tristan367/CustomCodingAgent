"""User-defined tools: a JSON Schema and a shell script, stored in the database."""

import json
from typing import Any

from agent_server import database as db
from agent_server.tools.base import ToolContext, ToolResult
from agent_server.tools.bash import run_bash
from agent_server.tools.registry import (
    BUILT_IN_NAMES,
    Tool,
    _custom_tool_names,
    register_custom,
    unregister_custom,
)

__all__ = ["BUILT_IN_NAMES", "load_custom_tools"]


def _make_handler(script: str):
    async def _run(ctx: ToolContext, **kwargs: Any) -> ToolResult:
        env_vars = {f"TOOL_ARG_{k.upper()}": json.dumps(v) for k, v in kwargs.items()}
        env_vars.update(await db.load_secrets_dict())
        return await run_bash(ctx, command=script, env=env_vars)
    return _run


async def load_custom_tools() -> list[str]:
    """Register every custom tool. Returns any problems, for the UI.

    A row that cannot be loaded is skipped rather than raised. Loading used to
    deregister everything first and then parse, so one row with unparseable
    parameters -- which the save path allowed, because it skipped validation
    when the field was empty -- left *every* custom tool deregistered and made
    the next startup fail before the app could serve a page to fix it with.
    """
    problems: list[str] = []
    unregister_custom(_custom_tool_names.copy())

    for row in await db.list_custom_tools():
        name = row["name"]
        try:
            parameters = json.loads(row["parameters"] or "{}")
        except json.JSONDecodeError as e:
            problems.append(f"{name}: parameters are not valid JSON ({e})")
            continue
        if not isinstance(parameters, dict):
            problems.append(f"{name}: parameters must be a JSON object")
            continue

        error = register_custom(Tool(
            name=name,
            description=row["description"],
            parameters=parameters,
            handler=_make_handler(row["script"]),
            pause="permission" if row["ask_permission"] else None,
        ))
        if error:
            problems.append(error)
    return problems
