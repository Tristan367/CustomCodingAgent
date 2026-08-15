"""Session CRUD."""

from fastapi import APIRouter, HTTPException

from agent_server import agent
from agent_server import database as db
from agent_server.config import (
    MIN_COMPACT_THRESHOLD,
    REASONING_EFFORTS,
    is_known_model,
    provider_for_model,
)
from agent_server.models import SessionCreate, SessionUpdate
from agent_server.providers import list_providers
from agent_server.system_prompt import list_prompt_names

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


async def _validate(body: SessionCreate | SessionUpdate):
    if body.provider and body.provider not in list_providers():
        raise HTTPException(400, f"Unknown provider: {body.provider}")
    # A custom endpoint serves whatever its operator configured, so its model
    # ids cannot be checked against the built-in table. Everything else must be
    # a model this app knows how to price and size a context window for -- the
    # hand-configured table or the ids discovered from the DeepSeek endpoint.
    known_model = is_known_model(body.model)
    custom_provider = (body.provider or "").startswith("custom:")
    if body.model and not known_model and not custom_provider:
        raise HTTPException(400, f"Unknown model: {body.model}")
    # A model implies its provider. Letting the two be set independently is how
    # a session ends up asking DeepSeek for an Anthropic model.
    if known_model and body.provider and provider_for_model(body.model) != body.provider:
        raise HTTPException(
            400,
            f"{body.model} is served by {provider_for_model(body.model)}, "
            f"not {body.provider}.",
        )
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
        provider=body.provider or provider_for_model(body.model),
        model=body.model,
        prompt_profile=body.prompt_profile,
        subagent_model=body.subagent_model,
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
    # Switching model switches provider with it. The settings form only sends a
    # model, so without this a session moved to Claude kept asking DeepSeek for
    # it -- the same mismatch the creation form used to produce.
    if updates.get("model") and is_known_model(updates["model"]) and "provider" not in updates:
        updates["provider"] = provider_for_model(updates["model"])
    # An empty thinking_effort means "fall back to the default".
    if "thinking_effort" in updates and not updates["thinking_effort"]:
        updates["thinking_effort"] = None
    # The threshold is a floor, not a ceiling: it may sit above the model's
    # real window when the user wants to compact by hand instead of on a timer.
    if "compact_threshold" in updates:
        updates["compact_threshold"] = max(MIN_COMPACT_THRESHOLD, updates["compact_threshold"])
    return await db.update_session(session_id, **updates)


@router.get("/{session_id}/changes")
async def session_changes(session_id: str):
    """Files changed since the last user message, for the persistent summary."""
    if await db.get_session(session_id) is None:
        raise HTTPException(404, "Session not found")
    return await db.get_turn_changes(session_id)


@router.get("/{session_id}/system-prompt")
async def get_system_prompt(session_id: str):
    """What this session is running with, and what it will switch to.

    Both are returned separately. Collapsing them into one field meant a queued
    change was displayed as though it were already in force, with no way to see
    the text actually being sent.
    """
    session = await db.get_session(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    from agent_server.system_prompt import session_system_prompt

    return {
        "live": await session_system_prompt(session),
        "pending": session.get("pending_system_prompt") or None,
        "profile": session.get("prompt_profile") or "default",
        "custom": bool(session.get("prompt_custom")),
        "started": bool(await db.get_messages(session_id)),
    }


@router.delete("/{session_id}/system-prompt/pending")
async def discard_pending_prompt(session_id: str):
    """Drop a queued change and stay on the prompt already in use."""
    if await db.get_session(session_id) is None:
        raise HTTPException(404, "Session not found")
    await db.update_session(session_id, pending_system_prompt=None)
    return {"ok": True}


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

    from agent_server.system_prompt import session_system_prompt

    live = await session_system_prompt(session)
    pending = session.get("pending_system_prompt") or None

    # Saying "saved, it will apply at the next compaction" when the text is
    # identical to what is already running is a lie that repeats every click.
    if text == live:
        if pending:
            await db.update_session(session_id, pending_system_prompt=None)
            return {"ok": True, "status": "cancelled", "prompt": text,
                    "custom": bool(custom), "deferred": False}
        return {"ok": True, "status": "unchanged", "prompt": text,
                "custom": bool(custom), "deferred": False}
    if text == pending:
        return {"ok": True, "status": "already_queued", "prompt": text,
                "custom": bool(custom), "deferred": True}

    # Changing the prompt of a conversation that has already started rewrites
    # the front of the prefix and re-bills every token of it. Before the first
    # message there is nothing cached, so it applies straight away; after that
    # it waits for compaction, which rewrites the prefix anyway.
    if await db.get_messages(session_id):
        await db.update_session(
            session_id, pending_system_prompt=text, prompt_custom=custom
        )
        return {"ok": True, "status": "queued", "prompt": text,
                "custom": bool(custom), "deferred": True}

    await db.update_session(
        session_id, system_prompt=text, prompt_custom=custom, pending_system_prompt=None
    )
    return {"ok": True, "status": "applied", "prompt": text,
            "custom": bool(custom), "deferred": False}


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

