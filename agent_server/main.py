from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse
from pathlib import Path

from agent_server.database import init_db
from agent_server.routes import sessions, chat, files
from agent_server import database as db
from agent_server.system_prompt import PROFILE_NAMES
from agent_server.providers import list_providers

BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="CustomCodingAgent", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web_ui" / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "web_ui" / "templates"))

# Add human-readable date filter
from datetime import datetime, timezone
import time as _time

def _format_date(value: str) -> str:
    if not value:
        return ""
    try:
        # Parse ISO format (with or without timezone)
        dt_str = value.replace("Z", "+00:00")
        if "T" in dt_str:
            dt = datetime.fromisoformat(dt_str)
        else:
            dt = datetime.strptime(dt_str, "%Y-%m-%d")
        # Convert to local time
        if dt.tzinfo is not None:
            local_offset = -_time.timezone if not _time.daylight else -_time.altzone
            local_tz = timezone(__import__('datetime').timedelta(seconds=local_offset))
            dt = dt.astimezone(local_tz)
        return dt.strftime("%b %-d, %Y")
    except Exception:
        return value

templates.env.filters["humandate"] = _format_date


def _get_hidden_tabs(request: Request) -> set[str]:
    """Parse hidden_tabs cookie into a set of session IDs."""
    cookie = request.cookies.get("hidden_tabs", "")
    return set(cookie.split(",")) if cookie else set()


def _filter_sessions(sessions: list, hidden: set[str]) -> list:
    return [s for s in sessions if s["id"] not in hidden]


def _get_tab_sessions(request: Request) -> list:
    """Return sessions for tab bar only (excludes hidden)."""
    import asyncio
    loop = asyncio.get_event_loop()
    sessions = loop.run_until_complete(db.list_sessions())
    return _filter_sessions(sessions, _get_hidden_tabs(request))


async def _get_tab_sessions_async(request: Request) -> list:
    sessions = await db.list_sessions()
    return _filter_sessions(sessions, _get_hidden_tabs(request))

app.include_router(sessions.router)
app.include_router(chat.router)
app.include_router(files.router)

DEEPSEEK_MODELS = [
    {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"},
    {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"},
]


# ── Page routes ──

@app.get("/")
async def index(request: Request):
    sessions_list = await db.list_sessions()
    settings = await db.get_all_settings()
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"sessions": sessions_list, "settings": settings,
                 "providers": list_providers(), "models": DEEPSEEK_MODELS,
                 "profiles": PROFILE_NAMES},
    )


@app.get("/_session/{session_id}")
async def session_partial(request: Request, session_id: str):
    """HTMX partial: session page content without base layout."""
    session = await db.get_session(session_id)
    if not session:
        return HTMLResponse("Session not found", status_code=404)
    messages = await db.get_messages(session_id, include_compacted=False)
    compactions = await db.get_compactions(session_id)
    return templates.TemplateResponse(
        request=request, name="session_content.html",
        context={"session": session, "messages": messages,
                 "compactions": compactions,
                 "models": DEEPSEEK_MODELS, "profiles": PROFILE_NAMES},
    )


@app.get("/sessions/{session_id}")
async def session_page(request: Request, session_id: str):
    session = await db.get_session(session_id)
    if not session:
        return RedirectResponse("/")
    messages = await db.get_messages(session_id, include_compacted=False)
    compactions = await db.get_compactions(session_id)
    all_sessions = await db.list_sessions()
    return templates.TemplateResponse(
        request=request, name="session.html",
        context={"session": session, "messages": messages,
                 "compactions": compactions, "sessions": all_sessions,
                 "models": DEEPSEEK_MODELS, "profiles": PROFILE_NAMES},
    )


@app.get("/_messages/{session_id}")
async def get_messages_html(request: Request, session_id: str):
    session = await db.get_session(session_id)
    if not session:
        return HTMLResponse("Session not found", status_code=404)
    messages = await db.get_messages(session_id, include_compacted=False)
    compactions = await db.get_compactions(session_id)
    return templates.TemplateResponse(
        request=request, name="chat_messages.html",
        context={"messages": messages, "compactions": compactions, "session": session},
    )


@app.get("/_tab_bar")
async def get_tab_bar_html(request: Request, current: str = ""):
    all_sessions = await db.list_sessions()
    open_ids = await _get_open_tabs()
    ordered = []
    for sid in open_ids:
        match = next((s for s in all_sessions if s["id"] == sid), None)
        if match:
            ordered.append(match)
    return templates.TemplateResponse(
        request=request, name="components/tab_bar.html",
        context={"sessions": ordered, "current_session_id": current},
    )


@app.post("/_tab_open/{session_id}")
async def tab_open(session_id: str):
    """Called when a session is opened — adds it to the open tabs list."""
    open_ids = await _get_open_tabs()
    if session_id not in open_ids:
        open_ids.append(session_id)
    await _set_open_tabs(open_ids)
    return {"ok": True}


@app.post("/_tab_close/{session_id}")
async def tab_close(session_id: str):
    """Called when a tab is closed — removes it from open tabs."""
    open_ids = await _get_open_tabs()
    if session_id in open_ids:
        open_ids.remove(session_id)
    await _set_open_tabs(open_ids)
    return {"ok": True}


async def _get_open_tabs() -> list[str]:
    import json
    raw = await db.get_setting("open_tabs", "[]")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


async def _set_open_tabs(ids: list[str]):
    import json
    await db.set_setting("open_tabs", json.dumps(ids))


# ── Settings routes ──

@app.post("/_settings")
async def save_settings(
    request: Request,
    deepseek_api_key: str = Form(""),
):
    if deepseek_api_key:
        await db.set_setting("deepseek_api_key", deepseek_api_key)

    sessions_list = await db.list_sessions()
    settings = await db.get_all_settings()
    return templates.TemplateResponse(
        request=request, name="index_content.html",
        context={"sessions": sessions_list, "settings": settings,
                 "providers": list_providers(), "models": DEEPSEEK_MODELS,
                 "profiles": PROFILE_NAMES},
    )


@app.get("/_browse")
async def browse_partial(request: Request, dir: str = ""):
    """HTMX partial: directory browser."""
    import httpx
    from pathlib import Path
    import os

    path = Path(dir).expanduser().resolve() if dir else Path.home()
    if not path.exists() or not path.is_dir():
        path = Path.home()

    # Breadcrumbs
    crumbs = []
    p = path
    while True:
        crumbs.append({"name": p.name or str(p), "path": str(p)})
        if p.parent == p:
            break
        p = p.parent
    crumbs.reverse()

    # List entries (directories first, files after)
    entries = []
    try:
        for entry in sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith("."):
                continue
            entries.append({
                "name": entry.name,
                "path": str(entry),
                "is_dir": entry.is_dir(),
            })
    except PermissionError:
        return HTMLResponse("Permission denied", status_code=403)

    return templates.TemplateResponse(
        request=request, name="components/_browse_list.html",
        context={"current": str(path), "parent": str(path.parent) if path != path.parent else None,
                 "crumbs": crumbs, "entries": entries},
    )


@app.post("/_browse/mkdir")
async def browse_mkdir(request: Request, dir: str = Form(...), name: str = Form(...)):
    """HTMX: create a directory and refresh the browser."""
    from pathlib import Path
    safe_name = Path(name).name
    if not safe_name or safe_name.startswith("."):
        return HTMLResponse("Invalid name", status_code=400)
    new_path = Path(dir).expanduser().resolve() / safe_name
    if new_path.exists():
        return HTMLResponse(f"Already exists: {safe_name}", status_code=400)
    new_path.mkdir(parents=True)
    # Redirect back to the parent dir browser
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/_browse?dir={dir}", status_code=303)


@app.get("/prompts")
async def prompts_page(request: Request):
    """System prompt manager page."""
    from agent_server.system_prompt import PROFILES, PROFILE_NAMES
    from agent_server.tools.registry import BASE_TOOLS, VISION_TOOL_DEF
    profiles = {}
    for pn in PROFILE_NAMES:
        profiles[pn] = await db.get_setting(f"profile_{pn}", PROFILES[pn])
    user_prefs = await db.get_setting("user_prefs", "")
    sessions_list = await db.list_sessions()

    all_tools = BASE_TOOLS + [VISION_TOOL_DEF]
    import json
    tools_json = json.dumps(all_tools, indent=2)
    compact_prompt = await db.get_setting("compact_prompt", "Summarize this conversation concisely. Keep all important facts, decisions, and code changes.")

    return templates.TemplateResponse(
        request=request, name="prompts.html",
        context={"profiles": profiles, "user_prefs": user_prefs,
                 "sessions": sessions_list, "tools_json": tools_json,
                 "compact_prompt": compact_prompt},
    )


@app.post("/_save_prompts")
async def save_prompts(request: Request):
    """Save edited system prompts and user preferences."""
    from agent_server.system_prompt import PROFILES, PROFILE_NAMES
    form = await request.form()

    for profile_name in PROFILE_NAMES:
        key = f"profile_{profile_name}"
        if key in form:
            await db.set_setting(key, form[key])

    user_prefs = form.get("user_prefs", "")
    await db.set_setting("user_prefs", user_prefs)

    compact_prompt = form.get("compact_prompt", "")
    if compact_prompt:
        await db.set_setting("compact_prompt", compact_prompt)

    # Reload with updated values
    updated_profiles = {}
    for pn in PROFILE_NAMES:
        updated_profiles[pn] = await db.get_setting(f"profile_{pn}", PROFILES[pn])
    user_prefs = await db.get_setting("user_prefs", "")
    sessions_list = await db.list_sessions()

    import json
    from agent_server.tools.registry import BASE_TOOLS, VISION_TOOL_DEF
    all_tools = BASE_TOOLS + [VISION_TOOL_DEF]
    tools_json = json.dumps(all_tools, indent=2)
    compact_prompt = await db.get_setting("compact_prompt", "Summarize this conversation concisely. Keep all important facts, decisions, and code changes.")

    return templates.TemplateResponse(
        request=request, name="prompts.html",
        context={"profiles": updated_profiles, "user_prefs": user_prefs,
                 "sessions": sessions_list, "tools_json": tools_json,
                 "compact_prompt": compact_prompt},
    )


@app.post("/_create_session")
async def create_session_form(
    request: Request,
    name: str = Form(...),
    project_dir: str = Form(...),
    model: str = Form("deepseek-v4-pro"),
    prompt_profile: str = Form("default"),
):
    # Pre-flight: if visual-verify profile, check vision rig is reachable
    if prompt_profile == "visual-verify":
        rig_ok = await _check_vision_rig()
        if not rig_ok:
            sessions_list = await db.list_sessions()
            settings = await db.get_all_settings()
            return templates.TemplateResponse(
                request=request, name="index_content.html",
                context={"sessions": sessions_list, "settings": settings,
                         "providers": list_providers(), "models": DEEPSEEK_MODELS,
                         "profiles": PROFILE_NAMES,
                         "error": "Vision rig (vision-host.local) is not reachable. Start it and try again, or use a different profile."},
            )

    session = await db.create_session(
        name=name, project_dir=project_dir, provider="deepseek",
        model=model, prompt_profile=prompt_profile,
    )
    return RedirectResponse(f"/sessions/{session['id']}", status_code=303)


async def _check_vision_rig() -> bool:
    """Check if vision-host.local:11434 is responding."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("http://vision-host.local:11434/api/tags")
            return resp.status_code == 200
    except Exception:
        return False
