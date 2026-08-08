"""Conversation compaction.

The hard constraint: an assistant message carrying `tool_calls` and the `tool`
messages answering it form one atomic unit. Compacting part of that unit leaves
either dangling tool calls or orphaned results, and every subsequent request in
the session fails with a 400. The previous implementation sliced at a fixed
offset and could split a group; this one only ever cuts on a group boundary.
"""

from agent_server import database as db
from agent_server.config import COMPACT_THRESHOLD_TOKENS
from agent_server.conversation import normalize_tool_calls
from agent_server.providers import get_provider
from agent_server.system_prompt import get_compact_prompt

# Turns kept verbatim at the tail so recent context survives compaction.
KEEP_RECENT_GROUPS = 3


def group_messages(rows: list[dict]) -> list[list[dict]]:
    """Split a transcript into atomic units that must not be broken apart."""
    groups: list[list[dict]] = []
    current: list[dict] = []

    for row in rows:
        role = row["role"]
        if role == "user":
            if current:
                groups.append(current)
            current = [row]
        elif role == "tool":
            # Always belongs with the assistant turn that requested it.
            if current:
                current.append(row)
            else:
                groups.append([row])
        else:  # assistant / system
            if current:
                current.append(row)
            else:
                current = [row]
            if not normalize_tool_calls(row.get("tool_calls")):
                groups.append(current)
                current = []

    if current:
        groups.append(current)
    return groups


def split_for_compaction(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (messages_to_summarise, messages_to_keep) cut on a group boundary."""
    groups = group_messages(rows)
    if len(groups) <= KEEP_RECENT_GROUPS:
        return [], rows
    head = groups[: -KEEP_RECENT_GROUPS]
    tail = groups[-KEEP_RECENT_GROUPS:]

    # Never keep a leading orphan: the kept window must not start with a tool result.
    while tail and tail[0] and tail[0][0]["role"] == "tool":
        head.append(tail.pop(0))
    if not tail:
        return [], rows

    return [m for g in head for m in g], [m for g in tail for m in g]


def render_transcript(rows: list[dict], per_message_limit: int = 4000) -> str:
    lines: list[str] = []
    for row in rows:
        role = row["role"]
        content = (row.get("content") or "").strip()
        calls = normalize_tool_calls(row.get("tool_calls"))
        if calls:
            names = ", ".join(
                f"{c['function']['name']}({c['function']['arguments'][:200]})" for c in calls
            )
            lines.append(f"[assistant called tools] {names}")
        if not content:
            continue
        if len(content) > per_message_limit:
            content = content[:per_message_limit] + " ...[truncated]"
        label = f"tool:{row.get('tool_name') or '?'}" if role == "tool" else role
        lines.append(f"[{label}] {content}")
    return "\n\n".join(lines)


async def should_offer_compaction(session_id: str) -> bool:
    usage = await db.get_session_usage(session_id)
    return usage.get("context", 0) >= COMPACT_THRESHOLD_TOKENS


async def compact_session(session_id: str, manual_summary: str = "") -> dict:
    session = await db.get_session(session_id)
    if session is None:
        return {"ok": False, "reason": "Session not found"}

    rows = await db.get_messages(session_id)
    to_compact, kept = split_for_compaction(rows)
    if not to_compact:
        return {"ok": False, "reason": "Not enough completed turns to compact yet."}

    provider = get_provider(session["provider"])

    if manual_summary.strip():
        summary = manual_summary.strip()
    else:
        if not provider.has_credentials():
            return {"ok": False, "reason": "No API key configured."}
        instructions = await get_compact_prompt()
        summary = ""
        async for event in provider.chat_completion(
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": render_transcript(to_compact)},
            ],
            tools=[],
            model=session["model"],
            thinking_effort="low",
        ):
            if event["type"] == "content":
                summary += event["text"]
            elif event["type"] == "error":
                return {"ok": False, "reason": event["message"]}
        summary = summary.strip()
        if not summary:
            return {"ok": False, "reason": "The model returned an empty summary."}

    original_tokens = sum(r.get("token_count") or 0 for r in to_compact)
    compressed_tokens = provider.count_tokens([{"role": "system", "content": summary}])

    await db.add_compaction(
        session_id=session_id,
        summary_text=summary,
        range_start=to_compact[0]["id"],
        range_end=to_compact[-1]["id"],
        original_tokens=original_tokens,
        compressed_tokens=compressed_tokens,
    )
    await db.mark_messages_compacted(session_id, [r["id"] for r in to_compact])

    return {
        "ok": True,
        "compacted": len(to_compact),
        "kept": len(kept),
        "original_tokens": original_tokens,
        "compressed_tokens": compressed_tokens,
    }
