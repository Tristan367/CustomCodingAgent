"""Session CRUD."""

from fastapi import APIRouter, HTTPException

from agent_server import agent
from agent_server import database as db
from agent_server.config import MODELS_BY_ID, REASONING_EFFORTS
from agent_server.models import SessionCreate, SessionUpdate
from agent_server.providers import list_providers
from agent_server.system_prompt import list_prompt_names

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


async def _validate(body: SessionCreate | SessionUpdate):
    if body.provider and body.provider not in list_providers():
        raise HTTPException(400, f"Unknown provider: {body.provider}")
    if body.model and body.model not in MODELS_BY_ID:
        raise HTTPException(400, f"Unknown model: {body.model}")
    if body.prompt_profile and body.prompt_profile not in await list_prompt_names():
        raise HTTPException(400, f"Unknown prompt profile: {body.prompt_profile}")
    if body.thinking_effort and body.thinking_effort not in REASONING_EFFORTS:
        raise HTTPException(400, f"Unknown thinking effort: {body.thinking_effort}")


@router.post("")
async def create_session(body: SessionCreate):
    await _validate(body)
    return await db.create_session(
        name=body.name.strip() or "Untitled",
        project_dir=body.project_dir,
        provider=body.provider,
        model=body.model,
        prompt_profile=body.prompt_profile,
        thinking_effort=body.thinking_effort,
    )


@router.get("")
async def list_sessions(archived: bool = False):
    return await db.list_sessions(archived=archived)


@router.get("/{session_id}")
async def get_session(session_id: str):
    session = await db.get_session(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    return session


@router.patch("/{session_id}")
async def update_session(session_id: str, body: SessionUpdate):
    if await db.get_session(session_id) is None:
        raise HTTPException(404, "Session not found")
    await _validate(body)
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    # An empty thinking_effort means "fall back to the default".
    if "thinking_effort" in updates and not updates["thinking_effort"]:
        updates["thinking_effort"] = None
    return await db.update_session(session_id, **updates)


@router.get("/{session_id}/system-prompt")
async def get_system_prompt(session_id: str):
    """The exact prompt this session is running with."""
    session = await db.get_session(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    from agent_server.system_prompt import session_system_prompt

    started = bool(await db.get_messages(session_id))
    pending = session.get("pending_system_prompt")
    return {
        # Show the queued text if there is one: that is what the user last
        # asked for, and what they will want to edit further.
        "prompt": pending or await session_system_prompt(session),
        "pending": bool(pending),
        "custom": bool(session.get("prompt_custom")),
        "started": started,
    }


@router.post("/{session_id}/system-prompt")
async def set_system_prompt(session_id: str, payload: dict):
    """Override the prompt for this session only.

    Changing it invalidates the cached prefix once, then the new text caches in
    its turn, so this is cheap to do between turns and expensive to do often.
    """
    session = await db.get_session(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    text = (payload.get("prompt") or "").strip()
    custom = 1
    if not text:
        # Empty restores whatever the shared prompt renders to now, and gives up
        # the exemption from "apply to existing".
        from agent_server.system_prompt import build_system_prompt

        text = await build_system_prompt(
            session.get("prompt_profile") or "default", session["project_dir"], session_id
        )
        custom = 0

    # Changing the prompt of a conversation that has already started rewrites
    # the front of the prefix and re-bills every token of it. Before the first
    # message there is nothing cached, so it applies straight away; after that
    # it waits for compaction, which rewrites the prefix anyway.
    started = bool(await db.get_messages(session_id))
    if started:
        await db.update_session(
            session_id, pending_system_prompt=text, prompt_custom=custom
        )
        return {"ok": True, "prompt": text, "custom": bool(custom), "deferred": True}

    await db.update_session(
        session_id, system_prompt=text, prompt_custom=custom, pending_system_prompt=None
    )
    return {"ok": True, "prompt": text, "custom": bool(custom), "deferred": False}


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    if await db.get_session(session_id) is None:
        raise HTTPException(404, "Session not found")
    agent.request_abort(session_id)
    await db.delete_session(session_id)
    agent.forget_session(session_id)
    return {"ok": True}


@router.get("/{session_id}/messages")
async def get_messages(session_id: str):
    if await db.get_session(session_id) is None:
        raise HTTPException(404, "Session not found")
    return await db.get_messages(session_id)

