"""The agent loop.

Responsibilities, in order of how badly they used to break:

1. Persist the user's message before doing anything else. The previous
   implementation accepted the message and dropped it, so the model was prompted
   with a system prompt and nothing else and hallucinated an entire task.
2. Drive the provider/tool cycle, persisting each step so the transcript in the
   database always matches what was actually sent.
3. Pause cleanly for tool calls that need the user (shell approval, questions)
   without ever leaving an assistant `tool_calls` message unanswered.
4. Emit a single, well-defined event stream for the UI.
"""

import asyncio
import json
from typing import AsyncIterator

from agent_server import database as db
from agent_server.config import (
    MAX_TOOL_RESULT_CHARS,
    MAX_TOOL_ROUNDS,
)
from agent_server.conversation import (
    build_messages,
    normalize_tool_calls,
    parse_arguments,
    pending_tool_calls,
    tool_call_name,
)
from agent_server.providers import Provider, get_provider
from agent_server.system_prompt import build_system_prompt
from agent_server.tools.base import ToolContext, ToolResult, truncate
from agent_server.tools.question import format_prompt
from agent_server.tools.registry import execute_tool, get_tool, requires_permission, tool_schemas

# session_id -> abort signal for the in-flight run.
_aborts: dict[str, asyncio.Event] = {}
# Sessions the user chose to auto-approve for the lifetime of this process.
_runtime_auto_approve: set[str] = set()
# Individual tool calls the user approved. Consumed by the next _drain_pending
# so the approved tool runs inside the loop and streams its output like any
# other tool call, instead of executing silently in the resolve endpoint.
_approved_calls: set[str] = set()


def request_abort(session_id: str) -> bool:
    event = _aborts.get(session_id)
    if event is None:
        return False
    event.set()
    return True


def is_running(session_id: str) -> bool:
    return session_id in _aborts


def set_runtime_auto_approve(session_id: str, enabled: bool):
    if enabled:
        _runtime_auto_approve.add(session_id)
    else:
        _runtime_auto_approve.discard(session_id)


def runtime_auto_approve(session_id: str) -> bool:
    return session_id in _runtime_auto_approve


async def _auto_approves(session: dict) -> bool:
    return bool(session.get("bash_auto_approve")) or runtime_auto_approve(session["id"])


class Paused(Exception):
    """Raised internally when the loop stops to wait for the user."""


async def run(session_id: str) -> AsyncIterator[dict]:
    """Drive the session forward and yield UI events.

    Assumes any new user input has already been persisted.
    """
    session = await db.get_session(session_id)
    if session is None:
        yield {"type": "error", "message": "Session not found"}
        return

    provider = get_provider(session["provider"])
    if not provider.has_credentials():
        yield {
            "type": "error",
            "message": f"No API key configured for {session['provider']}. Add one on the home page.",
        }
        return

    if session_id in _aborts:
        yield {"type": "error", "message": "This session already has a run in progress."}
        return

    abort = asyncio.Event()
    _aborts[session_id] = abort
    ctx = ToolContext(session_id=session_id, project_dir=session["project_dir"], abort=abort)

    try:
        async for event in _loop(session, provider, ctx, abort):
            yield event
    except asyncio.CancelledError:
        # Client disconnected: stop quietly, transcript is already consistent.
        raise
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": f"Agent error: {type(e).__name__}: {e}"}
    finally:
        _aborts.pop(session_id, None)


async def _loop(
    session: dict,
    provider: Provider,
    ctx: ToolContext,
    abort: asyncio.Event,
) -> AsyncIterator[dict]:
    session_id = session["id"]
    tools = tool_schemas(include_vision=not provider.supports_vision())

    # Finish any tool calls left outstanding by a previous pause before asking
    # the model for more. Without this the next request would carry an assistant
    # message whose tool_calls have no matching results, which the API rejects.
    async for event in _drain_pending(session, ctx):
        yield event
        if event["type"] in ("permission", "question"):
            return

    # Rebuilt every round, but deterministic per session, so the cached prompt
    # prefix survives. See build_system_prompt for why that matters.
    system_prompt = await build_system_prompt(
        session.get("prompt_profile") or "default", session["project_dir"], session_id
    )

    for _round in range(MAX_TOOL_ROUNDS):
        if abort.is_set():
            yield {"type": "aborted"}
            return

        rows = await db.get_messages(session_id)
        messages = build_messages(system_prompt, await db.get_compactions(session_id), rows)

        if not any(m["role"] != "system" for m in messages):
            yield {"type": "error", "message": "Nothing to send: the conversation is empty."}
            return

        content = ""
        reasoning = ""
        partials: dict[int, dict] = {}
        usage: dict | None = None
        finish = "stop"
        failed = False

        async for event in provider.chat_completion(
            messages=messages,
            tools=tools,
            model=session["model"],
            thinking_effort=session.get("thinking_effort"),
        ):
            if abort.is_set():
                break

            etype = event["type"]
            if etype == "content":
                content += event["text"]
                yield {"type": "content", "text": event["text"]}
            elif etype == "reasoning":
                reasoning += event["text"]
                yield {"type": "reasoning", "text": event["text"]}
            elif etype == "tool_calls":
                _accumulate(partials, event["deltas"])
            elif etype == "usage":
                usage = event["usage"]
            elif etype == "finish":
                finish = event["reason"]
            elif etype == "error":
                failed = True
                # Persist partial output so the turn is not silently lost.
                if content.strip() or reasoning.strip():
                    await db.add_message(
                        session_id, "assistant", content,
                        reasoning_content=reasoning or None,
                        token_count=provider.count_tokens([{"role": "assistant", "content": content}]),
                    )
                yield {"type": "error", "message": event["message"]}
                break

        if failed:
            return

        if abort.is_set():
            if content.strip() or reasoning.strip():
                await db.add_message(
                    session_id, "assistant", content,
                    reasoning_content=reasoning or None,
                    token_count=provider.count_tokens([{"role": "assistant", "content": content}]),
                )
            yield {"type": "aborted"}
            return

        calls = normalize_tool_calls([partials[i] for i in sorted(partials)])

        # DeepSeek requires reasoning_content to be echoed back on any assistant
        # turn that made a tool call, so it is always stored alongside the message.
        message = await db.add_message(
            session_id,
            "assistant",
            content,
            reasoning_content=reasoning or None,
            tool_calls=calls or None,
            token_count=provider.count_tokens(
                [{"role": "assistant", "content": content, "reasoning_content": reasoning,
                  "tool_calls": calls}]
            ),
            usage=usage,
        )
        if usage:
            yield {"type": "usage", "usage": usage}

        if finish == "length":
            yield {"type": "error", "message": "Model hit its output limit. Ask it to continue."}
            return

        if not calls:
            yield {"type": "done", "reason": finish, "message_id": message["id"]}
            return

        paused = False
        async for event in _drain_pending(session, ctx):
            yield event
            if event["type"] in ("permission", "question"):
                paused = True
        if paused:
            return

    yield {
        "type": "error",
        "message": f"Stopped after {MAX_TOOL_ROUNDS} tool rounds without finishing.",
    }


async def _drain_pending(session: dict, ctx: ToolContext) -> AsyncIterator[dict]:
    """Execute every unanswered tool call on the latest assistant turn.

    Yields a `permission` or `question` event and stops as soon as one needs the
    user. The remaining calls stay pending in the database and are picked up on
    the next call, so multi-tool rounds resume correctly.
    """
    session_id = session["id"]
    rows = await db.get_messages(session_id)
    assistant_row, pending = pending_tool_calls(rows)
    if assistant_row is None or not pending:
        return

    auto = await _auto_approves(session)

    for call in pending:
        if ctx.abort.is_set():
            # Leave a result so the turn stays structurally valid.
            await _record(session_id, call, ToolResult.error("cancelled by user", "cancelled"))
            continue

        name = tool_call_name(call)
        args = parse_arguments(call)
        tool = get_tool(name)

        if tool is not None and tool.pause == "question":
            yield {
                "type": "question",
                "tool_call_id": call["id"],
                "question": args.get("question", ""),
                "options": args.get("options") or [],
                "prompt": format_prompt(args.get("question", ""), args.get("options")),
            }
            return

        if not auto and call["id"] not in _approved_calls and requires_permission(name, args):
            yield {
                "type": "permission",
                "tool_call_id": call["id"],
                "name": name,
                "args": args,
                "command": args.get("command", ""),
                "workdir": args.get("workdir") or session["project_dir"],
            }
            return

        _approved_calls.discard(call["id"])
        yield {"type": "tool_start", "tool_call_id": call["id"], "name": name, "args": args}
        result = await execute_tool(name, args, ctx)
        await _record(session_id, call, result)
        yield {
            "type": "tool_end",
            "tool_call_id": call["id"],
            "name": name,
            "title": result.title,
            "output": truncate(result.output, 20_000, "preview"),
            "is_error": result.is_error,
        }


async def _record(session_id: str, call: dict, result: ToolResult) -> dict:
    from agent_server.providers.base import estimate_tokens

    output = truncate(result.output, MAX_TOOL_RESULT_CHARS)
    return await db.add_message(
        session_id,
        "tool",
        output,
        tool_call_id=call["id"],
        tool_name=tool_call_name(call),
        is_error=result.is_error,
        token_count=estimate_tokens([{"role": "tool", "content": output}]),
    )


async def resolve_pending(
    session_id: str,
    tool_call_id: str,
    action: str,
    value: str = "",
) -> bool:
    """Answer one paused tool call so the loop can continue.

    action: "approve" | "reject" | "answer"
    Returns False if the id is not actually pending (double submit, stale UI).
    """
    session = await db.get_session(session_id)
    if session is None:
        return False

    rows = await db.get_messages(session_id)
    _, pending = pending_tool_calls(rows)
    call = next((c for c in pending if c["id"] == tool_call_id), None)
    if call is None:
        return False

    name = tool_call_name(call)

    if action == "approve":
        # Don't run it here. Marking it approved lets the agent loop execute it
        # and stream tool_start/tool_end, so the user sees the command output.
        _approved_calls.add(tool_call_id)
        return True

    if action == "reject":
        # Feed the refusal back as a normal tool result. The model can then adapt
        # instead of the conversation dead-ending on an unanswered tool call.
        note = value.strip()
        result = ToolResult(
            output="The user rejected this tool call and it was not executed."
                   + (f" They said: {note}" if note else "")
                   + " Do not retry it; ask how to proceed or choose another approach.",
            is_error=True,
            title=f"{name} (rejected)",
        )
    elif action == "answer":
        result = ToolResult(output=value or "(no answer given)", title="answered")
    else:
        return False

    await _record(session_id, call, result)
    return True


def _accumulate(partials: dict[int, dict], deltas: list[dict]):
    """Reassemble streamed tool-call fragments keyed by their index."""
    for d in deltas:
        idx = d.get("index") or 0
        slot = partials.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if d.get("id"):
            slot["id"] = d["id"]
        if d.get("name"):
            slot["name"] = d["name"]
        if d.get("arguments"):
            slot["arguments"] += d["arguments"]


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
