"""User scripts: named shell scripts the user runs with a click.

Deliberately not tools. A script is never sent to the model and has no schema,
because "start the Ollama box" is a convenience for the person sitting here and
has no business occupying schema tokens on every request.
"""

import asyncio
import html
import logging
import os
import signal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from agent_server import database as db
from agent_server.config import BASE_DIR
from agent_server.routes.context import _slug
from agent_server.tools.bash import _collect, _kill

RUN_TIMEOUT_SEC = 120
MAX_SCRIPT_CHARS = 32000
MAX_OUTPUT_CHARS = 5000

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/_save_script")
async def save_script(request: Request):
    form = await request.form()
    name = _slug(str(form.get("name", "")))
    body = str(form.get("body", ""))

    if not name:
        return RedirectResponse("/", status_code=303)
    if len(body) > MAX_SCRIPT_CHARS:
        return RedirectResponse(f"/?script={name}&error=toolong", status_code=303)

    await db.save_script(name, body)
    return RedirectResponse(f"/?script={name}&saved=true", status_code=303)


@router.post("/_delete_script")
async def delete_script(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        await db.delete_script(name)
    return RedirectResponse("/", status_code=303)


@router.post("/_new_script")
async def new_script(request: Request):
    form = await request.form()
    name = _slug(str(form.get("new_name", "")))
    if not name:
        return RedirectResponse("/", status_code=303)
    await db.save_script(name, "")
    return RedirectResponse(f"/?script={name}", status_code=303)


@router.post("/_shutdown")
async def shutdown():
    """Stop the server from the UI.

    Signals the process rather than calling sys.exit: uvicorn installs a SIGTERM
    handler that runs the lifespan shutdown, which is what closes the database
    and the browser. Killing the worker directly would skip it.
    """
    log.info("shutdown requested from the UI")

    async def _later():
        # Let the response reach the browser first, or the page reports a
        # network error for a shutdown that worked.
        await asyncio.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.get_running_loop().create_task(_later())
    return HTMLResponse('<div class="script-note">Stopped. This page is now dead.</div>')


@router.post("/_run_script")
async def run_script(request: Request):
    form = await request.form()
    name = str(form.get("name", ""))

    # The saved script is run, not a body posted with the request. Trusting the
    # form would make this endpoint "run arbitrary shell" rather than "run the
    # thing the user saved and just confirmed", and the confirmation dialog
    # names a script -- so that had better be what executes.
    row = await db.get_script(name)
    if row is None:
        return HTMLResponse(
            '<div class="script-exit fail">No saved script by that name. '
            "Save it first.</div>"
        )
    script = row["body"]
    if not script.strip():
        return HTMLResponse('<div class="script-exit fail">This script is empty.</div>')

    proc = None
    detached = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", script,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_DIR),
            # Inherit the environment so .env values -- the vision host, keys --
            # are visible, which is most of the point for these scripts. The
            # saved secrets (set on this page or the Tools page) are added on
            # top, so a script can read $VAR_NAME without touching .env.
            env={**os.environ, **await db.load_secrets_dict(),
                 "TERM": "dumb", "NO_COLOR": "1", "PAGER": "cat"},
            # Own process group, so a timeout kills the whole pipeline.
            start_new_session=True,
        )
        # _collect, not communicate(): communicate waits for the pipes to reach
        # EOF rather than for the shell to exit, so `ollama serve &` blocks for
        # the full timeout and then gets killed along with the server it just
        # started. Starting a daemon is the main thing these scripts are for.
        stdout, stderr, detached = await asyncio.wait_for(
            _collect(proc), timeout=RUN_TIMEOUT_SEC
        )
    except TimeoutError:
        _kill(proc)
        return HTMLResponse(
            f'<div class="script-exit fail">Timed out after {RUN_TIMEOUT_SEC}s '
            "and was killed.</div>"
        )
    except Exception as e:
        _kill(proc)
        return HTMLResponse(
            f'<div class="script-exit fail">{html.escape(str(e))}</div>'
        )

    return HTMLResponse(_render_run(proc.returncode, stdout, stderr, detached))


def _render_run(code: int, stdout: bytes, stderr: bytes, detached: bool) -> str:
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")

    state = "ok" if code == 0 else "fail"
    label = "Finished" if code == 0 else "Failed"
    parts = [f'<div class="script-exit {state}">{label} \u00b7 exit {code}</div>']

    if detached:
        parts.append(
            '<div class="script-note">The shell exited and left something running. '
            "It was not killed, and anything it prints from here on is not captured.</div>"
        )

    for title, text in (("stdout", out), ("stderr", err)):
        if not text.strip():
            continue
        note = ""
        if len(text) > MAX_OUTPUT_CHARS:
            note = f" (first {MAX_OUTPUT_CHARS} characters)"
            text = text[:MAX_OUTPUT_CHARS]
        css = "script-output" + (" stderr-output" if title == "stderr" else "")
        parts.append(
            f'<details class="script-output-details" open><summary>{title}{note}</summary>'
            f'<pre class="{css}">{html.escape(text)}</pre></details>'
        )

    if not out.strip() and not err.strip():
        parts.append('<div class="script-note">No output.</div>')
    return "".join(parts)
