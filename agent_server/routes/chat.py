"""Chat, tool resolution, compaction, transcription, and image endpoints."""

import asyncio
import uuid
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from agent_server import agent
from agent_server import database as db
from agent_server import stt as stt_service
from agent_server.compaction import compact_session, should_offer_compaction
from agent_server.config import UPLOAD_DIR
from agent_server.models import ChatRequest, ResolveRequest
from agent_server.providers import get_provider
from agent_server.tools.vision import describe_image

router = APIRouter(prefix="/api", tags=["chat"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "Content-Type": "text/event-stream; charset=utf-8",
}


def _stream(session_id: str, request: Request) -> StreamingResponse:
    """Wrap the agent loop in SSE, aborting the run if the client goes away."""

    async def generator() -> AsyncIterator[str]:
        try:
            async for event in agent.run(session_id):
                if await request.is_disconnected():
                    agent.request_abort(session_id)
                    break
                yield agent.sse(event)
        except asyncio.CancelledError:
            agent.request_abort(session_id)
            raise
        finally:
            yield agent.sse({"type": "stream_end"})

    return StreamingResponse(generator(), media_type="text/event-stream", headers=SSE_HEADERS)


async def _require_session(session_id: str) -> dict:
    session = await db.get_session(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    return session


# ── Chat ────────────────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/chat")
async def chat(session_id: str, request: Request, body: ChatRequest):
    await _require_session(session_id)
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Message is required")
    # Persist before streaming. This is the step whose absence caused the model
    # to be prompted with no user turn at all.
    await db.add_message(session_id, "user", text)
    return _stream(session_id, request)


@router.post("/sessions/{session_id}/chat-with-image")
async def chat_with_image(
    session_id: str,
    request: Request,
    message: str = Form(""),
    image: UploadFile | None = File(None),
    vision_prompt: str = Form("Describe this image in detail."),
):
    session = await _require_session(session_id)
    text = message.strip()

    if image is None or not image.filename:
        if not text:
            raise HTTPException(400, "Message or image is required")
        await db.add_message(session_id, "user", text)
        return _stream(session_id, request)

    path = await _save_upload(image)
    provider = get_provider(session["provider"])

    if provider.supports_vision():
        content = f"[image attached: {path}]"
    else:
        try:
            description = await describe_image(str(path), vision_prompt)
            content = (
                f"The user attached an image. A vision model was asked "
                f'"{vision_prompt}" and reported:\n\n{description}'
            )
        except Exception as e:  # noqa: BLE001
            content = f"[the user attached an image, but vision analysis failed: {e}]"

    if text:
        content += f"\n\nUser message: {text}"
    await db.add_message(session_id, "user", content)
    return _stream(session_id, request)


@router.post("/sessions/{session_id}/resolve")
async def resolve(session_id: str, request: Request, body: ResolveRequest):
    """Answer a paused tool call (shell approval or question) and resume."""
    await _require_session(session_id)

    if body.action == "approve" and body.scope == "session":
        agent.set_runtime_auto_approve(session_id, True)

    ok = await agent.resolve_pending(session_id, body.tool_call_id, body.action, body.value)
    if not ok:
        raise HTTPException(409, "That tool call is no longer pending.")
    return _stream(session_id, request)


@router.post("/sessions/{session_id}/cancel")
async def cancel(session_id: str):
    return {"ok": agent.request_abort(session_id)}


@router.get("/sessions/{session_id}/state")
async def state(session_id: str):
    session = await _require_session(session_id)
    usage = await db.get_session_usage(session_id)
    return {
        "running": agent.is_running(session_id),
        "auto_approve": bool(session.get("bash_auto_approve")) or agent.runtime_auto_approve(session_id),
        "auto_approve_persisted": bool(session.get("bash_auto_approve")),
        "auto_approve_runtime": agent.runtime_auto_approve(session_id),
        "usage": usage,
        "should_compact": await should_offer_compaction(session_id),
    }


@router.post("/sessions/{session_id}/auto-approve")
async def set_auto_approve(session_id: str, payload: dict = Body(default={})):
    """Toggle shell auto-approval. `persist` writes it to the session; otherwise
    it lasts only for this server process."""
    await _require_session(session_id)
    enabled = bool(payload.get("enabled"))
    if payload.get("persist"):
        await db.update_session(session_id, bash_auto_approve=1 if enabled else 0)
        agent.set_runtime_auto_approve(session_id, False)
    else:
        agent.set_runtime_auto_approve(session_id, enabled)
    return {"ok": True, "enabled": enabled}


@router.post("/sessions/{session_id}/compact")
async def compact(session_id: str, summary: str = Form("")):
    await _require_session(session_id)
    result = await compact_session(session_id, summary)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


# ── Speech to text ──────────────────────────────────────────────────────────

@router.get("/stt/status")
async def stt_status():
    return stt_service.availability()


@router.post("/stt")
async def transcribe(audio: UploadFile = File(...)):
    suffix = Path(audio.filename or "").suffix or ".webm"
    data = await audio.read()
    try:
        text = await stt_service.transcribe(data, suffix)
    except stt_service.STTError as e:
        raise HTTPException(400, str(e)) from e
    return {"text": text}


# ── Images ──────────────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/analyze-image")
async def analyze_image(
    session_id: str,
    image: UploadFile = File(...),
    prompt: str = Form("Describe this image in detail."),
):
    await _require_session(session_id)
    path = await _save_upload(image)
    try:
        description = await describe_image(str(path), prompt)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Vision analysis failed: {e}") from e
    return {"description": description}


async def _save_upload(image: UploadFile) -> Path:
    suffix = Path(image.filename or "").suffix or ".png"
    if suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        raise HTTPException(400, f"Unsupported image type: {suffix}")
    path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(await image.read())
    return path
