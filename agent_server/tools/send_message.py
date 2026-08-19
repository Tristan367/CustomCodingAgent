"""Send a message to another session by name."""

from agent_server import database as db
from agent_server.tools.base import ToolContext, ToolResult


async def send_message(ctx: ToolContext, *, session: str, message: str, **_) -> ToolResult:
    title = f"message to {session[:40] if session else '?'}"
    target_name = (session or "").strip()
    body = (message or "").strip()
    if not target_name:
        return ToolResult.error("a target session name is required", title)
    if not body:
        return ToolResult.error("a message body is required", title)

    target = await db.get_session_by_name(target_name)
    if target is None:
        names = [s["name"] for s in await db.list_sessions()]
        available = ", ".join(names) if names else "(none)"
        return ToolResult.error(
            f"no session named '{target_name}'. Available: {available}", title,
        )
    if target["id"] == ctx.session_id:
        return ToolResult.error("you cannot message yourself", title)

    sender = await db.get_session(ctx.session_id)
    sender_name = (sender or {}).get("name") or ctx.session_id

    # Wake the target if it is idle so the mail is actually picked up.
    from agent_server import agent

    # Stop-all aborts every run and empties the mailbox. A send already in
    # flight at that moment would otherwise land its message *after* the
    # clear-out and wake the target straight back up -- so two sessions
    # messaging each other could survive the one control that is meant to end
    # everything at once.
    if ctx.abort.is_set():
        return ToolResult.error("cancelled before the message was sent", title)

    was_running = agent.is_running(target["id"])
    if was_running:
        # Defer to the next turn boundary; inserting mid-tool-loop would corrupt
        # the conversation. Delivered by _flush_mailbox.
        await db.send_mail(target["id"], ctx.session_id, sender_name, body)
    else:
        # Idle: persist now so the message is visible immediately and becomes
        # the next turn's input.
        await db.add_message(
            target["id"], "user", agent.mail_content(sender_name, body),
            mail_from=sender_name,
        )
    agent.start_run(target["id"])

    reply_note = (
        f"Message sent to {target['name']}. They are working now and will reply "
        f"to you shortly."
        if was_running
        else f"Message sent to {target['name']}. They will reply to you shortly."
    )
    return ToolResult(output=reply_note, title=title)
