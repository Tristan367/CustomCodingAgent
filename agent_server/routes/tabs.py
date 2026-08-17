"""Tab bar and tab management endpoints."""

from fastapi import APIRouter, Request

from agent_server import database as db
from agent_server.routes.context import (
    _open_tabs,
    _save_tabs,
    _stop_watching,
)
from agent_server.templating import templates

router = APIRouter()


# ── Tabs ────────────────────────────────────────────────────────────────────


@router.get("/_tab_bar")
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


@router.post("/_tab_close/{session_id}")
async def tab_close(session_id: str):
    _stop_watching(session_id)
    tabs = await _open_tabs()
    if session_id in tabs:
        tabs.remove(session_id)
        await _save_tabs(tabs)
    return {"ok": True, "tabs": tabs}


@router.post("/_tab_order")
async def tab_order(payload: dict):
    ids = [str(i) for i in payload.get("ids", [])]
    # Deduplicate and drop ids whose sessions no longer exist, so a crafted or
    # stale order cannot pin a ghost tab to the bar.
    valid = {s["id"] for s in await db.list_sessions()}
    seen: set[str] = set()
    ordered = []
    for sid in ids:
        if sid in valid and sid not in seen:
            seen.add(sid)
            ordered.append(sid)
    await _save_tabs(ordered)
    return {"ok": True}
