"""Subagent tool: run a focused, read-only agent loop and return its answer.

The subagent keeps its conversation entirely in memory. Earlier versions created
a real row in `sessions`, which polluted the session list and leaked rows when
the subagent raised.
"""

import asyncio
import json

from agent_server.config import (
    MAX_TOOL_RESULT_CHARS,
    SUBAGENT_EFFORT,
    SUBAGENT_MAX_ROUNDS,
    SUBAGENT_TIMEOUT,
)
from agent_server.conversation import normalize_tool_calls, parse_arguments, tool_call_name
from agent_server.tools.base import ToolContext, ToolResult, truncate

# Deliberately read-only: a subagent researches, the main agent makes changes.
SUBAGENT_TOOLS = ("read", "grep", "glob", "webfetch", "websearch", "skill")
MAX_ROUNDS = SUBAGENT_MAX_ROUNDS
TIMEOUT = SUBAGENT_TIMEOUT

SUBAGENT_PROMPT = """You are a research subagent. You investigate and report back; \
you cannot modify anything.

Your tools are read-only: read, grep, glob, webfetch.

Work autonomously until you can fully answer the task, then reply with your \
findings. Your entire reply is the only thing returned to the parent agent, so \
it must stand alone: include concrete file paths with line numbers, relevant \
code snippets, and direct answers. Do not ask questions or describe your plan."""


async def run_task(ctx: ToolContext, *, description: str, prompt: str, count: int = 1, **_) -> ToolResult:
    title = f"task: {description[:70]}"
    if count < 1:
        count = 1

    try:
        if count == 1:
            return await asyncio.wait_for(_run(ctx, description, prompt, title), timeout=TIMEOUT)
        tool_cache: dict = {}
        tasks = [asyncio.wait_for(_run(ctx, description, prompt, title, tool_cache), timeout=TIMEOUT) for _ in range(count)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        parts = []
        total_usage: dict = {}
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                parts.append(f"[agent {i+1}]: failed: {r}")
            elif r.is_error:
                parts.append(f"[agent {i+1}]: {r.output}")
            else:
                parts.append(f"[agent {i+1}]: {r.output}")
            if hasattr(r, 'usage') and r.usage:
                for k, v in r.usage.items():
                    total_usage[k] = total_usage.get(k, 0) + v
        return ToolResult(output="\n\n".join(parts), title=title, usage=total_usage or None)
    except TimeoutError:
        return ToolResult.error(f"subagent timed out after {TIMEOUT}s", title)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return ToolResult.error(f"subagent failed: {type(e).__name__}: {e}", title)


async def _run(ctx: ToolContext, description: str, prompt: str, title: str, tool_cache: dict | None = None) -> ToolResult:
    from agent_server.providers import get_provider
    from agent_server.tools.registry import execute_tool, tool_schemas

    # Check for subagent overrides
    subagent_prompt = ""
    subagent_model = None
    try:
        from agent_server import database as db
        subagent_prompt = await db.get_setting("subagent_prompt", "")
        subagent_model = await db.get_setting("subagent_model", "")
    except Exception:
        pass

    provider_name = ctx.provider
    effective_model = subagent_model or ctx.model

    provider = get_provider(provider_name)
    tools = tool_schemas(SUBAGENT_TOOLS)

    system_content = subagent_prompt or SUBAGENT_PROMPT
    messages: list[dict] = [
        {"role": "system", "content": f"{system_content}\n\nWorking directory: {ctx.project_dir}"},
        {"role": "user", "content": prompt},
    ]

    usage_total: dict = {}

    for _round in range(MAX_ROUNDS):
        if ctx.abort.is_set():
            return ToolResult.error("cancelled", title, usage_total)

        content = ""
        reasoning = ""
        partials: dict[int, dict] = {}
        finish = "stop"

        async for event in provider.chat_completion(
            messages=messages, tools=tools, model=effective_model, thinking_effort=SUBAGENT_EFFORT
        ):
            if ctx.abort.is_set():
                return ToolResult.error("cancelled", title, usage_total)
            etype = event["type"]
            if etype == "content":
                content += event["text"]
            elif etype == "reasoning":
                reasoning += event["text"]
            elif etype == "tool_calls":
                _accumulate(partials, event["deltas"])
            elif etype == "usage":
                for key, value in (event["usage"] or {}).items():
                    if isinstance(value, (int, float)):
                        usage_total[key] = usage_total.get(key, 0) + value
            elif etype == "error":
                return ToolResult.error(event["message"], title, usage_total)
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
                return ToolResult(output=content.strip(), title=title, usage=usage_total or None)
            return ToolResult.error("subagent returned no answer", title, usage_total)

        for call in calls:
            tool_name = tool_call_name(call)
            tool_args = parse_arguments(call)
            if tool_cache is not None:
                cache_key = (tool_name, json.dumps(tool_args, sort_keys=True))
                if cache_key in tool_cache:
                    result = tool_cache[cache_key]
                else:
                    result = await execute_tool(tool_name, tool_args, ctx, allowed=SUBAGENT_TOOLS)
                    tool_cache[cache_key] = result
            else:
                result = await execute_tool(tool_name, tool_args, ctx, allowed=SUBAGENT_TOOLS)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": truncate(result.output, MAX_TOOL_RESULT_CHARS // 2, spill=True),
            })

    return ToolResult.error(
        f"subagent exceeded {MAX_ROUNDS} rounds without answering", title, usage_total
    )


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
