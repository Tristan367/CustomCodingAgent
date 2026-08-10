"""Playwright browser tooling for UI testing.

Tools:
- browser-goto:  navigate to a URL and capture a screenshot
- browser-click: click a CSS selector and capture what changed
- browser-fill:  type text into a field and capture after
- browser-screenshot: capture the current page without acting

All share one long-lived browser context to avoid cold starts.
"""

import asyncio
from typing import Any

from agent_server.tools.base import ToolContext, ToolResult

_BROWSER: Any = None
_PAGE: Any = None
_PW: Any = None
_LOCK = asyncio.Lock()


async def _ensure_browser():
    global _BROWSER, _PAGE, _PW
    if _BROWSER is None:
        from playwright.async_api import async_playwright
        _PW = await async_playwright().start()
        try:
            _BROWSER = await _PW.chromium.launch(headless=True)
            _PAGE = await _BROWSER.new_page(viewport={"width": 1080, "height": 1920})
        except Exception:
            if _BROWSER:
                try:
                    await _BROWSER.close()
                except Exception:
                    pass
                _BROWSER = None
            await _PW.stop()
            _PW = None
            raise


async def close_browser():
    global _BROWSER, _PAGE, _PW
    if _BROWSER:
        try:
            await _BROWSER.close()
        except Exception:
            pass
        _BROWSER = None
        _PAGE = None
    if _PW:
        try:
            await _PW.stop()
        except Exception:
            pass
        _PW = None


async def browser_goto(ctx: ToolContext, url: str, **_) -> ToolResult:
    async with _LOCK:
        try:
            await _ensure_browser()
            await _PAGE.goto(url, wait_until="load", timeout=30000)
            img = await _PAGE.screenshot()
            result = await _analyze(img, f"Describe what is visible on this page at {url}. What are the main elements?")
            return ToolResult(output=result, title=f"browser: {url[:60]}")
        except Exception as e:
            return ToolResult.error(f"browser-goto failed: {e}", "browser-goto")


async def browser_click(ctx: ToolContext, selector: str, **_) -> ToolResult:
    async with _LOCK:
        try:
            await _ensure_browser()
            await _PAGE.click(selector, timeout=10000)
            await asyncio.sleep(0.5)
            img = await _PAGE.screenshot()
            result = await _analyze(img, f"After clicking '{selector}', what changed on the page? Describe the new state.")
            return ToolResult(output=result, title=f"browser: click {selector}")
        except Exception as e:
            return ToolResult.error(f"browser-click failed: {e}", "browser-click")


async def browser_fill(ctx: ToolContext, selector: str, text: str, **_) -> ToolResult:
    async with _LOCK:
        try:
            await _ensure_browser()
            await _PAGE.fill(selector, text, timeout=10000)
            await asyncio.sleep(0.3)
            img = await _PAGE.screenshot()
            result = await _analyze(img, f"After typing '{text}' into '{selector}', what does the page show now?")
            return ToolResult(output=result, title=f"browser: fill {selector}")
        except Exception as e:
            return ToolResult.error(f"browser-fill failed: {e}", "browser-fill")


async def browser_screenshot(ctx: ToolContext, **_) -> ToolResult:
    async with _LOCK:
        try:
            await _ensure_browser()
            img = await _PAGE.screenshot()
            result = await _analyze(img, "Describe the current state of the page.")
            return ToolResult(output=result, title="browser: screenshot")
        except Exception as e:
            return ToolResult.error(f"browser-screenshot failed: {e}", "browser-screenshot")


async def browser_steps(ctx: ToolContext, steps: list[dict], **_) -> ToolResult:
    """Execute a sequence of browser actions, screenshot at each step."""
    if not steps:
        return ToolResult.error("steps list is empty", "browser-steps")
    if len(steps) > 8:
        steps = steps[:8]

    async with _LOCK:
        try:
            await _ensure_browser()
            reports: list[str] = []
            for i, step in enumerate(steps):
                action = step.get("action", "")
                if action == "goto":
                    url = step.get("url", "")
                    if not url:
                        return ToolResult.error(f"step {i + 1}: missing url for goto", "browser-steps")
                    await _PAGE.goto(url, timeout=30000)
                    await asyncio.sleep(1)
                elif action == "click":
                    sel = step.get("selector", "")
                    if not sel:
                        return ToolResult.error(f"step {i + 1}: missing selector for click", "browser-steps")
                    await _PAGE.click(sel, timeout=10000)
                    await asyncio.sleep(0.5)
                elif action == "fill":
                    sel = step.get("selector", "")
                    txt = step.get("text", "")
                    if not sel:
                        return ToolResult.error(f"step {i + 1}: missing selector for fill", "browser-steps")
                    await _PAGE.fill(sel, txt, timeout=10000)
                    await asyncio.sleep(0.3)
                elif action == "wait":
                    ms = min(step.get("ms", 1000), 5000)
                    await asyncio.sleep(ms / 1000)
                else:
                    return ToolResult.error(f"step {i + 1}: unknown action '{action}'", "browser-steps")

                img = await _PAGE.screenshot()
                analysis = await _analyze(img, f"Step {i + 1}/{len(steps)} ({action}): what changed?")
                reports.append(f"[Step {i + 1}] {action}: {analysis}")

            combined = "\n\n".join(reports)
            return ToolResult(
                output=combined,
                title=f"browser: {len(steps)} steps",
            )
        except Exception as e:
            return ToolResult.error(f"browser-steps failed: {e}", "browser-steps")


async def _analyze(img: bytes, prompt: str) -> str:
    try:
        from agent_server.vision import analyze, normalize_image
        decoded = normalize_image(img)
        return await analyze([decoded], prompt)
    except Exception as e:
        return f"(vision not available: {e})\n[raw screenshot captured but not analyzed]"
