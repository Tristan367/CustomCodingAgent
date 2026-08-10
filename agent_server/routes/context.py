"""Context builders and helpers shared by more than one route module.

The rule for this file is narrow: something belongs here when two route
modules need it. A helper used by exactly one module lives in that module. The
point is to have one obvious place for the handful of genuinely shared pieces,
not a second dumping ground.

`_home_context` and `_session_context` in particular are what every HTMX
partial re-renders through, so most handlers end by calling one of them.
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from agent_server import agent, permissions
from agent_server import database as db
from agent_server import tts as tts_service
from agent_server.compaction import should_offer_compaction
from agent_server.config import (
    DEFAULT_MODEL,
    MODELS,
    REASONING_EFFORTS,
    THRESHOLD_STEPS,
    stt_available,
)
from agent_server.conversation import parse_arguments, pending_tool_calls, tool_call_name
from agent_server.providers import (
    _providers,
    get_provider,
    get_provider_settings_fields,
    list_providers,
)
from agent_server.stt import availability as stt_availability
from agent_server.system_prompt import COMPACTION, list_prompt_names
from agent_server.tools.registry import get_tool

_SOUND_DIR = Path.home() / ".config" / "codeagent" / "sounds"
_ALLOWED_SOUND_EXTS = {".mp3", ".wav", ".ogg", ".m4a"}

# Tab order is read-modify-written, so concurrent tab opens must not interleave.
_tab_lock = asyncio.Lock()

def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower()).strip("-")[:40]


def _clamp(raw: str, low: float, high: float, fallback: float) -> float:
    try:
        return min(max(float(raw), low), high)
    except ValueError:
        return fallback


async def _sound_enabled() -> bool:
    return await db.get_setting("sound_enabled", "1") != "0"


def _ensure_sound_dir() -> Path:
    _SOUND_DIR.mkdir(parents=True, exist_ok=True)
    return _SOUND_DIR


def _list_uploaded_sounds() -> list[str]:
    d = _ensure_sound_dir()
    return sorted(f.name for f in d.iterdir() if f.suffix.lower() in _ALLOWED_SOUND_EXTS)


def _offerable_models() -> list[dict]:
    """Models that can actually be run right now, newest-configured last.

    A model is offered only when its provider has credentials, because picking
    one that cannot authenticate produces a session that fails on its first
    message with no hint as to why. Each configured custom endpoint contributes
    one entry: the provider is a property of the choice, so the form asks for
    one thing rather than letting a model and a provider disagree.

    The credential test is the provider's own `has_credentials`, which already
    knows about environment variables, so this no longer restates the mapping
    from provider name to env var and gets it wrong for new providers.
    """
    offered = []
    for model in MODELS:
        try:
            provider = get_provider(model["provider"])
        except ValueError:
            continue
        if provider.has_credentials():
            offered.append(model)

    for key, provider in _providers.items():
        if key.startswith("custom:") and provider.has_credentials():
            offered.append({
                "id": key,
                "name": f"{provider.name} (custom endpoint)",
                "provider": key,
                "needs_model_id": True,
            })
    return offered


def _start_watching(session_id: str, project_dir: str):
    from agent_server.dir_watcher import watch

    async def on_rename(sid: str, new_dir: str):
        await db.set_setting(f"session_dir:{sid}", new_dir)
        await db._execute(
            "UPDATE sessions SET project_dir = ? WHERE id = ?",
            (new_dir, sid),
        )

    watch(session_id, project_dir, on_rename)


def _stop_watching(session_id: str):
    from agent_server.dir_watcher import unwatch
    unwatch(session_id)


async def _open_tabs() -> list[str]:
    try:
        value = json.loads(await db.get_setting("open_tabs", "[]"))
        return [str(v) for v in value] if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


async def _save_tabs(ids: list[str]):
    await db.set_setting("open_tabs", json.dumps(ids))


async def _track_tab(session_id: str):
    # Read-modify-write: two tabs opened in quick succession would otherwise
    # each read the old list and the second would drop the first.
    async with _tab_lock:
        tabs = await _open_tabs()
        if session_id not in tabs:
            tabs.append(session_id)
            await _save_tabs(tabs)


async def _pending_prompt(session: dict, messages: list[dict]) -> dict | None:
    """Describe a tool call still waiting on the user, so a page reload can
    re-offer the approval instead of stranding the session."""
    _, pending = pending_tool_calls(messages)
    if not pending:
        return None
    call = pending[0]
    name = tool_call_name(call)
    args = parse_arguments(call)
    shell_auto = bool(session.get("bash_auto_approve")) or agent.runtime_auto_approve(session["id"])
    tool = get_tool(name)
    if tool and tool.pause == "permission" and name not in ("bash", "edit", "write"):
        return {
            "type": "permission",
            "tool_call_id": call["id"],
            "name": name,
            "args": args,
            "message": f"Run custom tool '{name}'?",
            "kind": "custom_tool",
        }
    prompt = await permissions.check(
        name, args, session["id"], session["project_dir"], shell_auto
    )
    if prompt is None:
        return None
    return {
        "type": "permission",
        "tool_call_id": call["id"],
        "name": name,
        "args": args,
        **prompt,
    }


async def _session_context(session: dict) -> dict:
    usage = await db.get_session_usage(session["id"])
    messages = await db.get_messages(session["id"])
    return {
        "session": session,
        "messages": messages,
        "compactions": await db.get_compactions(session["id"]),
        # Only models that can actually authenticate, so switching to one does
        # not produce a session that fails on its next message.
        "models": _offerable_models(),
        "profiles": await list_prompt_names(),
        "compact_profiles": await list_prompt_names(COMPACTION),
        "efforts": REASONING_EFFORTS,
        "usage": usage,
        "should_compact": await should_offer_compaction(session["id"]),
        "auto_approve": bool(session.get("bash_auto_approve"))
        or agent.runtime_auto_approve(session["id"]),
        "stt_enabled": stt_available(),
        "pending": await _pending_prompt(session, messages),
        "sound_enabled": await _sound_enabled(),
        "uploaded_sounds": _list_uploaded_sounds(),
        "threshold_steps": THRESHOLD_STEPS,
        "allowed_dirs": await permissions.list_allowed(session["id"]),
    }


async def _home_context(
    error: str = "", clone_id: str = "", edit_script: str = "", saved: bool = False
) -> dict:
    settings = await db.get_all_settings()
    clone_defaults = {}
    if clone_id:
        clone_session = await db.get_session(clone_id)
        if clone_session:
            base_name = clone_session["name"]
            # Find next available "(N)" suffix
            existing = {s["name"] for s in await db.list_sessions()}
            n = 1
            while f"{base_name} ({n})" in existing:
                n += 1
            clone_defaults = {
                "clone_name": f"{base_name} ({n})",
                "clone_project_dir": clone_session["project_dir"],
                "clone_model": clone_session.get("model", DEFAULT_MODEL),
                "clone_provider": clone_session.get("provider", "deepseek"),
                "clone_profile": clone_session.get("prompt_profile", "default"),
                "clone_compact": clone_session.get("compact_profile", "default"),
                "clone_thinking": clone_session.get("thinking_effort", "high"),
                "clone_bash_auto": clone_session.get("bash_auto_approve", 0),
            }
    provider_settings = []
    for ps in get_provider_settings_fields():
        f_list = []
        for f in ps["fields"]:
            raw = settings.get(f["key"], "")
            is_pw = f.get("kind") == "password"
            preview = ""
            if raw and is_pw:
                # Shows the first and last quarter so a key is recognisable.
                # Half of it reaches the page either way, which is more than
                # identification needs -- worth revisiting.
                edge = max(0, len(raw) // 4)
                preview = raw[:edge] + "\u2026" + raw[len(raw) - edge:]
            f_list.append(dict(f, value=("\u2022" * 12), has_value=bool(raw) and is_pw, preview=preview))
        provider_settings.append({"name": ps["name"], "fields": f_list})

    custom_endpoints = await db.list_custom_endpoints()
    filtered_models = _offerable_models()

    return {
        "sessions": await db.list_sessions(),
        # Scripts are a home-page panel rather than a page of their own: they
        # are a handful of buttons, not a destination.
        "scripts": await db.list_scripts(),
        "edit_script": edit_script,
        "saved": saved,
        "sound_enabled": await _sound_enabled(),
        "uploaded_sounds": _list_uploaded_sounds(),
        "stt": stt_availability(),
        "tts": tts_service.availability(),
        "settings": settings,
        "provider_settings": provider_settings,
        "custom_endpoints": custom_endpoints,
        "providers": list_providers(),
        "models": filtered_models,
        "profiles": await list_prompt_names(),
        "compact_profiles": await list_prompt_names(COMPACTION),
        "default_model": DEFAULT_MODEL,
        "clone_defaults": clone_defaults,
        "default_name": f"temp session {datetime.now().strftime('%-m-%-d-%Y')}",
        "error": error,
    }
