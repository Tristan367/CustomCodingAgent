"""FastAPI application: page routes, HTMX partials, and settings."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent_server import agent
from agent_server import database as db
from agent_server.compaction import should_offer_compaction
from agent_server.config import (
    DEFAULT_MODEL,
    MODELS,
    REASONING_EFFORTS,
    THRESHOLD_STEPS,
    stt_available,
)
from agent_server.stt import availability as stt_availability
from agent_server.conversation import (
    normalize_tool_calls,
    parse_arguments,
    pending_tool_calls,
    tool_call_name,
)
from agent_server.database import close as close_db
from agent_server.database import init_db
from agent_server.providers import list_providers
from agent_server.providers.deepseek import invalidate_key_cache
from agent_server.routes import chat, sessions
from agent_server.system_prompt import (
    BUILTIN_PROFILES,
    COMPACT_PROMPT_DEFAULT,
    PROFILE_NAMES,
)
from agent_server import permissions
from agent_server.tools.registry import TOOLS, get_tool
from agent_server.vision import rig_available

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "web_ui" / "templates"
STATIC_DIR = BASE_DIR / "web_ui" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await close_db()


app = FastAPI(title="CodeAgent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(sessions.router)
app.include_router(chat.router)

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


# ── Template filters ────────────────────────────────────────────────────────

def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def humantime(value: str) -> str:
    """Relative for recent timestamps, absolute once it stops being useful."""
    dt = _parse(value)
    if dt is None:
        return value
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 604800:
        return f"{int(secs // 86400)}d ago"
    return dt.strftime("%b %-d, %Y")


def clocktime(value: str) -> str:
    dt = _parse(value)
    if dt is None:
        return value
    return dt.strftime("%-I:%M %p").lower().replace("am", "AM").replace("pm", "PM")


def tildepath(value: str) -> str:
    """Render /home/you/projects/x as ~/projects/x."""
    if not value:
        return value
    home = str(Path.home())
    if value == home:
        return "~"
    if value.startswith(home + "/"):
        return "~" + value[len(home):]
    return value


templates.env.filters["humantime"] = humantime
_ATTACHMENT_RE = re.compile(r"^\[Image attached: (?P<path>.+?) \((?P<meta>[^)]*)\)\]$", re.M)
_ATTACHMENT_HINT = re.compile(r"^Use the `vision` tool on th(?:is path|ese paths) to see the images?\.$", re.M)


def extract_attachments(content: str) -> list[dict]:
    """Attachment paths recorded in a user message, for rendering as thumbnails."""
    return [
        {"path": m.group("path"), "meta": m.group("meta")}
        for m in _ATTACHMENT_RE.finditer(content or "")
    ]


def strip_attachments(content: str) -> str:
    """The message without the plumbing the model needs but the user does not."""
    text = _ATTACHMENT_RE.sub("", content or "")
    text = _ATTACHMENT_HINT.sub("", text)
    return text.strip()


def difflines(diff: str) -> list[tuple[str, str]]:
    """Tag each diff line with a CSS class, matching renderDiff() in app.js so a
    reloaded transcript looks identical to the streamed one."""
    out = []
    for line in (diff or "").rstrip("\n").split("\n"):
        if line.startswith("@@"):
            cls = "diff-hunk"
        elif line.startswith("+++") or line.startswith("---"):
            cls = "diff-meta"
        elif line.startswith("+"):
            cls = "diff-add"
        elif line.startswith("-"):
            cls = "diff-del"
        else:
            cls = "diff-ctx"
        out.append((cls, line))
    return out


def diffstat_counts(diff: str) -> tuple[int, int]:
    added = sum(1 for ln in (diff or "").splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in (diff or "").splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return added, removed


def duration_label(ms: int | None) -> str:
    """Only worth showing once a call is slow enough to have been noticed."""
    if not ms or ms < 1000:
        return ""
    return f"{ms / 1000:.1f}s"


templates.env.filters["clocktime"] = clocktime
templates.env.filters["tildepath"] = tildepath
templates.env.filters["attachments"] = extract_attachments
templates.env.filters["withoutattachments"] = strip_attachments
templates.env.filters["toolcalls"] = normalize_tool_calls
templates.env.filters["difflines"] = difflines
templates.env.filters["diffstat"] = diffstat_counts
templates.env.filters["duration"] = duration_label


# ── Shared context ──────────────────────────────────────────────────────────

async def _sound_enabled() -> bool:
    return await db.get_setting("sound_enabled", "1") != "0"


async def _session_context(session: dict) -> dict:
    usage = await db.get_session_usage(session["id"])
    messages = await db.get_messages(session["id"])
    return {
        "session": session,
        "messages": messages,
        "compactions": await db.get_compactions(session["id"]),
        "models": MODELS,
        "profiles": PROFILE_NAMES,
        "efforts": REASONING_EFFORTS,
        "usage": usage,
        "should_compact": await should_offer_compaction(session["id"]),
        "auto_approve": bool(session.get("bash_auto_approve"))
        or agent.runtime_auto_approve(session["id"]),
        "stt_enabled": stt_available(),
        "pending": await _pending_prompt(session, messages),
        "sound_enabled": await _sound_enabled(),
        "threshold_steps": THRESHOLD_STEPS,
        "allowed_dirs": await permissions.list_allowed(session["id"]),
    }


async def _pending_prompt(session: dict, messages: list[dict]) -> dict | None:
    """Describe a tool call still waiting on the user, so a page reload can
    re-offer the approval or question instead of stranding the session."""
    _, pending = pending_tool_calls(messages)
    if not pending:
        return None
    call = pending[0]
    name = tool_call_name(call)
    args = parse_arguments(call)
    tool = get_tool(name)
    if tool is not None and tool.pause == "question":
        return {
            "type": "question",
            "tool_call_id": call["id"],
            "question": args.get("question", ""),
            "options": args.get("options") or [],
            "multiple": bool(args.get("multiple")),
        }
    shell_auto = bool(session.get("bash_auto_approve")) or agent.runtime_auto_approve(session["id"])
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


async def _home_context(error: str = "") -> dict:
    return {
        "sessions": await db.list_sessions(),
        "totals": await db.get_total_cost(),
        "sound_enabled": await _sound_enabled(),
        "stt": stt_availability(),
        "settings": await db.get_all_settings(),
        "providers": list_providers(),
        "models": MODELS,
        "profiles": PROFILE_NAMES,
        "default_model": DEFAULT_MODEL,
        "error": error,
    }


# ── Pages ───────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html", context=await _home_context()
    )


@app.get("/sessions/{session_id}")
async def session_page(request: Request, session_id: str):
    session = await db.get_session(session_id)
    if session is None:
        return RedirectResponse("/", status_code=303)
    await _track_tab(session_id)
    return templates.TemplateResponse(
        request=request, name="session.html", context=await _session_context(session)
    )


@app.get("/_session/{session_id}")
async def session_partial(request: Request, session_id: str):
    session = await db.get_session(session_id)
    if session is None:
        return HTMLResponse("Session not found", status_code=404)
    await _track_tab(session_id)
    return templates.TemplateResponse(
        request=request, name="session_content.html", context=await _session_context(session)
    )


@app.get("/_messages/{session_id}")
async def messages_partial(request: Request, session_id: str):
    session = await db.get_session(session_id)
    if session is None:
        return HTMLResponse("Session not found", status_code=404)
    return templates.TemplateResponse(
        request=request, name="chat_messages.html", context=await _session_context(session)
    )


@app.get("/_session_meta/{session_id}")
async def session_meta_partial(request: Request, session_id: str):
    session = await db.get_session(session_id)
    if session is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(
        request=request, name="components/session_meta.html",
        context=await _session_context(session),
    )


# ── Tabs ────────────────────────────────────────────────────────────────────

_tab_lock = asyncio.Lock()


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


@app.get("/_tab_bar")
async def tab_bar(request: Request, current: str = ""):
    tabs = await _open_tabs()
    all_sessions = {s["id"]: s for s in await db.list_sessions()}
    ordered = [all_sessions[sid] for sid in tabs if sid in all_sessions]
    # Drop ids whose sessions were deleted.
    if len(ordered) != len(tabs):
        await _save_tabs([s["id"] for s in ordered])
    return templates.TemplateResponse(
        request=request, name="components/tab_bar.html",
        context={"sessions": ordered, "current_session_id": current},
    )


@app.post("/_tab_close/{session_id}")
async def tab_close(session_id: str):
    tabs = await _open_tabs()
    if session_id in tabs:
        tabs.remove(session_id)
        await _save_tabs(tabs)
    return {"ok": True, "tabs": tabs}


@app.post("/_tab_order")
async def tab_order(payload: dict):
    ids = [str(i) for i in payload.get("ids", [])]
    await _save_tabs(ids)
    return {"ok": True}


# ── Settings ────────────────────────────────────────────────────────────────

@app.post("/_settings")
async def save_settings(request: Request, deepseek_api_key: str = Form("")):
    key = deepseek_api_key.strip()
    # The masked placeholder means "unchanged"; don't overwrite the real key.
    if key and "\u2022" not in key:
        await db.set_setting("deepseek_api_key", key)
        invalidate_key_cache()
    return templates.TemplateResponse(
        request=request, name="index_content.html", context=await _home_context()
    )


@app.post("/_settings/sound")
async def save_sound_setting(enabled: str = Form("1")):
    await db.set_setting("sound_enabled", "1" if enabled in ("1", "true", "on") else "0")
    return {"ok": True}


@app.post("/_create_session")
async def create_session_form(
    request: Request,
    name: str = Form(...),
    project_dir: str = Form(...),
    model: str = Form(DEFAULT_MODEL),
    prompt_profile: str = Form("default"),
    thinking_effort: str = Form(""),
):
    directory = Path(project_dir).expanduser()
    if not directory.is_dir():
        return templates.TemplateResponse(
            request=request, name="index_content.html",
            context=await _home_context(f"Not a directory: {project_dir}"),
        )
    if prompt_profile == "visual-verify" and not await rig_available():
        return templates.TemplateResponse(
            request=request, name="index_content.html",
            context=await _home_context(
                "The vision rig is not reachable, so the visual-verify profile cannot be used. "
                "Start it, or pick another profile."
            ),
        )

    session = await db.create_session(
        name=name.strip() or directory.name,
        project_dir=str(directory.resolve()),
        model=model,
        prompt_profile=prompt_profile,
        thinking_effort=thinking_effort or None,
    )
    return RedirectResponse(f"/sessions/{session['id']}", status_code=303)


@app.get("/prompts")
async def prompts_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="prompts.html", context=await _prompts_context()
    )


@app.post("/_save_prompts")
async def save_prompts(request: Request):
    form = await request.form()
    for profile in PROFILE_NAMES:
        key = f"profile_{profile}"
        if key in form:
            value = str(form[key]).strip()
            # Blank restores the built-in default.
            await db.set_setting(key, "" if value == BUILTIN_PROFILES[profile].strip() else value)
    await db.set_setting("user_prefs", str(form.get("user_prefs", "")).strip())
    compact = str(form.get("compact_prompt", "")).strip()
    await db.set_setting("compact_prompt", "" if compact == COMPACT_PROMPT_DEFAULT.strip() else compact)
    return templates.TemplateResponse(
        request=request, name="prompts.html", context=await _prompts_context(saved=True)
    )


async def _prompts_context(saved: bool = False) -> dict:
    profiles = {
        name: (await db.get_setting(f"profile_{name}", "") or BUILTIN_PROFILES[name])
        for name in PROFILE_NAMES
    }
    schemas = [t.schema() for t in TOOLS.values()]
    return {
        "profiles": profiles,
        "user_prefs": await db.get_setting("user_prefs", ""),
        "compact_prompt": await db.get_setting("compact_prompt", "") or COMPACT_PROMPT_DEFAULT,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "pause": t.pause or "",
                "tokens": len(json.dumps(t.schema())) // 4,
            }
            for t in TOOLS.values()
        ],
        "tools_tokens": len(json.dumps(schemas)) // 4,
        "saved": saved,
        "sound_enabled": await _sound_enabled(),
    }


# ── Directory browser ───────────────────────────────────────────────────────

@app.get("/_browse")
async def browse(request: Request, dir: str = "", show_hidden: bool = False):
    path = Path(dir).expanduser() if dir else Path.home()
    try:
        path = path.resolve()
    except OSError:
        path = Path.home()
    if not path.is_dir():
        path = path.parent if path.parent.is_dir() else Path.home()

    crumbs = []
    node = path
    while True:
        crumbs.append({"name": node.name or str(node), "path": str(node)})
        if node.parent == node:
            break
        node = node.parent
    crumbs.reverse()

    entries = []
    error = ""
    try:
        for entry in sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith(".") and not show_hidden:
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            entries.append({"name": entry.name, "path": str(entry), "is_dir": is_dir})
    except PermissionError:
        error = f"Permission denied: {path}"

    return templates.TemplateResponse(
        request=request, name="components/_browse_list.html",
        context={
            "current": str(path),
            "parent": str(path.parent) if path.parent != path else None,
            "crumbs": crumbs,
            "entries": entries[:500],
            "truncated": len(entries) > 500,
            "error": error,
            "show_hidden": show_hidden,
        },
    )


@app.post("/_browse/mkdir")
async def browse_mkdir(request: Request, dir: str = Form(...), name: str = Form(...)):
    parent = Path(dir).expanduser().resolve()
    safe = Path(name.strip()).name
    if not safe or safe in (".", ".."):
        raise HTTPException(400, "Invalid directory name")
    if not parent.is_dir():
        raise HTTPException(400, f"Not a directory: {parent}")
    target = parent / safe
    if target.exists():
        raise HTTPException(400, f"Already exists: {safe}")
    try:
        target.mkdir(parents=True)
    except OSError as e:
        raise HTTPException(400, f"Could not create directory: {e}") from e
    return await browse(request, dir=str(target))
