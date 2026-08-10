"""Application settings: providers, bash rules, sound control, and TTS."""


from fastapi import APIRouter, Form, Request

from agent_server import database as db
from agent_server.providers import get_provider, get_provider_settings_fields
from agent_server.routes.context import _clamp, _home_context
from agent_server.templating import templates

router = APIRouter()


# ── Settings ────────────────────────────────────────────────────────────────

@router.post("/_settings")
async def save_settings(request: Request):
    form = await request.form()
    for ps in get_provider_settings_fields():
        changed = False
        for f in ps["fields"]:
            value = str(form.get(f["key"], "")).strip()
            if not value:
                continue
            if f.get("kind") == "password" and "\u2022" in value:
                continue
            await db.set_setting(f["key"], value)
            changed = True
        if changed:
            p = get_provider(ps["key"])
            p.invalidate_key_cache()
    return templates.TemplateResponse(
        request=request, name="index_content.html", context=await _home_context()
    )


@router.post("/_settings/sound")
async def save_sound_setting(request: Request):
    form = await request.form()
    if form.get("enabled") is not None:
        enabled = str(form.get("enabled", "1"))
        await db.set_setting("sound_enabled", "1" if enabled in ("1", "true", "on") else "0")
    if "sound" in form:
        await db.set_setting("sound_choice", str(form.get("sound", "click")))
    if "volume" in form:
        await db.set_setting("sound_volume", str(form.get("volume", "0.5")))
    return {"ok": True}


@router.post("/_settings/tts")
async def save_tts_settings(
    voice: str = Form(""), speed: str = Form(""), volume: str = Form(""),
    tone: str = Form(""),
):
    """Each field is optional so the controls can save independently."""
    if voice:
        await db.set_setting("tts_voice", voice)
    if speed:
        await db.set_setting("tts_speed", str(_clamp(speed, 0.5, 2.0, 1.0)))
    if volume:
        await db.set_setting("tts_volume", str(_clamp(volume, 0.0, 1.0, 0.66)))
    if tone:
        await db.set_setting("tts_tone", str(int(_clamp(tone, 2000, 20000, 20000))))
    return {"ok": True}
