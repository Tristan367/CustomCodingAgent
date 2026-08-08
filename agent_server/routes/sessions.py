"""Session CRUD."""

from fastapi import APIRouter, HTTPException

from agent_server import agent
from agent_server import database as db
from agent_server.config import MODELS_BY_ID, REASONING_EFFORTS
from agent_server.models import SessionCreate, SessionUpdate
from agent_server.providers import list_providers
from agent_server.system_prompt import PROFILE_NAMES

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _validate(body: SessionCreate | SessionUpdate):
    if body.provider and body.provider not in list_providers():
        raise HTTPException(400, f"Unknown provider: {body.provider}")
    if body.model and body.model not in MODELS_BY_ID:
        raise HTTPException(400, f"Unknown model: {body.model}")
    if body.prompt_profile and body.prompt_profile not in PROFILE_NAMES:
        raise HTTPException(400, f"Unknown prompt profile: {body.prompt_profile}")
    if body.thinking_effort and body.thinking_effort not in REASONING_EFFORTS:
        raise HTTPException(400, f"Unknown thinking effort: {body.thinking_effort}")


@router.post("")
async def create_session(body: SessionCreate):
    _validate(body)
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
    _validate(body)
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    # An empty thinking_effort means "fall back to the default".
    if "thinking_effort" in updates and not updates["thinking_effort"]:
        updates["thinking_effort"] = None
    return await db.update_session(session_id, **updates)


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

