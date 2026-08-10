"""Custom provider endpoint CRUD."""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from agent_server import database as db
from agent_server.providers import load_custom_endpoint_providers
from agent_server.routes.context import _home_context, _slug
from agent_server.templating import templates

router = APIRouter()


# ── Custom endpoints ────────────────────────────────────────────────────────


@router.post("/_save_custom_endpoint")
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
    await load_custom_endpoint_providers()
    return templates.TemplateResponse(
        request=request, name="index_content.html", context=await _home_context(),
    )


@router.post("/_delete_custom_endpoint")
async def delete_custom_endpoint(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        await db.delete_custom_endpoint(name)
        await load_custom_endpoint_providers()
    return templates.TemplateResponse(
        request=request, name="index_content.html", context=await _home_context(),
    )


@router.post("/_new_custom_endpoint")
async def new_custom_endpoint(request: Request):
    form = await request.form()
    name = _slug(str(form.get("name", "")))
    if not name:
        return RedirectResponse("/", status_code=303)
    if not await db.get_custom_endpoint(name):
        await db.save_custom_endpoint(name, "", "")
    await load_custom_endpoint_providers()
    return RedirectResponse("/", status_code=303)
