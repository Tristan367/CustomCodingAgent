"""Subagent tool: run a focused, read-only agent loop and return its answer.

The subagent keeps its conversation entirely in memory. Earlier versions created
a real row in `sessions`, which polluted the session list and leaked rows when
the subagent raised.
"""

import asyncio

from agent_server.config import DEFAULT_MODEL, DEFAULT_PROVIDER, MAX_TOOL_RESULT_CHARS
from agent_server.conversation import normalize_tool_calls, parse_arguments, tool_call_name
from agent_server.tools.base import ToolContext, ToolResult, truncate

# Deliberately read-only: a subagent researches, the main agent makes changes.
SUBAGENT_TOOLS = ("read", "grep", "glob", "webfetch")
MAX_ROUNDS = 20
TIMEOUT = 600

SUBAGENT_PROMPT = """You are a research subagent. You investigate and report back; \
you cannot modify anything.

Your tools are read-only: read, grep, glob, webfetch.

Work autonomously until you can fully answer the task, then reply with your \
findings. Your entire reply is the only thing returned to the parent agent, so \
it must stand alone: include concrete file paths with line numbers, relevant \
code snippets, and direct answers. Do not ask questions or describe your plan."""


async def run_task(ctx: ToolContext, *, description: str, prompt: str, **_) -> ToolResult:
    title = f"task: {description[:70]}"
    try:
        return await asyncio.wait_for(_run(ctx, description, prompt, title), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        return ToolResult.error(f"subagent timed out after {TIMEOUT}s", title)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(f"subagent failed: {type(e).__name__}: {e}", title)


async def _run(ctx: ToolContext, description: str, prompt: str, title: str) -> ToolResult:
    from agent_server.providers import get_provider
    from agent_server.tools.registry import execute_tool, tool_schemas

    provider = get_provider(DEFAULT_PROVIDER)
    tools = tool_schemas(SUBAGENT_TOOLS)

    messages: list[dict] = [
        {"role": "system", "content": f"{SUBAGENT_PROMPT}\n\nWorking directory: {ctx.project_dir}"},
        {"role": "user", "content": prompt},
    ]

    for _round in range(MAX_ROUNDS):
        if ctx.abort.is_set():
            return ToolResult.error("cancelled", title)

        content = ""
        reasoning = ""
        partials: dict[int, dict] = {}
        finish = "stop"

        async for event in provider.chat_completion(
            messages=messages, tools=tools, model=DEFAULT_MODEL, thinking_effort="low"
        ):
            etype = event["type"]
            if etype == "content":
                content += event["text"]
            elif etype == "reasoning":
                reasoning += event["text"]
            elif etype == "tool_calls":
                _accumulate(partials, event["deltas"])
            elif etype == "error":
                return ToolResult.error(event["message"], title)
            elif etype == "finish":
                finish = event["reason"]

        calls = normalize_tool_calls(
            [partials[i] for i in sorted(partials)]
        )

        assistant: dict = {"role": "assistant", "content": content}
        if reasoning:
            assistant["reasoning_content"] = reasoning
        if calls:
            assistant["tool_calls"] = calls
        messages.append(assistant)

        if finish != "tool_calls" or not calls:
            if content.strip():
                return ToolResult(output=content.strip(), title=title)
            return ToolResult.error("subagent returned no answer", title)

        for call in calls:
            result = await execute_tool(
                tool_call_name(call), parse_arguments(call), ctx, allowed=SUBAGENT_TOOLS
            )
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": truncate(result.output, MAX_TOOL_RESULT_CHARS // 2),
            })

    return ToolResult.error(f"subagent exceeded {MAX_ROUNDS} rounds without answering", title)


def _accumulate(partials: dict[int, dict], deltas: list[dict]):
    for d in deltas:
        idx = d.get("index", 0)
        slot = partials.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if d.get("id"):
            slot["id"] = d["id"]
        if d.get("name"):
            slot["name"] = d["name"]
        if d.get("arguments"):
            slot["arguments"] += d["arguments"]
