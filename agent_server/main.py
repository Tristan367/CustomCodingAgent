"""FastAPI application: page routes, HTMX partials, and settings."""

import asyncio
import html
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent_server import agent, permissions
from agent_server import database as db
from agent_server import tts as tts_service
from agent_server.compaction import should_offer_compaction
from agent_server.config import (
    DEFAULT_MODEL,
    MODELS,
    REASONING_EFFORTS,
    THRESHOLD_STEPS,
    resolve_model_choice,
    stt_available,
)
from agent_server.conversation import (
    normalize_tool_calls,
    parse_arguments,
    pending_tool_calls,
    tool_call_name,
)
from agent_server.database import close as close_db
from agent_server.database import init_db
from agent_server.providers import (
    _providers,
    get_provider,
    get_provider_settings_fields,
    list_providers,
)
from agent_server.routes import chat, sessions, tts
from agent_server.stt import availability as stt_availability
from agent_server.system_prompt import (
    COMPACTION,
    PROTECTED_PROMPT,
    SYSTEM,
    build_system_prompt,
    list_prompt_names,
    migrate_prompts,
)
from agent_server.tools.registry import TOOLS, get_tool, tool_schemas

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "web_ui" / "templates"
STATIC_DIR = BASE_DIR / "web_ui" / "static"


async def _warm_vision():
    """Bring the vision rig up in the background if its machine is switched on.

    Deliberately quiet: if vision-host is off there is nothing to report and nothing to
    retry, and the first vision call will try again anyway.
    """
    from agent_server import vision

    try:
        ready, note = await vision.ensure_rig()
        if ready and note:
            print(f"[vision] {note}")
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await migrate_prompts()
    from agent_server.providers import credentials, load_custom_endpoint_providers
    from agent_server.tools.custom import load_custom_tools

    # Fill the key cache from the async connection, so no provider has to open
    # its own blocking sqlite handle on the event loop to find its key.
    credentials.prime(await db.get_all_settings())
    problems = await load_custom_tools()
    for problem in problems:
        print(f"[tools] {problem}")
    await load_custom_endpoint_providers()
    # Background: startup must not wait on a machine that may be off.
    warm = asyncio.create_task(_warm_vision())

    yield

    warm.cancel()
    from agent_server import vision
    from agent_server.tools import browser

    # Stop in-flight turns before closing the database underneath them. A run
    # is a server-owned task, so shutdown used to leave them writing into a
    # connection that had just been closed, losing the assistant message and
    # raising into a background task nobody was watching.
    await agent.shutdown()
    await vision.unload_model()
    await vision.close_client()
    await browser.close_browser()
    await close_db()


app = FastAPI(title="CodeAgent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(sessions.router)
app.include_router(chat.router)
app.include_router(tts.router)

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
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone()


def humantime(value: str) -> str:
    """Relative for recent timestamps, absolute once it stops being useful."""
    dt = _parse(value)
    if dt is None:
        return value
    delta = datetime.now(UTC) - dt.astimezone(UTC)
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


def _render_bash_rules(raw: str) -> str:
    """Format bash rules JSON for the human-readable textarea."""
    try:
        rules = json.loads(raw or "[]")
        return "\n".join(f"{r['pattern']} → {r['action']}" for r in rules)
    except Exception:
        return raw


templates.env.filters["render_bash_rules"] = _render_bash_rules


# ── Shared context ──────────────────────────────────────────────────────────

async def _sound_enabled() -> bool:
    return await db.get_setting("sound_enabled", "1") != "0"


def _list_uploaded_sounds() -> list[str]:
    d = _ensure_sound_dir()
    return sorted(f.name for f in d.iterdir() if f.suffix.lower() in _ALLOWED_SOUND_EXTS)


# ── Directory rename watcher ────────────────────────────────────────────────


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


async def _home_context(error: str = "", clone_id: str = "") -> dict:
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
                l = len(raw)
                a = max(0, l // 4)
                b = max(0, l - l // 4)
                preview = raw[:a] + "\u2026" + raw[b:]
            f_list.append(dict(f, value=("\u2022" * 12), has_value=bool(raw) and is_pw, preview=preview))
        provider_settings.append({"name": ps["name"], "fields": f_list})

    custom_endpoints = await db.list_custom_endpoints()
    filtered_models = _offerable_models()

    return {
        "sessions": await db.list_sessions(),
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


# ── Pages ───────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request, clone: str = ""):
    return templates.TemplateResponse(
        request=request, name="index.html", context=await _home_context(clone_id=clone)
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
    _stop_watching(session_id)
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


@app.post("/_settings/bash_rules")
async def save_bash_rules(request: Request):
    data = await request.json()
    await db.set_setting("bash_rules", json.dumps(data))
    return {"ok": True}


@app.post("/_settings/sound")
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


_SOUND_DIR = Path.home() / ".config" / "codeagent" / "sounds"
_ALLOWED_SOUND_EXTS = {".mp3", ".wav", ".ogg", ".m4a"}


def _ensure_sound_dir() -> Path:
    _SOUND_DIR.mkdir(parents=True, exist_ok=True)
    return _SOUND_DIR


@app.get("/_settings/sounds")
async def list_uploaded_sounds():
    d = _ensure_sound_dir()
    files = sorted(
        [f.name for f in d.iterdir() if f.suffix.lower() in _ALLOWED_SOUND_EXTS]
    )
    return {"sounds": files}


@app.post("/_settings/sounds/upload")
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


@app.delete("/_settings/sounds/{name}")
async def delete_sound(name: str):
    d = _ensure_sound_dir()
    path = d / re.sub(r"[^\w.-]", "_", name)
    if path.is_file():
        path.unlink()
        return {"ok": True}
    return {"ok": False, "error": "Not found"}


@app.get("/_settings/sounds/{name}/play")
async def serve_sound(name: str):
    d = _ensure_sound_dir()
    path = d / re.sub(r"[^\w.-]", "_", name)
    if not path.is_file():
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(path)


@app.post("/_init")
async def init_project(request: Request):
    """Auto-detect project structure and write a rules file."""
    form = await request.form()
    project_dir = str(form.get("dir", "")).strip()
    if not project_dir:
        return {"ok": False, "error": "No directory provided"}
    p = Path(project_dir).expanduser().resolve()
    if not p.is_dir():
        return {"ok": False, "error": f"Not a directory: {p}"}

    rules_path = p / "AGENTS.md"
    content = _generate_rules(p)
    rules_path.write_text(content)

    return {"ok": True, "path": str(rules_path), "preview": content[:500]}


def _generate_rules(p: Path) -> str:
    """Scan a directory and produce a concise AGENTS.md."""
    try:
        entries = list(p.iterdir())
    except (OSError, PermissionError):
        return f"# Project rules\n\nCould not scan {p}: permission denied or unreadable.\n"
    files = {f.name for f in entries if f.is_file()}
    lines = ["# Project rules (auto-generated)", ""]
    lines.append(f"Generated from {p.name} at {datetime.now().strftime('%Y-%m-%d')}.")
    lines.append("")

    has_pkg = False
    if "package.json" in files:
        has_pkg = True
        try:
            import json as _json
            pkg = _json.loads((p / "package.json").read_text())
            name = pkg.get("name", p.name)
            lines.append(f"- **Project**: {name}")
            if pkg.get("scripts"):
                lines.append("- **Scripts**: " + ", ".join(f"`{k}`" for k in list(pkg["scripts"].keys())[:8]))
        except Exception:
            lines.append("- **Project**: Node.js (package.json)")
    if "tsconfig.json" in files:
        lines.append("- **Language**: TypeScript")
    if "requirements.txt" in files or "pyproject.toml" in files or "setup.py" in files:
        lines.append("- **Language**: Python")
    if "Cargo.toml" in files:
        lines.append("- **Language**: Rust")
    if "go.mod" in files:
        lines.append("- **Language**: Go")
    if "Makefile" in files:
        lines.append("- **Build**: Make")
    if "Dockerfile" in files:
        lines.append("- **Deploy**: Docker")

    # Test framework detection
    if any(f.startswith(".eslint") for f in files):
        lines.append("- **Lint**: ESLint")
    if "pyproject.toml" in files and has_pkg:
        lines.append("- **Lint/Format**: Check pyproject.toml for ruff/black config")
    if ".pylintrc" in files:
        lines.append("- **Lint**: Pylint")
    if "jest.config" in str(files) or "vitest.config" in str(files) or ".jest." in str(files):
        lines.append("- **Test**: Jest/Vitest")
    if "pytest" in str(files) or (p / "tests").is_dir() or (p / "test").is_dir():
        lines.append("- **Test**: Pytest")
    if "cargo" in str(files) and (p / "tests").is_dir():
        lines.append("- **Test**: Cargo test")

    # Git
    if (p / ".git").is_dir():
        lines.append("- **VCS**: Git — commit small, atomic changes with descriptive messages")

    lines.append("")
    lines.append("## Conventions")
    lines.append("")
    lines.append("- Read existing code before writing new code. Match the existing style.")
    lines.append("- Prefer the project's existing patterns over what you remember from elsewhere.")
    lines.append("- Run the project's tests after changes. If no tests exist, verify manually.")
    lines.append("- Delete unused code. Don't leave commented-out blocks or dead paths.")
    lines.append("")

    return "\n".join(lines)


@app.post("/_settings/tts")
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


def _clamp(raw: str, low: float, high: float, fallback: float) -> float:
    try:
        return min(max(float(raw), low), high)
    except ValueError:
        return fallback


@app.post("/_create_session")
async def create_session_form(
    request: Request,
    name: str = Form(...),
    project_dir: str = Form(...),
    model: str = Form(DEFAULT_MODEL),
    prompt_profile: str = Form("default"),
    compact_profile: str = Form("default"),
    thinking_effort: str = Form(""),
    custom_model: str = Form(""),
):
    directory = Path(project_dir).expanduser()
    if not directory.is_dir():
        return templates.TemplateResponse(
            request=request, name="index_content.html",
            context=await _home_context(f"Not a directory: {project_dir}"),
        )
    # The provider comes from the model. Leaving it to the database default is
    # how picking Claude produced a session that sent `claude-opus-5` to
    # api.deepseek.com -- this form never sent a provider at all.
    try:
        provider, effective_model = resolve_model_choice(model, custom_model)
    except ValueError as e:
        return templates.TemplateResponse(
            request=request, name="index_content.html",
            context=await _home_context(str(e)),
        )
    session = await db.create_session(
        name=name.strip() or directory.name,
        project_dir=str(directory.resolve()),
        provider=provider,
        model=effective_model,
        prompt_profile=prompt_profile,
        compact_profile=compact_profile,
        thinking_effort=thinking_effort or None,
    )
    _start_watching(session["id"], str(directory.resolve()))
    return RedirectResponse(f"/sessions/{session['id']}", status_code=303)


@app.post("/_quick_chat")
async def quick_chat(request: Request):
    """A throwaway session for a passing question.

    Everything is defaulted so there is nothing to fill in. The working
    directory is a fresh dated folder rather than the home directory or /tmp:
    the system prompt snapshots a listing of it, and pointing that at a busy
    directory buries the prompt in filenames that have nothing to do with the
    question being asked.
    """
    stamp = datetime.now().strftime("%-m-%-d-%Y")
    root = Path.home() / ".codeagent" / "scratch"
    label, scratch, n = stamp, root / stamp, 2
    while scratch.exists():
        label = f"{stamp} ({n})"
        scratch = root / f"{stamp}-{n}"
        n += 1
    try:
        scratch.mkdir(parents=True)
    except OSError:
        scratch = root

    session = await db.create_session(
        name=f"temp session {label}",
        project_dir=str(scratch),
        model=DEFAULT_MODEL,
        prompt_profile=PROTECTED_PROMPT,
        compact_profile=PROTECTED_PROMPT,
    )
    return RedirectResponse(f"/sessions/{session['id']}", status_code=303)


@app.get("/prompts")
async def prompts_page(request: Request, selected: str = ""):
    return templates.TemplateResponse(
        request=request, name="prompts.html", context=await _prompts_context(selected)
    )


@app.post("/_save_prompts")
async def save_prompts(request: Request):
    """Save one prompt's text."""
    form = await request.form()
    kind, _, name = str(form.get("name", "")).partition(":")
    name = name.strip()
    body = str(form.get("body", "")).strip()

    moved = 0
    if name and body and kind in (SYSTEM, COMPACTION):
        await db.save_prompt(name, body, kind, "")
        if kind == SYSTEM:
            moved = await _propagate(name)

    return templates.TemplateResponse(
        request=request,
        name="prompts.html",
        context=await _prompts_context(selected=f"{kind}:{name}", saved=True, moved=moved),
    )


@app.post("/_new_prompt")
async def new_prompt(request: Request):
    form = await request.form()
    kind = str(form.get("kind", SYSTEM))
    name = _slug(str(form.get("new_name", "")))
    if not name or kind not in (SYSTEM, COMPACTION):
        return RedirectResponse("/prompts", status_code=303)
    await db.save_prompt(name, "", kind)
    return RedirectResponse(f"/prompts?selected={kind}:{name}", status_code=303)


@app.post("/_save_subagent")
async def save_subagent(request: Request):
    form = await request.form()
    use_system = str(form.get("use_system_prompt", "")) == "1"
    prompt = str(form.get("prompt", "")).strip() if not use_system else ""
    model = str(form.get("model", "")).strip()

    await db.set_setting("subagent_prompt", prompt)
    await db.set_setting("subagent_model", model)

    return templates.TemplateResponse(
        request=request, name="prompts.html", context=await _prompts_context(saved=True),
    )


@app.post("/_delete_prompt")
async def delete_prompt(request: Request):
    form = await request.form()
    kind, _, name = str(form.get("name", "")).partition(":")
    name = name.strip()
    if name and name != PROTECTED_PROMPT and kind in (SYSTEM, COMPACTION):
        await db.delete_prompt(name, kind)
        # Sessions pointing at it fall back to the default. A system prompt's
        # frozen text is untouched, so nothing in flight changes.
        field = "prompt_profile" if kind == SYSTEM else "compact_profile"
        for row in await db.list_sessions():
            if row.get(field) == name:
                await db.update_session(row["id"], **{field: PROTECTED_PROMPT})
    return RedirectResponse("/prompts", status_code=303)


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower()).strip("-")[:40]


# ── Custom tools editor ─────────────────────────────────────────────────────


async def _tools_context(edit_tool: str = "", saved: bool = False, error: str = "") -> dict:
    """Everything custom_tools.html needs.

    The error branches used to build a context of four keys, so the template's
    loop over `secrets` raised UndefinedError and the intended inline message
    came back as a 500 instead.
    """
    schemas = tool_schemas()
    tools_list = await db.list_custom_tools()
    edit_tool_data = next((t for t in tools_list if t["name"] == edit_tool), None)
    return {
        "tools": tools_list,
        "saved": saved,
        "error": error,
        "edit_tool": edit_tool,
        "secrets": await db.list_secrets(),
        "tool_schemas_json": json.dumps(schemas, indent=2),
        "tool_count": len(schemas),
        # The page claimed "N tokens" and was given the number of tools. The
        # schemas go out on every single request, so their real size is the
        # number worth showing.
        "tool_schema_tokens": len(json.dumps(schemas, separators=(",", ":"))) // 4,
        "tool_warnings": _tool_param_warnings(edit_tool_data) if edit_tool_data else [],
        "default_test_args": _default_test_args(edit_tool_data),
    }


@app.get("/tools")
async def tools_page(request: Request, saved: bool = False):
    return templates.TemplateResponse(
        request=request, name="custom_tools.html",
        context=await _tools_context(request.query_params.get("edit", ""), saved),
    )


@app.post("/_save_custom_tool")
async def save_custom_tool(request: Request):
    from agent_server.tools.custom import reload_custom_tools
    from agent_server.tools.registry import BUILT_IN_NAMES

    form = await request.form()
    name = _slug(str(form.get("name", "")))
    description = str(form.get("description", "")).strip()
    parameters = str(form.get("parameters", "")).strip() or "{}"
    script = str(form.get("script", ""))
    enabled = str(form.get("enabled", "")).lower() in ("1", "true", "on")
    ask_permission = str(form.get("ask_permission", "")).lower() in ("1", "true", "on")

    async def refuse(message: str):
        return templates.TemplateResponse(
            request=request, name="custom_tools.html",
            context=await _tools_context(name, error=message),
        )

    if not name:
        return await refuse("Name is required")
    if name in BUILT_IN_NAMES:
        return await refuse(f"'{name}' is a built-in tool name")
    if len(description) > 1000:
        return await refuse("Description too long (max 1000 chars)")
    if len(parameters) > 8000:
        return await refuse("Parameters too long (max 8000 chars)")
    if len(script) > 32000:
        return await refuse("Script too long (max 32000 chars)")

    # Always validated, including when the field was left blank. Skipping the
    # check for an empty value stored "", which json.loads then choked on at
    # load time -- and because loading deregisters everything before parsing,
    # one such row disabled every custom tool and made the next startup fail
    # before the app could serve the page needed to fix it.
    try:
        params_json = json.loads(parameters)
    except json.JSONDecodeError as e:
        return await refuse(f"Invalid JSON in parameters: {e}")
    if not isinstance(params_json, dict):
        return await refuse("Parameters must be a JSON object")

    await db.save_custom_tool(name, description, parameters, script, enabled, ask_permission)
    problems = await reload_custom_tools()
    if problems:
        return await refuse("; ".join(problems))

    return RedirectResponse(f"/tools?edit={name}&saved=true", status_code=303)


def _tool_param_warnings(tool: dict) -> list[str]:
    """Warn about parameter/script mismatches."""
    import re

    warnings: list[str] = []
    try:
        params = json.loads(tool.get("parameters") or "{}")
    except Exception:
        return warnings
    props = params.get("properties") or {}
    script = tool.get("script") or ""
    for name in props:
        ref = f"$TOOL_ARG_{name.upper()}"
        if ref not in script and "$@" not in script and "$*" not in script:
            warnings.append(f"Parameter '{name}' defined but not referenced in script (add {ref})")
    used = set(re.findall(r'\$TOOL_ARG_(\w+)', script))
    for var in used - {k.upper() for k in props}:
        warnings.append(f"Script uses $TOOL_ARG_{var} but no parameter '{var.lower()}' defined")
    return warnings


def _default_test_args(tool: dict | None) -> str:
    """Build sample JSON from schema defaults for the test textarea."""
    if not tool:
        return "{}"
    try:
        params = json.loads(tool.get("parameters") or "{}")
    except Exception:
        return "{}"
    props = params.get("properties", {})
    if not props:
        return "{}"
    sample = {}
    for name, schema in props.items():
        if "default" in schema:
            sample[name] = schema["default"]
        elif schema.get("type") == "string":
            sample[name] = ""
        elif schema.get("type") == "integer" or schema.get("type") == "number":
            sample[name] = 0
        elif schema.get("type") == "boolean":
            sample[name] = False
        else:
            sample[name] = None
    return json.dumps(sample, indent=2)


@app.post("/_delete_custom_tool")
async def delete_custom_tool(request: Request):
    from agent_server.tools.custom import reload_custom_tools

    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        await db.delete_custom_tool(name)
        await reload_custom_tools()
    return RedirectResponse("/tools", status_code=303)


@app.post("/_new_custom_tool")
async def new_custom_tool(request: Request):
    from agent_server.tools.custom import reload_custom_tools

    form = await request.form()
    name = _slug(str(form.get("new_name", "")))
    if not name:
        return RedirectResponse("/tools", status_code=303)
    await db.save_custom_tool(name, "", "{}", "", True, True)
    await reload_custom_tools()
    return RedirectResponse(f"/tools?edit={name}", status_code=303)


@app.post("/_save_secret")
async def save_secret(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    value = str(form.get("value", "")).strip()
    if not name:
        return RedirectResponse("/tools", status_code=303)
    if value and "\u2022" in value:
        return RedirectResponse("/tools", status_code=303)
    if value:
        await db.save_secret(name, value)
    return RedirectResponse("/tools", status_code=303)


@app.post("/_delete_secret")
async def delete_secret(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        await db.delete_secret(name)
    return RedirectResponse("/tools", status_code=303)


@app.post("/_new_secret")
async def new_secret(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        await db.save_secret(name, "")
    return RedirectResponse("/tools", status_code=303)


@app.post("/_test_custom_tool")
async def test_custom_tool(request: Request):
    form = await request.form()
    name = str(form.get("name", ""))
    script = str(form.get("script", ""))
    test_args = str(form.get("test_args", "{}"))

    try:
        script_params = json.loads(str(form.get("parameters", "{}")))
    except json.JSONDecodeError:
        script_params = {}

    try:
        params = script_params
        args = json.loads(test_args) if test_args else {}
    except json.JSONDecodeError:
        return HTMLResponse("<div class='notice-error'>Invalid JSON in test arguments</div>")

    # Build default args from schema properties
    if not args and params.get("properties"):
        for key, prop in params["properties"].items():
            ptype = prop.get("type", "string")
            if ptype == "string":
                args[key] = prop.get("default", "")
            elif ptype == "number" or ptype == "integer":
                args[key] = prop.get("default", 0)
            elif ptype == "array":
                args[key] = prop.get("default", [])
            elif ptype == "object":
                args[key] = prop.get("default", {})
            elif ptype == "boolean":
                args[key] = prop.get("default", False)

    if not name or not script:
        return HTMLResponse("<div class='notice-error'>Missing name or script</div>")

    env_vars = {f"TOOL_ARG_{k.upper()}": json.dumps(v) for k, v in args.items()}
    secrets = await db.load_secrets_dict()
    env_vars.update(secrets)

    import asyncio as _asyncio
    import os as _os
    try:
        proc = await _asyncio.create_subprocess_shell(
            script,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
            env={**_os.environ, "TERM": "dumb", "NO_COLOR": "1", **env_vars},
        )
        stdout, stderr = await _asyncio.wait_for(proc.communicate(), timeout=30)
        out = stdout.decode("utf-8", errors="replace")[:5000]
        err = stderr.decode("utf-8", errors="replace")[:2000]
        if proc.returncode != 0:
            return _test_output(err or out, f"Exit code {proc.returncode}")
        return _test_output(out or "(no output)")
    except TimeoutError:
        await _kill(proc)
        return _test_output("", "Timed out after 30s")
    except Exception as e:
        # The subprocess outlives a non-timeout failure otherwise.
        await _kill(proc)
        return _test_output("", f"{type(e).__name__}: {e}")


async def _kill(proc):
    try:
        proc.kill()
        await proc.wait()
    except (ProcessLookupError, AttributeError):
        pass


def _test_output(body: str, error: str = "") -> HTMLResponse:
    """Render a tool test result.

    The output is whatever the script printed, so it is escaped. It used to be
    interpolated into an f-string and written to innerHTML, which made any tool
    that echoes markup -- or is handed a crafted argument -- script running in
    this page, with the secrets store one fetch away.
    """
    safe = html.escape(body)
    if error:
        return HTMLResponse(
            f'<div class="notice-error"><strong>{html.escape(error)}</strong>'
            + (f"<pre>{safe}</pre>" if body else "")
            + "</div>"
        )
    return HTMLResponse(f'<pre class="test-output">{safe}</pre>')


# ── Custom endpoints ────────────────────────────────────────────────────────


@app.post("/_save_custom_endpoint")
async def save_custom_endpoint(request: Request):
    form = await request.form()
    name = _slug(str(form.get("name", "")))
    base_url = str(form.get("base_url", "")).strip()
    api_key = str(form.get("api_key", "")).strip()
    if not name or not base_url:
        return templates.TemplateResponse(
            request=request, name="index_content.html",
            context=await _home_context(error="Name and base URL are required"),
        )
    # Skip masked passwords (unchanged)
    if api_key and "\u2022" in api_key:
        existing = await db.get_custom_endpoint(name)
        api_key = existing["api_key"] if existing else ""
    await db.save_custom_endpoint(name, base_url, api_key)
    _reload_custom_endpoint_providers()
    return templates.TemplateResponse(
        request=request, name="index_content.html", context=await _home_context(),
    )


@app.post("/_delete_custom_endpoint")
async def delete_custom_endpoint(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        await db.delete_custom_endpoint(name)
        _reload_custom_endpoint_providers()
    return templates.TemplateResponse(
        request=request, name="index_content.html", context=await _home_context(),
    )


@app.post("/_new_custom_endpoint")
async def new_custom_endpoint(request: Request):
    form = await request.form()
    name = _slug(str(form.get("name", "")))
    if not name:
        return RedirectResponse("/", status_code=303)
    if not await db.get_custom_endpoint(name):
        await db.save_custom_endpoint(name, "", "")
    _reload_custom_endpoint_providers()
    return RedirectResponse("/", status_code=303)


def _reload_custom_endpoint_providers():
    """Add/remove custom endpoint providers without a server restart."""
    import asyncio

    from agent_server.providers import _providers
    from agent_server.providers.custom_openai import CustomOpenAIProvider

    # Remove old custom endpoint providers
    for key in list(_providers):
        if key.startswith("custom:"):
            del _providers[key]

    # Load from DB
    async def _load():
        for row in await db.list_custom_endpoints():
            if row["base_url"]:
                p = CustomOpenAIProvider(row["name"], row["base_url"])
                _providers[f"custom:{row['name']}"] = p

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_load())
    else:
        loop.create_task(_load())


async def _propagate(name: str) -> int:
    """Queue the edited prompt onto the sessions that share it.

    Every session using this prompt and not carrying its own gets the new text
    at its next compaction. Swapping it in now would invalidate the cached
    prefix and re-bill the whole conversation; at compaction the prefix is being
    rewritten regardless, so the switch is close to free.
    """
    moved = 0
    for row in await db.list_sessions():
        if row.get("prompt_custom"):
            continue  # has its own prompt; not ours to overwrite
        if (row.get("prompt_profile") or PROTECTED_PROMPT) != name:
            continue
        fresh = await build_system_prompt(name, row["project_dir"], row["id"])
        if fresh == row.get("system_prompt"):
            continue
        if row.get("system_prompt"):
            await db.update_session(row["id"], pending_system_prompt=fresh)
        else:
            # Never ran, so nothing is cached and there is nothing to lose.
            await db.update_session(row["id"], system_prompt=fresh)
        moved += 1
    return moved


async def _prompts_context(
    selected: str = "", saved: bool = False, moved: int = 0
) -> dict:
    """Both kinds share one editor; a key is "kind:name" so they cannot collide."""
    prompts = await db.list_prompts()
    bodies = {f"{p['kind']}:{p['name']}": p["body"] for p in prompts}
    groups = {
        "System prompts": [f"{SYSTEM}:{p['name']}" for p in prompts if p["kind"] == SYSTEM],
        "Summarising prompts": [
            f"{COMPACTION}:{p['name']}" for p in prompts if p["kind"] == COMPACTION
        ],
    }
    if selected not in bodies:
        selected = f"{SYSTEM}:{PROTECTED_PROMPT}"

    return {
        "groups": groups,
        "bodies": bodies,
        "selected": selected,
        "body": bodies.get(selected, ""),
        "protected": PROTECTED_PROMPT,
        # Subagent defaults
        "sa_prompt": await db.get_setting("subagent_prompt", ""),
        "sa_model": await db.get_setting("subagent_model", ""),
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "pause": t.pause or "",
                "tokens": len(json.dumps(t.schema())) // 4,
            }
            for t in TOOLS.values()
        ],
        "saved": saved,
        "moved": moved,
        "sound_enabled": await _sound_enabled(),
        "uploaded_sounds": _list_uploaded_sounds(),
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
