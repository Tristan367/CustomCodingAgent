"""The home page, the session view, and the HTMX partials that refresh it."""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from agent_server import database as db
from agent_server.config import DEFAULT_MODEL, resolve_model_choice
from agent_server.routes.context import (
    TRANSCRIPT_WINDOW,
    _apply_transcript_hiding,
    _expand_tools,
    _hide_thinking,
    _hide_tool_calls,
    _home_context,
    _session_context,
    _start_watching,
    _tool_inputs,
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


@router.get("/_messages/{session_id}/earlier")
async def earlier_messages(request: Request, session_id: str, before: int, limit: int = 0):
    """The batch of messages just older than `before`, for the transcript.

    Rendered with the same template as the rest so an older message is drawn
    exactly like a recent one. `compactions` is deliberately empty: the summary
    cards belong at the very top of the transcript and are already there, and
    repeating them above every batch would read as new ones arriving.
    """
    session = await db.get_session(session_id)
    if session is None:
        return HTMLResponse("Session not found", status_code=404)
    count = max(1, min(limit or TRANSCRIPT_WINDOW, 500))
    rows = await db.get_messages_before(session_id, before, count)
    # Hiding is annotated as if these were the whole transcript, which is right
    # for a batch drawn from the middle: nothing here is the current turn, so
    # nothing here is the block that survives.
    _apply_transcript_hiding(
        rows, await _hide_tool_calls(), await _hide_thinking(), keep_last=False
    )
    remaining = await db.count_messages_before(session_id, rows[0]["id"]) if rows else 0
    return templates.TemplateResponse(
        request=request,
        name="chat_messages.html",
        context={
            "session": session,
            "messages": rows,
            "compactions": [],
            "tool_inputs": _tool_inputs(rows),
            "expand_tools": await _expand_tools(),
            "older_count": remaining,
            "oldest_id": rows[0]["id"] if rows else 0,
            "pending": None,
        },
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
