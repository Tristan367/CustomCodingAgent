"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agent_server import agent
from agent_server import database as db
from agent_server.database import close as close_db
from agent_server.database import init_db
from agent_server.providers import load_custom_endpoint_providers
from agent_server.routes import (
    chat,
    custom_tools,
    endpoints,
    pages,
    projects,
    prompts,
    scripts,
    sessions,
    settings,
    sounds,
    tabs,
    tts,
)
from agent_server.system_prompt import migrate_prompts
from agent_server.templating import STATIC_DIR


async def _reap_browsers():
    """Close browser contexts nobody has used lately.

    A Chromium context is about 100MB and holds whatever the session was
    logged into, so leaving one per session open indefinitely is neither free
    nor especially private.
    """
    from agent_server import browser

    while True:
        await asyncio.sleep(120)
        try:
            await browser.reap_idle()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await migrate_prompts()
    from agent_server.providers import credentials
    from agent_server.tools.custom import load_custom_tools

    # Fill the key cache from the async connection, so no provider has to open
    # its own blocking sqlite handle on the event loop to find its key.
    credentials.prime(await db.get_all_settings())
    problems = await load_custom_tools()
    for problem in problems:
        print(f"[tools] {problem}")
    await load_custom_endpoint_providers()
    reaper = asyncio.create_task(_reap_browsers())

    yield

    reaper.cancel()
    from agent_server.tools import browser

    # Stop in-flight turns before closing the database underneath them. A run
    # is a server-owned task, so shutdown used to leave them writing into a
    # connection that had just been closed, losing the assistant message and
    # raising into a background task nobody was watching.
    await agent.shutdown()
    await browser.close_browser()
    await close_db()


app = FastAPI(title="CodeAgent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(sessions.router)
app.include_router(chat.router)
app.include_router(tts.router)
app.include_router(pages.router)
app.include_router(tabs.router)
app.include_router(settings.router)
app.include_router(sounds.router)
app.include_router(projects.router)
app.include_router(prompts.router)
app.include_router(custom_tools.router)
app.include_router(endpoints.router)
app.include_router(scripts.router)
