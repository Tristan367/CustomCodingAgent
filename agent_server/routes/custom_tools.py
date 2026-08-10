"""Custom tool editor: CRUD, secrets, and test runner."""

import html
import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from agent_server import database as db
from agent_server.routes.context import _slug
from agent_server.templating import templates
from agent_server.tools.registry import tool_schemas

router = APIRouter()


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


def _tool_param_warnings(tool: dict) -> list[str]:
    """Warn about parameter/script mismatches."""

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


@router.get("/tools")
async def tools_page(request: Request, saved: bool = False):
    return templates.TemplateResponse(
        request=request, name="custom_tools.html",
        context=await _tools_context(request.query_params.get("edit", ""), saved),
    )


@router.post("/_save_custom_tool")
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


@router.post("/_delete_custom_tool")
async def delete_custom_tool(request: Request):
    from agent_server.tools.custom import reload_custom_tools

    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        await db.delete_custom_tool(name)
        await reload_custom_tools()
    return RedirectResponse("/tools", status_code=303)


@router.post("/_new_custom_tool")
async def new_custom_tool(request: Request):
    from agent_server.tools.custom import reload_custom_tools

    form = await request.form()
    name = _slug(str(form.get("new_name", "")))
    if not name:
        return RedirectResponse("/tools", status_code=303)
    await db.save_custom_tool(name, "", "{}", "", True, True)
    await reload_custom_tools()
    return RedirectResponse(f"/tools?edit={name}", status_code=303)


@router.post("/_save_secret")
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


@router.post("/_delete_secret")
async def delete_secret(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        await db.delete_secret(name)
    return RedirectResponse("/tools", status_code=303)


@router.post("/_new_secret")
async def new_secret(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        await db.save_secret(name, "")
    return RedirectResponse("/tools", status_code=303)


@router.post("/_test_custom_tool")
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
