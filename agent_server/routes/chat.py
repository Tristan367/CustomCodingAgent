"""Chat, tool resolution, compaction, transcription, and image endpoints."""

import uuid
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from agent_server import agent
from agent_server import database as db
from agent_server import permissions
from agent_server import stt as stt_service
from agent_server.compaction import compact_session, should_offer_compaction
from agent_server.config import MIN_COMPACT_THRESHOLD, MODELS_BY_ID, UPLOAD_DIR
from agent_server.models import ChatRequest, EditMessageRequest, ResolveRequest
from agent_server import vision as vision_engine

router = APIRouter(prefix="/api", tags=["chat"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "Content-Type": "text/event-stream; charset=utf-8",
}


def _stream(session_id: str, request: Request) -> StreamingResponse:
    """Start the turn and follow it over SSE.

    The run is owned by the server, not by this request. Disconnecting -- a
    reload, a tab switch, closing the laptop -- unsubscribes and nothing more;
    the turn keeps going and its results are still recorded. Only an explicit
    cancel stops it.
    """
    agent.start_run(session_id)

    async def generator() -> AsyncIterator[str]:
        async for event in agent.subscribe(session_id):
            yield agent.sse(event)

    return StreamingResponse(generator(), media_type="text/event-stream", headers=SSE_HEADERS)


def _attach(session_id: str) -> StreamingResponse:
    """Follow a turn that is already running, without restarting it."""

    async def generator() -> AsyncIterator[str]:
        if agent.active_run(session_id) is None:
            yield agent.sse({"type": "stream_end"})
            return
        async for event in agent.subscribe(session_id, replay=False):
            yield agent.sse(event)

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
    images: list[UploadFile] = File(default=[]),
):
    """Send a message with one or more attached images.

    The images are saved and referenced by path; the agent decides for itself
    whether and how to look at them with the `vision` tool. Earlier versions ran
    vision eagerly here and injected a description, which forced the user to
    write the vision prompt and threw away the original image.
    """
    await _require_session(session_id)
    text = message.strip()
    attachments = [i for i in images if i and i.filename]

    if not attachments:
        if not text:
            raise HTTPException(400, "Message or image is required")
        await db.add_message(session_id, "user", text)
        return _stream(session_id, request)

    saved: list[str] = []
    for upload in attachments[:6]:
        try:
            saved.append(await _save_image(session_id, upload))
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Could not read {upload.filename}: {e}") from e

    lines = [
        f"[Image attached: {path} ({vision_engine.describe_image_file(path)})]"
        for path in saved
    ]
    noun = "image" if len(saved) == 1 else "images"
    lines.append(
        f"\nUse the `vision` tool on {'this path' if len(saved) == 1 else 'these paths'} "
        f"to see the {noun}."
    )
    content = "\n".join(lines)
    if text:
        content += f"\n\n{text}"

    await db.add_message(session_id, "user", content)
    return _stream(session_id, request)


@router.post("/sessions/{session_id}/resolve")
async def resolve(session_id: str, request: Request, body: ResolveRequest):
    """Answer a paused tool call (shell approval or question) and resume."""
    await _require_session(session_id)

    if body.action == "approve" and body.scope == "session":
        agent.set_runtime_auto_approve(session_id, True)

    ok = await agent.resolve_pending(
        session_id, body.tool_call_id, body.action, body.value,
        scope=body.scope, grant_path=body.grant_path,
    )
    if not ok:
        raise HTTPException(409, "That tool call is no longer pending.")
    return _stream(session_id, request)


@router.post("/sessions/{session_id}/continue")
async def continue_run(session_id: str, request: Request):
    """Resume the loop without adding a message.

    Used after the user resolves a compaction prompt, and by retry.
    """
    await _require_session(session_id)
    return _stream(session_id, request)


@router.post("/sessions/{session_id}/queue")
async def queue(session_id: str, payload: dict):
    """Add a message to a turn that is already running."""
    await _require_session(session_id)
    text = (payload.get("message") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "reason": "Empty message"}, status_code=400)
    if not agent.queue_message(session_id, text):
        return JSONResponse({"ok": False, "reason": "Nothing is running"}, status_code=409)
    return {"ok": True}


@router.get("/sessions/{session_id}/attach")
async def attach(session_id: str):
    await _require_session(session_id)
    return _attach(session_id)


@router.post("/sessions/{session_id}/cancel")
async def cancel(session_id: str):
    return {"ok": agent.request_abort(session_id)}


@router.get("/status")
async def status():
    """Per-session run state, polled by the tab bar."""
    return {"sessions": agent.status_snapshot()}


@router.post("/sessions/{session_id}/seen")
async def seen(session_id: str):
    agent.mark_seen(session_id)
    return {"ok": True}


@router.get("/usage")
async def total_usage():
    return await db.get_total_cost()


# ── Write permissions ───────────────────────────────────────────────────────

@router.get("/sessions/{session_id}/write-dirs")
async def list_write_dirs(session_id: str):
    await _require_session(session_id)
    return {"dirs": await permissions.list_allowed(session_id)}


@router.post("/sessions/{session_id}/write-dirs")
async def add_write_dir(session_id: str, payload: dict = Body(default={})):
    await _require_session(session_id)
    path = str(payload.get("path", "")).strip()
    if not path:
        raise HTTPException(400, "A path is required")
    resolved = Path(path).expanduser()
    if not resolved.is_dir():
        raise HTTPException(400, f"Not a directory: {resolved}")
    if permissions.is_denied(resolved):
        raise HTTPException(400, f"{resolved} can never be granted")
    return {"dirs": await permissions.allow_directory(session_id, str(resolved))}


@router.delete("/sessions/{session_id}/write-dirs")
async def remove_write_dir(session_id: str, path: str):
    await _require_session(session_id)
    return {"dirs": await permissions.revoke_directory(session_id, path)}


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
async def compact(
    session_id: str,
    request: Request,
    summary: str = Form(""),
    extra_instructions: str = Form(""),
    resume: bool = Form(False),
):
    """Compact the conversation. With `resume`, stream the continuation after."""
    await _require_session(session_id)
    result = await compact_session(session_id, summary, extra_instructions)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    agent.snooze_compaction(session_id)
    if resume:
        return _stream(session_id, request)
    return JSONResponse(result)


@router.post("/sessions/{session_id}/compact-threshold")
async def set_compact_threshold(
    session_id: str,
    request: Request,
    threshold: int = Form(...),
    resume: bool = Form(False),
):
    """Raise or lower the point at which compaction is offered."""
    session = await _require_session(session_id)
    ceiling = MODELS_BY_ID.get(session["model"], {}).get("context", 1_000_000)
    value = max(MIN_COMPACT_THRESHOLD, min(int(threshold), ceiling))
    await db.update_session(session_id, compact_threshold=value)
    agent.snooze_compaction(session_id)
    if resume:
        return _stream(session_id, request)
    return JSONResponse({"ok": True, "threshold": value})


# ── Retry and edit ──────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/messages/{message_id}/retry")
async def retry_message(session_id: str, message_id: int, request: Request):
    """Discard everything after a user message and run that turn again."""
    await _require_session(session_id)
    rows = await db.get_messages(session_id, include_compacted=True)
    target = next((r for r in rows if r["id"] == message_id), None)
    if target is None or target["role"] != "user":
        raise HTTPException(400, "Can only retry from a user message")
    await db.delete_messages_after(session_id, message_id)
    return _stream(session_id, request)


@router.post("/sessions/{session_id}/messages/{message_id}/edit")
async def edit_message(
    session_id: str,
    message_id: int,
    request: Request,
    body: EditMessageRequest,
):
    """Rewrite a user message and re-run from there."""
    await _require_session(session_id)
    rows = await db.get_messages(session_id, include_compacted=True)
    target = next((r for r in rows if r["id"] == message_id), None)
    if target is None or target["role"] != "user":
        raise HTTPException(400, "Can only edit a user message")
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "Message cannot be empty")
    await db.update_message(message_id, content)
    await db.delete_messages_after(session_id, message_id)
    return _stream(session_id, request)


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


@router.get("/files/image")
async def serve_image(path: str):
    """Serve an attached or captured image.

    Restricted to the directories this app writes into, so a crafted path cannot
    turn the endpoint into an arbitrary file read.
    """
    from agent_server.vision import CAPTURE_DIR

    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        raise HTTPException(400, "Bad path") from None

    roots = [UPLOAD_DIR.resolve(), CAPTURE_DIR.resolve()]
    if not any(_within(resolved, root) for root in roots):
        raise HTTPException(403, "Outside the allowed image directories")
    if not resolved.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(resolved)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ── Images ──────────────────────────────────────────────────────────────────

ALLOWED_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".heic"}


async def _save_image(session_id: str, upload: UploadFile) -> str:
    """Normalise an upload to PNG and store it under the session's directory.

    Browsers happily hand over a WebP named `.jpg`, and the vision backend
    rejects WebP outright, so the bytes are decoded and re-encoded rather than
    trusting the extension.
    """
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix and suffix not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(400, f"Unsupported image type: {suffix}")

    raw = await upload.read()
    if not raw:
        raise HTTPException(400, "Empty image upload")
    if len(raw) > 40 * 1024 * 1024:
        raise HTTPException(400, "Image is larger than 40 MB")

    try:
        data = await vision_engine.normalize_in_thread(raw)
    except vision_engine.VisionError as e:
        raise HTTPException(400, str(e)) from e

    directory = UPLOAD_DIR / session_id
    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(upload.filename or "image").stem[:40] or "image"
    safe = "".join(c for c in stem if c.isalnum() or c in "-_") or "image"
    path = directory / f"{safe}-{uuid.uuid4().hex[:6]}.png"
    path.write_bytes(data)
    return str(path)
