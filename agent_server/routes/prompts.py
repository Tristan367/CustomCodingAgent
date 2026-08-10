"""System and summarising prompt editor, plus subagent defaults."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from agent_server import database as db
from agent_server.routes.context import _list_uploaded_sounds, _slug, _sound_enabled
from agent_server.system_prompt import (
    COMPACTION,
    PROTECTED_PROMPT,
    SYSTEM,
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
        # Keyed by the same "kind:name" the picker uses, so the warning can
        # follow the selection without another round trip.
        "drift": {k: prompt_drift(v) for k, v in bodies.items() if prompt_drift(v)},
        "body": bodies.get(selected, ""),
        "protected": PROTECTED_PROMPT,
        # Subagent defaults
        "sa_prompt": await db.get_setting("subagent_prompt", ""),
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
            f"{p['kind']}:{p['name']}": [
                n.strip() for n in (p["disabled_tools"] or "").split(",") if n.strip()
            ]
            for p in prompts
        },
        "saved": saved,
        "moved": moved,
        "sound_enabled": await _sound_enabled(),
        "uploaded_sounds": _list_uploaded_sounds(),
    }


@router.get("/prompts")
async def prompts_page(request: Request, selected: str = ""):
    return templates.TemplateResponse(
        request=request, name="prompts.html", context=await _prompts_context(selected)
    )


@router.post("/_save_prompts")
async def save_prompts(request: Request):
    """Save one prompt's text."""
    form = await request.form()
    kind, _, name = str(form.get("name", "")).partition(":")
    name = name.strip()
    body = str(form.get("body", "")).strip()

    # Unchecked boxes are simply absent from the form, so the set of tools to
    # switch off is everything the registry has minus everything ticked.
    enabled = {str(v) for v in form.getlist("tool")}
    off = ",".join(sorted(n for n in TOOLS if n not in enabled))

    moved = 0
    if name and body and kind in (SYSTEM, COMPACTION):
        await db.save_prompt(name, body, kind, off if kind == SYSTEM else None)
        if kind == SYSTEM:
            moved = await _propagate(name)

    return templates.TemplateResponse(
        request=request,
        name="prompts.html",
        context=await _prompts_context(selected=f"{kind}:{name}", saved=True, moved=moved),
    )


@router.post("/_new_prompt")
async def new_prompt(request: Request):
    form = await request.form()
    kind = str(form.get("kind", SYSTEM))
    name = _slug(str(form.get("new_name", "")))
    if not name or kind not in (SYSTEM, COMPACTION):
        return RedirectResponse("/prompts", status_code=303)
    await db.save_prompt(name, "", kind)
    return RedirectResponse(f"/prompts?selected={kind}:{name}", status_code=303)


@router.post("/_save_subagent")
async def save_subagent(request: Request):
    form = await request.form()
    use_system = str(form.get("use_system_prompt", "")) == "1"
    prompt = str(form.get("prompt", "")).strip() if not use_system else ""

    await db.set_setting("subagent_prompt", prompt)

    return templates.TemplateResponse(
        request=request, name="prompts.html", context=await _prompts_context(saved=True),
    )


@router.post("/_delete_prompt")
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
