"""Sound upload, listing, playback, and deletion."""

import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse

from agent_server.routes.context import _ALLOWED_SOUND_EXTS, _ensure_sound_dir

router = APIRouter()


@router.post("/_settings/sounds/upload")
async def upload_sound(request: Request):
    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "filename"):
        return {"ok": False, "error": "No file provided"}
    name = Path(file.filename).name
    ext = Path(name).suffix.lower()
    if ext not in _ALLOWED_SOUND_EXTS:
        return {"ok": False, "error": f"Unsupported format: {ext}. Use .mp3, .wav, .ogg, or .m4a."}
    safe = re.sub(r"[^\w.-]", "_", name)
    d = _ensure_sound_dir()
    dest = d / safe
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        return {"ok": False, "error": "File too large (max 5 MB)"}
    dest.write_bytes(data)
    return {"ok": True, "name": safe}


@router.delete("/_settings/sounds/{name}")
async def delete_sound(name: str):
    d = _ensure_sound_dir()
    path = d / re.sub(r"[^\w.-]", "_", name)
    if path.is_file():
        path.unlink()
        return {"ok": True}
    return {"ok": False, "error": "Not found"}


@router.get("/_settings/sounds/{name}/play")
async def serve_sound(name: str):
    d = _ensure_sound_dir()
    path = d / re.sub(r"[^\w.-]", "_", name)
    if not path.is_file():
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(path)
