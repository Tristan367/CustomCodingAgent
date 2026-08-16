"""Subagent tool: run a focused, read-only agent loop and return its answer.

The subagent keeps its conversation entirely in memory. Earlier versions created
a real row in `sessions`, which polluted the session list and leaked rows when
the subagent raised.
"""

import asyncio
import dataclasses
import json

import agent_server.system_prompt  # deferred: subagent_parallel_cap in run_task
from agent_server.config import (
    MAX_TOOL_RESULT_CHARS,
    SUBAGENT_EFFORT,
    SUBAGENT_MAX_ROUNDS,
    SUBAGENT_TIMEOUT,
)
from agent_server.conversation import normalize_tool_calls, parse_arguments, tool_call_name
from agent_server.tools.base import ToolContext, ToolResult, truncate


# Subagent tool names are read from the registry at import time so profiles can
# turn any tool on or off. The profile's subagent_disabled_tools removes from
# this list — by default everything except the read-only set is disabled.
def _subagent_tools():
    from agent_server.tools.registry import TOOLS
    return tuple(TOOLS.keys())

# Tools only real sessions may use. Subagents are scoped to a task and must not
# message other sessions; this is enforced here rather than in the profile's
# disabled list so a profile cannot accidentally re-enable it.
TOP_LEVEL_ONLY = frozenset({"send_message"})

MAX_ROUNDS = SUBAGENT_MAX_ROUNDS
TIMEOUT = SUBAGENT_TIMEOUT

# Final fallback — should only be used if default_subagent.md is missing AND
# the DB has no subagent_body for any profile. Better than an empty prompt.
SUBAGENT_FALLBACK = """You are a research subagent. Investigate and report back. \
Your tools are read-only. Work autonomously until you can fully answer the task, \
then reply with your findings. Include concrete file paths with line numbers \
and relevant code snippets. Do not ask questions or describe your plan."""

# Per-session semaphores keyed by session_id. Limits total concurrently-running
# subagents across all tiers in a single session. Each entry is (capacity, sem).
_session_sem: dict[str, tuple[int, asyncio.Semaphore]] = {}


async def run_task(ctx: ToolContext, *, description: str, prompt: str, count: int = 1, **_) -> ToolResult:
    title = description[:70]
    if count < 1:
        count = 1

    tier = ctx.subagent_tier
    cap = await agent_server.system_prompt.subagent_parallel_cap(ctx.prompt_profile or "default", tier)
    gcap = await agent_server.system_prompt.max_concurrent_subagents(ctx.prompt_profile or "default")

    # Subagents launched from here are one tier deeper. Passed to _run rather
    # than stored on the shared ctx, which is reused by every later tool call in
    # this turn and would otherwise remember the deeper tier forever.
    child_tier = tier + 1

    # Ensure a session semaphore with the right capacity.
    if gcap > 0 and ctx.session_id:
        entry = _session_sem.get(ctx.session_id)
        if entry is None or entry[0] != gcap:
            sem = asyncio.Semaphore(gcap)
            _session_sem[ctx.session_id] = (gcap, sem)
        else:
            sem = entry[1]
    else:
        sem = None

    async def _guarded(desc, prompt_text, t, tc=None):
        if sem:
            await sem.acquire()
        try:
            return await _run(ctx, desc, prompt_text, t, tc, child_tier)
        finally:
            if sem:
                sem.release()

    running = 0
    if gcap > 0 and ctx.session_id:
        entry = _session_sem.get(ctx.session_id)
        if entry:
            sem_obj = entry[1]
            # _value is CPython implementation detail; guarded by hasattr.
            if hasattr(sem_obj, '_value'):
                running = max(0, entry[0] - sem_obj._value)
    queued = max(0, count - running)
    if queued > 0:
        title = f"{description[:50]} ({running} running, {queued} queued)"

    if cap > 0 and count > cap:
        return await _batched(ctx, description, prompt, title, count, cap, _guarded)

    try:
        if count == 1:
            return await asyncio.wait_for(_guarded(description, prompt, title), timeout=TIMEOUT)
        tool_cache: dict = {}
        tasks = [asyncio.wait_for(_guarded(description, prompt, title, tool_cache), timeout=TIMEOUT) for _ in range(count)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return _combine(results, title)
    except TimeoutError:
        return ToolResult.error(f"subagent timed out after {TIMEOUT}s", title)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return ToolResult.error(f"subagent failed: {type(e).__name__}: {e}", title)


async def _batched(ctx, description, prompt, title, count, cap, _guarded):
    """Run up to *cap* parallel subagents at a time, in sequence."""
    parts = []
    total_usage: dict = {}
    remaining = count
    batch_num = 0
    while remaining > 0:
        n = min(remaining, cap)
        batch_num += 1
        tool_cache: dict = {}
        tasks = [asyncio.wait_for(_guarded(description, prompt, title, tool_cache), timeout=TIMEOUT) for _ in range(n)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        _append_results(parts, total_usage, results, (batch_num - 1) * cap)
        remaining -= n
        if remaining > 0 and ctx.abort.is_set():
            parts.append(f"(cancelled after {count - remaining} of {count})")
            break
    return ToolResult(output="\n\n".join(parts), title=title, usage=total_usage or None)


def _append_results(parts, total_usage, results, offset=0):
    for i, r in enumerate(results):
        label = i + offset + 1
        if isinstance(r, Exception):
            parts.append(f"[agent {label}]: failed: {r}")
        else:
            parts.append(f"[agent {label}]: {r.output}")
        if hasattr(r, 'usage') and r.usage:
            for k, v in r.usage.items():
                total_usage[k] = total_usage.get(k, 0) + v


def _combine(results, title):
    parts = []
    total_usage: dict = {}
    _append_results(parts, total_usage, results)
    return ToolResult(output="\n\n".join(parts), title=title, usage=total_usage or None)


async def _run(ctx: ToolContext, description: str, prompt: str, title: str, tool_cache: dict | None = None, tier: int = 0) -> ToolResult:
    from agent_server.config import provider_for_model
    from agent_server.providers import get_provider
    from agent_server.system_prompt import subagent_body as _subagent_body
    from agent_server.system_prompt import subagent_disabled_tools
    from agent_server.tools.registry import execute_tool, tool_schemas

    profile = ctx.prompt_profile or "default"
    # Nested tool calls (including a further `task`) must see this subagent's
    # tier, not the parent's. The shared ctx is left untouched so the parent's
    # later tool calls in the same turn stay at their own tier.
    child_ctx = dataclasses.replace(ctx, subagent_tier=tier)
    system_content = (await _subagent_body(profile, tier)).strip()
    if not system_content:
        system_content = (await _subagent_body(profile)).strip()
    off = await subagent_disabled_tools(profile, tier)
    tool_names = [n for n in _subagent_tools() if n not in off and n not in TOP_LEVEL_ONLY]
    tools = tool_schemas(tool_names)
    # The subagent model is a property of the session, so a search-heavy session
    # can fan out onto something cheap while a session writing code keeps the
    # parent's model. It was a single global setting, which meant choosing it
    # for one session silently changed every other one.
    effective_model = ctx.subagent_model or ""
    if not effective_model:
        effective_model = await agent_server.system_prompt.subagent_model_name(
            ctx.prompt_profile or "default", tier
        )
    if not effective_model:
        effective_model = ctx.model
    # A model implies its provider. Reading the parent's provider while
    # overriding only the model is how a session ends up asking DeepSeek to
    # serve an Anthropic model.
    provider_name = provider_for_model(effective_model) or ctx.provider

    provider = get_provider(provider_name)

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
                    result = await execute_tool(tool_name, tool_args, child_ctx, allowed=tool_names)
                    tool_cache[cache_key] = result
            else:
                result = await execute_tool(tool_name, tool_args, child_ctx, allowed=tool_names)
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
