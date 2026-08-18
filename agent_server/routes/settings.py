"""Application settings: providers, bash rules, and sound control."""


import json
import re

from fastapi import APIRouter, Request

from agent_server import database as db
from agent_server.providers import get_provider, get_provider_settings_fields
from agent_server.routes.context import _home_context
from agent_server.templating import set_custom_color, set_theme, templates

router = APIRouter()


# ── Settings ────────────────────────────────────────────────────────────────

@router.post("/_settings")
async def save_settings(request: Request):
    form = await request.form()
    changed_keys: dict[str, str] = {}
    for ps in get_provider_settings_fields():
        changed = False
        for f in ps["fields"]:
            # Each provider's form posts only its own field. Absent fields are
            # the *other* providers' keys and must not be cleared by this save.
            if f["key"] not in form:
                continue
            value = str(form.get(f["key"], "")).strip()
            # A masked password field must never overwrite the stored key if it
            # ever submits bullets. An emptied field is a deliberate clear, so it
            # is saved.
            if f.get("kind") == "password" and "\u2022" in value:
                continue
            await db.set_setting(f["key"], value)
            changed_keys[f["key"]] = value
            changed = True
        if changed:
            p = get_provider(ps["key"])
            p.invalidate_key_cache()
    if changed_keys:
        # Re-seed the credentials cache with the values just saved, so the next
        # api_key() lookup does not fall through to a blocking sqlite read on the
        # event loop (the cache was just invalidated above).
        from agent_server.providers import credentials

        credentials.prime(changed_keys)
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


@router.post("/_settings/expand")
async def save_expand_setting(request: Request):
    """Which tool results the transcript opens without a click."""
    from agent_server.routes.context import _expandable_tools

    try:
        body = await request.json()
    except Exception:
        return {"ok": False}
    allowed = set(_expandable_tools())
    names = [str(v) for v in body if str(v) in allowed] if isinstance(body, list) else []
    await db.set_setting("expand_tools", json.dumps(names))
    return {"ok": True}


@router.post("/_settings/hide")
async def save_hide_flags(request: Request):
    """Whether past thinking blocks / tool calls are hidden from the transcript."""
    form = await request.form()
    for key in ("hide_thinking", "hide_tool_calls"):
        if key in form:
            val = str(form.get(key, "0"))
            await db.set_setting(key, "1" if val in ("1", "true", "on") else "0")
    return {"ok": True}


@router.post("/_settings/theme")
async def save_theme(request: Request):
    """The accent colour family: green (default), red, blue, gray, or custom."""
    form = await request.form()
    theme = str(form.get("theme", "")).strip()
    if theme not in ("green", "red", "blue", "gray", "custom"):
        return {"ok": False}
    if theme == "custom":
        custom = str(form.get("custom", "")).strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", custom):
            return {"ok": False}
        await db.set_setting("theme_custom", custom)
        set_custom_color(custom)
    await db.set_setting("theme", theme)
    set_theme(theme)
    return {"ok": True}


@router.post("/_settings/stt-model")
async def save_stt_model(request: Request):
    """Switch the whisper model; the streaming server restarts with it."""
    from pathlib import Path

    from agent_server import config, whisper_streaming

    form = await request.form()
    model = str(form.get("model", "")).strip()
    if not model or not Path(model).expanduser().is_file():
        return {"ok": False, "detail": "that model file does not exist"}
    await db.set_setting("whisper_model", model)
    config.set_whisper_model(model)
    await whisper_streaming.restart()
    return {"ok": True}
