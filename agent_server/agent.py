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
from agent_server.system_prompt import build_system_prompt, get_compact_prompt
from agent_server.tools.base import ToolContext, ToolResult, truncate
from agent_server.tools.question import format_prompt
from agent_server import permissions
from agent_server.tools.registry import execute_tool, get_tool, tool_schemas

# session_id -> abort signal for the in-flight run.
_aborts: dict[str, asyncio.Event] = {}
# Sessions the user chose to auto-approve for the lifetime of this process.
_runtime_auto_approve: set[str] = set()
# Individual tool calls the user approved. Consumed by the next _drain_pending
# so the approved tool runs inside the loop and streams its output like any
# other tool call, instead of executing silently in the resolve endpoint.
_approved_calls: set[str] = set()
# Sessions whose compaction prompt the user dismissed for the current run.
_compaction_snoozed: set[str] = set()

# ── Session status, for the tab-bar indicators ──────────────────────────────
# "running"  the agent is working
# "waiting"  paused on a permission prompt, question, or compaction confirm
# "idle"     nothing in flight
_status: dict[str, str] = {}
# Sessions that finished or started waiting since the user last looked at them.
_unseen: dict[str, str] = {}


def _set_status(session_id: str, status: str, notify: str = ""):
    if status == "idle":
        _status.pop(session_id, None)
    else:
        _status[session_id] = status
    if notify:
        _unseen[session_id] = notify


def session_status(session_id: str) -> str:
    return _status.get(session_id, "idle")


def status_snapshot() -> dict[str, dict]:
    ids = set(_status) | set(_unseen)
    return {
        sid: {"status": _status.get(sid, "idle"), "unseen": _unseen.get(sid, "")}
        for sid in ids
    }


def mark_seen(session_id: str):
    _unseen.pop(session_id, None)


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


def snooze_compaction(session_id: str):
    """Stop re-prompting for compaction until this run finishes."""
    _compaction_snoozed.add(session_id)


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
    ctx = ToolContext(
        session_id=session_id,
        project_dir=session["project_dir"],
        provider=session["provider"],
        model=session["model"],
        abort=abort,
    )
    _set_status(session_id, "running")
    outcome = "done"

    try:
        # Tell the client the database id of the turn it just started, so the
        # message bubble it optimistically rendered can gain its edit/retry
        # actions without a full re-render.
        rows = await db.get_messages(session_id)
        last_user = next((r for r in reversed(rows) if r["role"] == "user"), None)
        if last_user is not None:
            yield {"type": "turn_start", "user_message_id": last_user["id"]}

        async for event in _loop(session, provider, ctx, abort):
            if event["type"] in ("permission", "question", "compaction_required"):
                outcome = "waiting"
            elif event["type"] == "error":
                outcome = "error"
            yield event
    except asyncio.CancelledError:
        # Client disconnected: stop quietly, transcript is already consistent.
        _set_status(session_id, "idle")
        raise
    except Exception as e:  # noqa: BLE001
        outcome = "error"
        yield {"type": "error", "message": f"Agent error: {type(e).__name__}: {e}"}
    finally:
        _aborts.pop(session_id, None)
        if outcome == "waiting":
            _set_status(session_id, "waiting", notify="waiting")
        else:
            _compaction_snoozed.discard(session_id)
            _set_status(session_id, "idle", notify=outcome)


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

        # Offer compaction at a clean turn boundary, before spending another
        # full-context request. Snoozed for the rest of the run once the user
        # either compacts or raises the threshold.
        if session_id not in _compaction_snoozed:
            usage = await db.get_session_usage(session_id)
            if usage["threshold"] and usage["context"] >= usage["threshold"]:
                yield {
                    "type": "compaction_required",
                    "context": usage["context"],
                    "threshold": usage["threshold"],
                    "max_context": usage["max_context"],
                    "cost": round(usage["cost"], 4),
                    "instructions": await get_compact_prompt(),
                }
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
                # A large `write` can spend a long time streaming its arguments
                # with no content and no reasoning, which looks like a hang.
                # Report what is being built so the UI can show progress.
                yield {
                    "type": "tool_progress",
                    "calls": [
                        {
                            "index": i,
                            "name": p["name"],
                            "chars": len(p["arguments"]),
                        }
                        for i, p in sorted(partials.items())
                        if p["name"]
                    ],
                }
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

    shell_auto = await _auto_approves(session)

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

        if call["id"] not in _approved_calls:
            prompt = await permissions.check(
                name, args, session_id, session["project_dir"], shell_auto
            )
            if prompt is not None:
                yield {
                    "type": "permission",
                    "tool_call_id": call["id"],
                    "name": name,
                    "args": args,
                    **prompt,
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
            "diff": result.diff,
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
        # Persisted so the inline diff is still there after a page reload.
        # It is display-only and never sent back to the model.
        diff=result.diff,
        tool_title=result.title,
    )


async def resolve_pending(
    session_id: str,
    tool_call_id: str,
    action: str,
    value: str = "",
    scope: str = "once",
    grant_path: str = "",
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
        # Grant a persistent write scope before running, if the user asked for it.
        if scope == "directory" and grant_path:
            await permissions.allow_directory(session_id, grant_path)
        # Don't run it here. Marking it approved lets the agent loop execute it
        # and stream tool_start/tool_end, so the user sees the result.
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
