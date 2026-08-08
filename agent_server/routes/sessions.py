from fastapi import APIRouter, HTTPException
from agent_server import database as db
from agent_server.models import SessionCreate, SessionUpdate, SessionResponse

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse)
async def create_session(body: SessionCreate):
    session = await db.create_session(
        name=body.name,
        project_dir=body.project_dir,
        provider=body.provider,
        model=body.model,
        prompt_profile=body.prompt_profile,
    )
    return session


@router.get("")
async def list_sessions(archived: bool = False):
    return await db.list_sessions(archived=archived)


@router.get("/{session_id}")
async def get_session(session_id: str):
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return session


@router.patch("/{session_id}")
async def update_session(session_id: str, body: SessionUpdate):
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    return await db.update_session(session_id, **updates)


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    await db.delete_session(session_id)
    return {"ok": True}


@router.get("/{session_id}/messages")
async def get_messages(session_id: str):
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return await db.get_messages(session_id)
