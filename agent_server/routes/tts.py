"""Text-to-speech: turn an assistant reply into audio the browser can play."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from agent_server import database as db
from agent_server import tts as tts_service

router = APIRouter(prefix="/api/tts", tags=["tts"])


class PlanBody(BaseModel):
    text: str


class SpeakBody(BaseModel):
    text: str
    voice: str = ""
    speed: float = 1.0


@router.get("/status")
async def tts_status():
    status = tts_service.availability()
    status["voice"] = await db.get_setting("tts_voice", status["default_voice"])
    status["speed"] = float(await db.get_setting("tts_speed", "1.0"))
    status["volume"] = float(await db.get_setting("tts_volume", "0.66"))
    status["tone"] = float(await db.get_setting("tts_tone", "20000"))
    return status


@router.post("/plan")
async def tts_plan(body: PlanBody):
    """Split a reply into the sentences that will be spoken.

    The client drives chunking, because only it knows how much audio is still
    buffered, so it needs the sentence list up front rather than a fixed
    server-side carve-up.
    """
    return {"sentences": tts_service.plan(body.text)}


@router.post("/speak")
async def tts_speak(body: SpeakBody):
    try:
        audio = await tts_service.synth(body.text, body.voice, body.speed)
    except tts_service.TTSError as e:
        raise HTTPException(400, str(e)) from e
    # no-store: these are regenerated freely and there is no point filling the
    # browser cache with one entry per sentence of every reply.
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )
