"""System and summarising prompt editor, plus subagent defaults."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from agent_server import database as db
from agent_server.routes.context import (
    _offerable_models,
    _page_or_body,
    _slug,
)
from agent_server.system_prompt import (
    COMPACTION,
    DEFAULT_PROMPT,
    DEFAULT_SUBAGENT_PROMPT,
    PROTECTED_PROMPT,
    READONLY_PROMPTS,
    SYSTEM,
    _default_subagent_off,
    build_system_prompt,
    prompt_drift,
)
from agent_server.templating import templates
from agent_server.tools.registry import TOOLS

router = APIRouter()


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


def _parse_tiers(p: dict) -> list[dict]:
    """Parse the subagent_tiers JSON column into a list of {body, off} dicts."""
    import json as _json
    raw = (p.get("subagent_tiers") or "").strip()
    if not raw:
        return []
    try:
        data = _json.loads(raw)
    except (_json.JSONDecodeError, TypeError):
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
            "parallel_cap": int(entry.get("parallel_cap", 3) or 3),
            "model": str(entry.get("model", "")).strip(),
        })
    return result


def _cap_val(raw, default=0):
    """Parse an integer column, falling back to *default*."""
    if raw is None:
        return default
    try:
        return int(raw)
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
    compact_bodies = {f"{SYSTEM}:{n}": compact_map.get(n, "") for n in profile_names}

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
        "disabled_tools": {
            f"{SYSTEM}:{p['name']}": [
                n.strip() for n in (p["disabled_tools"] or "").split(",") if n.strip()
            ]
            for p in sys
        },
        "subagent_body": {
            f"{SYSTEM}:{p['name']}": (p.get("subagent_body") or "").strip()
            for p in sys
        },
        "sa_parallel_cap": {
            f"{SYSTEM}:{p['name']}": _cap_val(p.get("subagent_parallel_cap"), 3)
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
        "master_spawn": {
            f"{SYSTEM}:{p['name']}": _cap_val(p.get("master_spawn_limit"), 0)
            for p in sys
        },
        "max_concurrent_subs": {
            f"{SYSTEM}:{p['name']}": _cap_val(p.get("max_concurrent_subagents"), 100)
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

    master_spawn_raw = str(form.get("master_spawn", "0"))
    try:
        master_spawn = int(master_spawn_raw)
    except (TypeError, ValueError):
        master_spawn = 0

    sa_body = str(form.get("subagent_body", "")).strip()
    sa_visible = str(form.get("sa_visible", "")) == "1"

    # Tier 0 tools — the checkbox names are "sa_tool".
    sa_enabled = {str(v) for v in form.getlist("sa_tool")}
    sa_off = ",".join(sorted(n for n in TOOLS if n not in sa_enabled)) if sa_visible else ""
    sa_cap_raw = str(form.get("sa_cap", "3"))
    try:
        sa_cap = int(sa_cap_raw)
    except (TypeError, ValueError):
        sa_cap = 3

    # Higher tiers — tools are "sa_tool_N" and bodies are "subagent_body_N".
    tiers_json = _collect_tiers(form)

    # Global session-wide cap.
    max_conc_raw = str(form.get("max_concurrent", "100"))
    try:
        max_conc = int(max_conc_raw)
    except (TypeError, ValueError):
        max_conc = 100

    sa_model = str(form.get("sa_model", "")).strip()
    sa_tier1_model = str(form.get("sa_tier_model", "")).strip()

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
            "UPDATE prompts SET master_spawn_limit = ?, max_concurrent_subagents = ?, subagent_model = ?, sa_tier_model = ? WHERE kind = ? AND name = ?",
            (master_spawn, max_conc, sa_model or None, sa_tier1_model or None, SYSTEM, name),
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
        moved = await _propagate(name)

    return templates.TemplateResponse(
        request=request,
        name=_page_or_body(request, "prompts.html", "prompts_body.html"),
        context=await _prompts_context(selected=f"{kind}:{name}", saved=True, moved=moved),
    )


def _collect_tiers(form) -> str:
    """Collect higher subagent tiers from form fields into a JSON string."""
    import json as _json
    tier = 2  # tier 1 uses dedicated columns; tiers 2+ use indexed form fields
    tiers = []
    while True:
        body = str(form.get(f"subagent_body_{tier}", "")).strip()
        enabled = {str(v) for v in form.getlist(f"sa_tool_{tier}")}
        if not body and not enabled:
            break
        cap_raw = str(form.get(f"sa_cap_{tier}", "3"))
        try:
            cap = int(cap_raw)
        except (TypeError, ValueError):
            cap = 3
        model = str(form.get(f"sa_model_{tier}", "")).strip()
        off = ",".join(sorted(n for n in TOOLS if n not in enabled))
        tiers.append({"body": body, "disabled_tools": off, "parallel_cap": cap, "model": model})
        tier += 1
    if not tiers:
        return ""
    return _json.dumps(tiers)


@router.post("/_new_prompt")
async def new_prompt(request: Request):
    form = await request.form()
    name = _slug(str(form.get("new_name", "")))
    if not name:
        return RedirectResponse("/prompts", status_code=303)
    # Copy everything from the default profile.
    default = await db.get_prompt("default", SYSTEM)
    compact_default = await db.get_prompt("default", COMPACTION)
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
        " subagent_model = ?, sa_tier_model = ?, subagent_tiers = ?"
        " WHERE kind = ? AND name = ?",
        (default.get("master_spawn_limit") if default else 0,
         default.get("max_concurrent_subagents") if default else 100,
         default.get("subagent_model") if default else None,
         default.get("sa_tier_model") if default else None,
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
