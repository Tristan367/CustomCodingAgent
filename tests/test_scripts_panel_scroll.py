"""Picking a script should not throw you back to the top of the page.

Selecting one navigates -- `/?script=name` re-renders the home page with the
editor in it -- and a navigation lands at the top. The scripts panel is a long
way down a long page, so choosing a script to run meant scrolling all the way
back to where you already were. Saving, deleting and creating a script come back
through the same URL, so they had it too.
"""

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def home(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("scripts-data")
    port = _free_port()
    env = {**os.environ, "CODEAGENT_DATA_DIR": str(data_dir), "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent_server.main:app", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited with {proc.returncode}")
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.5):
                break
            time.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


@pytest.fixture
async def page(home):
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no Playwright browser available: {exc}")
        pg = await browser.new_page(viewport={"width": 1300, "height": 760})
        pg._home = home
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg._console_errors = errors
        try:
            yield pg
        finally:
            await browser.close()


async def _panel(pg):
    return await pg.evaluate("""
    () => {
      const p = document.getElementById('scripts-panel');
      if (!p) return { missing: true };
      const r = p.getBoundingClientRect();
      return { top: Math.round(r.top),
               onScreen: r.bottom > 0 && r.top < window.innerHeight,
               viewport: window.innerHeight };
    }
    """)


async def test_the_panel_starts_below_the_fold(page):
    """The premise: if it were already on screen there would be nothing to fix,
    and the test below would pass for the wrong reason."""
    await page.goto(page._home + "/", wait_until="networkidle")
    await page.wait_for_timeout(400)
    panel = await _panel(page)
    assert not panel.get("missing"), "the home page has no scripts panel"
    assert not panel["onScreen"], (
        f"the scripts panel is already visible without scrolling ({panel}); "
        "this test can no longer tell whether the fix works")


async def test_choosing_a_script_lands_on_the_panel(page):
    await page.goto(page._home + "/?script=anything", wait_until="networkidle")
    await page.wait_for_timeout(600)
    panel = await _panel(page)
    assert not panel.get("missing")
    assert panel["onScreen"], (
        f"selecting a script left the panel off screen ({panel}) -- back to "
        "scrolling down to it every time")
    assert panel["top"] < panel["viewport"] / 2, (
        f"the panel is on screen but near the bottom ({panel})")


async def test_a_plain_visit_still_starts_at_the_top(page):
    """Only a named script scrolls. Opening the home page normally should look
    the way it always did."""
    await page.goto(page._home + "/", wait_until="networkidle")
    await page.wait_for_timeout(500)
    panel = await _panel(page)
    assert not panel["onScreen"], "a plain visit jumped down the page"
    assert page._console_errors == []
