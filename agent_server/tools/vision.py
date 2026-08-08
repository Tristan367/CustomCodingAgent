"""Vision: screenshot a URL or describe an image via the local Ollama rig.

VisionHelper exposes a synchronous, Playwright-backed API. Playwright's sync
interface raises if constructed inside a running event loop, so every call here
is dispatched to a worker thread.
"""

import asyncio
import sys
from pathlib import Path

from agent_server.config import VISION_HELPER_PATH, VISION_OLLAMA_URL
from agent_server.tools.base import ToolContext, ToolResult

SCREENSHOT_TIMEOUT = 120
_import_lock = asyncio.Lock()


def _load_helper():
    """Import VisionHelper's `core` module, adding it to sys.path once."""
    if VISION_HELPER_PATH not in sys.path:
        sys.path.insert(0, VISION_HELPER_PATH)
    import core  # type: ignore

    return core


async def vision(
    ctx: ToolContext,
    *,
    url: str,
    prompt: str | None = None,
    selector: str | None = None,
    width: int = 1280,
    height: int = 900,
    **_,
) -> ToolResult:
    title = f"vision {url[:70]}"
    if not Path(VISION_HELPER_PATH).is_dir():
        return ToolResult.error(
            f"VisionHelper not found at {VISION_HELPER_PATH}. Set VISION_HELPER_PATH.", title
        )

    def _run() -> str:
        core = _load_helper()
        shot = core.capture_screenshot(
            url=url, selector=selector, crop=bool(selector), width=width, height=height
        )
        return core.analyze(
            shot, prompt=prompt or core.DEFAULT_PROMPT, ollama_url=VISION_OLLAMA_URL
        )

    try:
        # Playwright sync API cannot run on the event loop thread.
        result = await asyncio.wait_for(asyncio.to_thread(_run), timeout=SCREENSHOT_TIMEOUT)
    except asyncio.TimeoutError:
        return ToolResult.error(f"vision timed out after {SCREENSHOT_TIMEOUT}s", title)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(f"{type(e).__name__}: {e}", title)

    return ToolResult(output=result, title=title)


async def describe_image(image_path: str, prompt: str | None = None) -> str:
    """Describe a user-uploaded image. Used by the chat image-attach flow."""
    def _run() -> str:
        core = _load_helper()
        return core.analyze_image_file(
            image_path=image_path,
            prompt=prompt or "Describe this image in detail.",
            ollama_url=VISION_OLLAMA_URL,
        )

    return await asyncio.wait_for(asyncio.to_thread(_run), timeout=SCREENSHOT_TIMEOUT)


async def rig_available() -> bool:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.get(f"{VISION_OLLAMA_URL}/api/tags")
            return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False
