"""System and summarising prompt editor, plus subagent defaults."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from agent_server import database as db
from agent_server.config import REASONING_EFFORTS
from agent_server.routes.context import (
    _offerable_models,
    _page_or_body,
    _slug,
)
from agent_server.system_prompt import (
    COMPACT_PROMPT_DEFAULT,
    COMPACTION,
    DEFAULT_MASTER_SPAWN_LIMIT,
    DEFAULT_PROMPT,
    DEFAULT_SESSION_SUBAGENT_CAP,
    DEFAULT_SUBAGENT_PROMPT,
    PROTECTED_PROMPT,
    READONLY_PROMPTS,
    STARTER_DISABLED_TOOLS,
    SYSTEM,
    _default_subagent_off,
    prompt_drift,
    propagate_prompt,
    starter_limit,
)
from agent_server.templating import templates
from agent_server.tools.registry import TOOLS

router = APIRouter()


def _parse_tiers(p: dict) -> list[dict]:
    """Parse the subagent_tiers JSON column into a list of {body, off} dicts."""
    raw = (p.get("subagent_tiers") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    result = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        off_raw = entry.get("disabled_tools", "")
        off = [n.strip() for n in (off_raw or "").split(",") if n.strip()]
        result.append({
            "body": str(entry.get("body", "")).strip(),
            "off": off,
            "parallel_cap": max(0, int(entry.get("parallel_cap", 3) or 3)),
            "model": str(entry.get("model", "")).strip(),
            "effort": str(entry.get("effort", "")).strip(),
        })
    return result


def _effective_disabled(name: str, raw: str | None) -> set[str]:
    """The tools this profile really switches off, column or defaults."""
    if raw is None:
        from agent_server.tools.registry import _custom_tool_names

        return set(_custom_tool_names) | STARTER_DISABLED_TOOLS.get(name, set())
    return {n.strip() for n in raw.split(",") if n.strip()}


def _cap_val(raw, default=0):
    """Parse an integer column, falling back to *default*.

    Clamped at -1, not 0: -1 is how unlimited is spelled, and clamping it away
    turned "no limit" into "none" the moment the form was saved.
    """
    if raw is None:
        return default
    try:
        return max(-1, int(raw))
    except (TypeError, ValueError):
        return default


async def _prompts_context(
    selected: str = "", saved: bool = False, moved: int = 0
) -> dict:
    """Profiles bundle system prompt, compaction prompt, and subagent config."""
    prompts = await db.list_prompts()
    # System prompt bodies
    sys = [p for p in prompts if p["kind"] == SYSTEM]
    bodies = {f"{SYSTEM}:{p['name']}": p["body"] for p in sys}
    profile_names = sorted([p["name"] for p in sys], key=lambda n: (n != "default", n))
    # Compaction prompt bodies, keyed the same way (they share the name)
    compact_map = {p["name"]: p["body"] for p in prompts if p["kind"] == COMPACTION}
    # A profile with no compaction row of its own falls back to `default`'s at
    # run time, so showing an empty box was a lie about what would be used --
    # `minimal` and `visual` have looked like they had no summarising prompt
    # since they were created.
    inherited = compact_map.get(PROTECTED_PROMPT, COMPACT_PROMPT_DEFAULT.strip())
    compact_bodies = {
        f"{SYSTEM}:{n}": compact_map.get(n) or inherited for n in profile_names
    }

    if selected not in bodies:
        selected = f"{SYSTEM}:{PROTECTED_PROMPT}"

    selected_name = selected.split(":")[-1] if ":" in selected else ""
    return {
        "profile_names": profile_names,
        "bodies": bodies,
        "compact_bodies": compact_bodies,
        "compact_body": compact_bodies.get(selected, ""),
        "selected": selected,
        "drift": {k: prompt_drift(v) for k, v in bodies.items() if prompt_drift(v)},
        "body": bodies.get(selected, ""),
        "protected": PROTECTED_PROMPT,
        "readonly": sorted(READONLY_PROMPTS),
        "selected_name": selected_name,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "pause": t.pause or "",
                "tokens": len(json.dumps(t.schema())) // 4,
            }
            for t in sorted(TOOLS.values(), key=lambda t: t.name)
        ],
        # What is actually in force, not the raw column. A profile that has
        # never been configured stores NULL and switches custom tools off at
        # read time -- reading the column showed every box ticked while the
        # agent was being offered none of them.
        "disabled_tools": {
            f"{SYSTEM}:{p['name']}": sorted(
                _effective_disabled(p["name"], p["disabled_tools"])
            )
            for p in sys
        },
        "subagent_body": {
            f"{SYSTEM}:{p['name']}": (p.get("subagent_body") or "").strip()
            for p in sys
        },
        "sa_parallel_cap": {
            f"{SYSTEM}:{p['name']}": _cap_val(
                p.get("subagent_parallel_cap"),
                starter_limit(p["name"], "subagent_parallel_cap", 3),
            )
            for p in sys
        },
        "sa_default_model": {
            f"{SYSTEM}:{p['name']}": (p.get("subagent_model") or "").strip()
            for p in sys
        },
        "sa_tier1_model": {
            f"{SYSTEM}:{p['name']}": (p.get("sa_tier_model") or "").strip()
            for p in sys
        },
        "sa_tier1_effort": {
            f"{SYSTEM}:{p['name']}": (p.get("sa_tier_effort") or "").strip()
            for p in sys
        },
        "master_spawn": {
            f"{SYSTEM}:{p['name']}": _cap_val(
                p.get("master_spawn_limit"),
                starter_limit(p["name"], "master_spawn_limit", DEFAULT_MASTER_SPAWN_LIMIT),
            )
            for p in sys
        },
        "max_concurrent_subs": {
            f"{SYSTEM}:{p['name']}": _cap_val(
                p.get("max_concurrent_subagents"),
                starter_limit(p["name"], "max_concurrent_subagents", DEFAULT_SESSION_SUBAGENT_CAP),
            )
            for p in sys
        },
        "sa_disabled": {
            f"{SYSTEM}:{p['name']}": (
                sorted(_default_subagent_off())
                if p.get("subagent_disabled_tools") is None
                else [n.strip() for n in (p["subagent_disabled_tools"] or "").split(",") if n.strip()]
            )
            for p in sys
        },
        # Higher subagent tiers stored as JSON.
        "sa_tiers": {
            f"{SYSTEM}:{p['name']}": _parse_tiers(p)
            for p in sys
        },
        "default_sa_body": DEFAULT_SUBAGENT_PROMPT.strip(),
        "profile_models": _offerable_models(),
        "efforts": REASONING_EFFORTS,
        "saved": saved,
        "moved": moved,
    }


@router.get("/prompts")
async def prompts_page(request: Request, selected: str = ""):
    return templates.TemplateResponse(
        request=request, name=_page_or_body(request, "prompts.html", "prompts_body.html"), context=await _prompts_context(selected)
    )


@router.post("/_save_prompts")
async def save_prompts(request: Request):
    """Save a profile: system prompt, compaction prompt, tools, and subagent tiers."""
    form = await request.form()
    kind, _, name = str(form.get("name", "")).partition(":")
    name = name.strip()
    body = str(form.get("body", "")).strip()
    compact_body = str(form.get("compact_body", "")).strip()

    # Unchecked boxes are simply absent from the form.
    enabled = {str(v) for v in form.getlist("tool")}
    off = ",".join(sorted(n for n in TOOLS if n not in enabled))

    master_spawn_raw = str(form.get("master_spawn", str(DEFAULT_MASTER_SPAWN_LIMIT)))
    try:
        master_spawn = max(-1, int(master_spawn_raw))
    except (TypeError, ValueError):
        master_spawn = DEFAULT_MASTER_SPAWN_LIMIT

    sa_body = str(form.get("subagent_body", "")).strip()
    sa_visible = str(form.get("sa_visible", "")) == "1"

    # Tier 0 tools — the checkbox names are "sa_tool".
    sa_enabled = {str(v) for v in form.getlist("sa_tool")}
    sa_off = ",".join(sorted(n for n in TOOLS if n not in sa_enabled)) if sa_visible else ""
    sa_cap_raw = str(form.get("sa_cap", "3"))
    try:
        sa_cap = max(0, int(sa_cap_raw))
    except (TypeError, ValueError):
        sa_cap = 3

    # Higher tiers — tools are "sa_tool_N" and bodies are "subagent_body_N".
    tiers_json = _collect_tiers(form)

    # Global session-wide cap.
    max_conc_raw = str(form.get("max_concurrent", str(DEFAULT_SESSION_SUBAGENT_CAP)))
    try:
        max_conc = max(-1, int(max_conc_raw))
    except (TypeError, ValueError):
        max_conc = DEFAULT_SESSION_SUBAGENT_CAP

    sa_model = str(form.get("sa_model", "")).strip()
    sa_tier1_model = str(form.get("sa_tier_model", "")).strip()
    sa_tier1_effort = str(form.get("sa_tier_effort", "")).strip()

    moved = 0
    if name and body and kind == SYSTEM:
        if name in READONLY_PROMPTS:
            return templates.TemplateResponse(
                request=request,
                name=_page_or_body(request, "prompts.html", "prompts_body.html"),
                context=await _prompts_context(selected=f"{kind}:{name}", saved=True, moved=moved),
            )
        await db.save_prompt(
            name, body, SYSTEM,
            disabled_tools=off,
            subagent_body=sa_body if sa_visible else None,
            subagent_disabled_tools=sa_off if sa_visible else None,
            subagent_parallel_cap=sa_cap if sa_visible else None,
        )
        await db._execute(
            "UPDATE prompts SET master_spawn_limit = ?, max_concurrent_subagents = ?, subagent_model = ?, sa_tier_model = ?, sa_tier_effort = ? WHERE kind = ? AND name = ?",
            (master_spawn, max_conc, sa_model or None, sa_tier1_model or None, sa_tier1_effort or None, SYSTEM, name),
        )
        # Store higher tiers as JSON.
        if tiers_json:
            await db._execute(
                "UPDATE prompts SET subagent_tiers = ? WHERE kind = ? AND name = ?",
                (tiers_json, SYSTEM, name),
            )
        else:
            await db._execute(
                "UPDATE prompts SET subagent_tiers = NULL WHERE kind = ? AND name = ?",
                (SYSTEM, name),
            )
        if compact_body:
            await db.save_prompt(name, compact_body, COMPACTION)
        moved = await propagate_prompt(name)

    return templates.TemplateResponse(
        request=request,
        name=_page_or_body(request, "prompts.html", "prompts_body.html"),
        context=await _prompts_context(selected=f"{kind}:{name}", saved=True, moved=moved),
    )


def _collect_tiers(form) -> str:
    """Collect higher subagent tiers from form fields into a JSON string."""
    tier = 2  # tier 1 uses dedicated columns; tiers 2+ use indexed form fields
    tiers = []
    while True:
        body = str(form.get(f"subagent_body_{tier}", "")).strip()
        enabled = {str(v) for v in form.getlist(f"sa_tool_{tier}")}
        if not body and not enabled:
            break
        cap_raw = str(form.get(f"sa_cap_{tier}", "3"))
        try:
            cap = max(0, int(cap_raw))
        except (TypeError, ValueError):
            cap = 3
        model = str(form.get(f"sa_model_{tier}", "")).strip()
        effort = str(form.get(f"sa_effort_{tier}", "")).strip()
        off = ",".join(sorted(n for n in TOOLS if n not in enabled))
        tiers.append({"body": body, "disabled_tools": off, "parallel_cap": cap, "model": model, "effort": effort})
        tier += 1
    if not tiers:
        return ""
    return json.dumps(tiers)


@router.post("/_new_prompt")
async def new_prompt(request: Request):
    form = await request.form()
    name = _slug(str(form.get("new_name", "")))
    if not name:
        return RedirectResponse("/prompts", status_code=303)
    # Copy the profile the user is looking at. Always copying `default` meant
    # that building a variant of anything else -- the usual reason to make one --
    # started by discarding the thing you were varying.
    source = _slug(str(form.get("copy_from", ""))) or "default"
    default = await db.get_prompt(source, SYSTEM) or await db.get_prompt("default", SYSTEM)
    compact_default = await db.get_prompt(source, COMPACTION) or await db.get_prompt(
        "default", COMPACTION
    )
    sys_body = (default["body"] if default else DEFAULT_PROMPT.strip())
    cmp_body = (compact_default["body"] if compact_default else "")
    await db.save_prompt(
        name, sys_body, SYSTEM,
        disabled_tools=(default.get("disabled_tools") or "") if default else "",
        subagent_body=default.get("subagent_body") if default else None,
        subagent_disabled_tools=default.get("subagent_disabled_tools") if default else None,
        subagent_parallel_cap=default.get("subagent_parallel_cap") if default else None,
    )
    await db.save_prompt(name, cmp_body, COMPACTION)
    # Copy the remaining profile-level columns that save_prompt doesn't handle.
    await db._execute(
        "UPDATE prompts SET master_spawn_limit = ?, max_concurrent_subagents = ?,"
        " subagent_model = ?, sa_tier_model = ?, sa_tier_effort = ?, subagent_tiers = ?"
        " WHERE kind = ? AND name = ?",
        (default.get("master_spawn_limit") if default else None,
         default.get("max_concurrent_subagents") if default else None,
         default.get("subagent_model") if default else None,
         default.get("sa_tier_model") if default else None,
         default.get("sa_tier_effort") if default else None,
         default.get("subagent_tiers") if default else None,
         SYSTEM, name),
    )
    return RedirectResponse(f"/prompts?selected={SYSTEM}:{name}", status_code=303)


@router.post("/_save_subagent")
async def save_subagent(request: Request):
    form = await request.form()
    use_system = str(form.get("use_system_prompt", "")) == "1"
    prompt = str(form.get("prompt", "")).strip() if not use_system else ""

    await db.set_setting("subagent_prompt", prompt)

    return templates.TemplateResponse(
        request=request, name=_page_or_body(request, "prompts.html", "prompts_body.html"), context=await _prompts_context(saved=True),
    )


@router.post("/_delete_prompt")
async def delete_prompt(request: Request):
    form = await request.form()
    kind, _, name = str(form.get("name", "")).partition(":")
    name = name.strip()
    if name and name != PROTECTED_PROMPT and kind == SYSTEM:
        await db.delete_prompt(name, SYSTEM)
        await db.delete_prompt(name, COMPACTION)
        for row in await db.list_sessions():
            if row.get("prompt_profile") == name:
                await db.update_session(row["id"], prompt_profile=PROTECTED_PROMPT)
    return RedirectResponse("/prompts", status_code=303)

# ── Sharing a profile ───────────────────────────────────────────────────────

@router.get("/_prompts/{name}/export")
async def export_profile(name: str):
    """Download a profile and the custom tools it uses as one JSON file."""
    from fastapi.responses import Response

    from agent_server import bundles

    bundle = await bundles.build_bundle(name)
    if bundle is None:
        return JSONResponse({"ok": False, "error": "No profile by that name."}, status_code=404)
    return Response(
        content=bundles.dump_bundle(bundle),
        media_type="application/json",
        headers={
            "Content-Disposition":
                f'attachment; filename="{bundles.bundle_filename(name)}"'
        },
    )


@router.post("/_prompts/inspect")
async def inspect_bundle(request: Request):
    """What a bundle would do, without doing any of it.

    Always the step before importing. A bundle carries shell scripts that will
    run on this machine, so the person has to see them first -- this is what
    puts them on screen.
    """
    from agent_server import bundles

    form = await request.form()
    upload = form.get("bundle")
    raw = await upload.read() if hasattr(upload, "read") else str(form.get("json") or "")
    try:
        parsed = bundles.read_bundle(raw)
    except bundles.BundleError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    # Handed back as a *complete* bundle, envelope and all, so the import that
    # follows validates exactly the same shape it would have validated straight
    # from the file. Returning the parsed halves alone meant the import step
    # re-read something with no `format` and refused it -- so the review worked
    # and the button after it did not.
    return JSONResponse({
        "ok": True,
        "summary": await bundles.describe_bundle(parsed),
        "bundle": {
            "format": bundles.FORMAT,
            "version": bundles.VERSION,
            "profile": parsed["profile"],
            "tools": parsed["tools"],
        },
    })


@router.post("/_prompts/import")
async def import_bundle(request: Request):
    """Apply a bundle the user has just been shown and accepted."""
    from agent_server import bundles

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Malformed request."}, status_code=400)
    try:
        parsed = bundles.read_bundle(body.get("bundle") or {})
        result = await bundles.apply_bundle(parsed, str(body.get("rename") or ""))
    except bundles.BundleError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **result})
