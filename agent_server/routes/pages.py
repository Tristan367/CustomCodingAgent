"""The home page, the session view, and the HTMX partials that refresh it."""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from agent_server import database as db
from agent_server.config import DEFAULT_MODEL, resolve_model_choice
from agent_server.routes.context import (
    _home_context,
    _session_context,
    _start_watching,
    _track_tab,
)
from agent_server.system_prompt import PROTECTED_PROMPT
from agent_server.templating import templates

router = APIRouter()


# ── Pages ───────────────────────────────────────────────────────────────────

@router.get("/")
async def index(request: Request, clone: str = ""):
    err = request.query_params.get("error", "")
    error_text = ""
    if err == "toolong":
        error_text = "That script is too long to save."
    return templates.TemplateResponse(
        request=request, name="index.html",
        context=await _home_context(
            error=error_text,
            clone_id=clone,
            edit_script=request.query_params.get("script", ""),
            saved=request.query_params.get("saved") == "true",
        )
    )


@router.get("/sessions/{session_id}")
async def session_page(request: Request, session_id: str):
    session = await db.get_session(session_id)
    if session is None:
        return RedirectResponse("/", status_code=303)
    await _track_tab(session_id)
    return templates.TemplateResponse(
        request=request, name="session.html", context=await _session_context(session)
    )


@router.get("/_session/{session_id}")
async def session_partial(request: Request, session_id: str):
    session = await db.get_session(session_id)
    if session is None:
        return HTMLResponse("Session not found", status_code=404)
    await _track_tab(session_id)
    return templates.TemplateResponse(
        request=request, name="session_content.html", context=await _session_context(session)
    )


@router.get("/_messages/{session_id}")
async def messages_partial(request: Request, session_id: str):
    session = await db.get_session(session_id)
    if session is None:
        return HTMLResponse("Session not found", status_code=404)
    return templates.TemplateResponse(
        request=request, name="chat_messages.html", context=await _session_context(session)
    )


@router.get("/_session_meta/{session_id}")
async def session_meta_partial(request: Request, session_id: str):
    session = await db.get_session(session_id)
    if session is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(
        request=request, name="components/session_meta.html",
        context=await _session_context(session),
    )


@router.post("/_create_session")
async def create_session_form(
    request: Request,
    name: str = Form(...),
    project_dir: str = Form(...),
    model: str = Form(DEFAULT_MODEL),
    prompt_profile: str = Form("default"),
    thinking_effort: str = Form(""),
    custom_model: str = Form(""),
    subagent_model: str = Form(""),
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
        thinking_effort=thinking_effort or None,
        subagent_model=subagent_model or None,
    )
    _start_watching(session["id"], str(directory.resolve()))
    return RedirectResponse(f"/sessions/{session['id']}", status_code=303)


@router.post("/_quick_chat")
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
    )
    return RedirectResponse(f"/sessions/{session['id']}", status_code=303)
