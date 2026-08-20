"""Exporting and importing a profile, through the controls a person clicks.

There is already a unit suite for the bundle format. It passed while Export was
broken for every profile on the page, because it called the endpoint with a bare
profile name and the button does not: the picker holds `system:default`, so the
button asked the server for a profile literally called "system:default" and got
a 404. Testing the endpoint proved the endpoint. Only clicking the button proves
the button.

So these drive the real page in a real browser: click Export, catch the
download, read it back, feed it to the import review, and check what it says it
would do.
"""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _seed(data_dir: Path) -> None:
    """A profile with a tool selection, and a custom tool it may use."""
    from agent_server import database as db

    original = db.DB_PATH
    db.DB_PATH = data_dir / "agent.db"
    try:
        await db.init_db()
        await db.save_custom_tool(
            name="deploy_check", description="Checks the deploy",
            parameters='{"type":"object","properties":{}}',
            script="#!/usr/bin/env bash\necho checking\n",
            enabled=True, ask_permission=True,
        )
        await db.save_custom_tool(
            name="noisy_one", description="Too chatty",
            parameters='{"type":"object","properties":{}}',
            script="echo noise\n", enabled=True, ask_permission=True,
        )
        # Configured, so the tool selection is a real answer rather than the
        # "never configured" default that switches every custom tool off.
        await db.save_prompt("shipper", "You ship things carefully.", "system",
                             disabled_tools="noisy_one")
        await db.save_prompt("shipper", "Summarise tersely.", "compaction")
    finally:
        await db.close()
        db.DB_PATH = original


def _wait_for(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with {proc.returncode}")
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.5):
            return
        time.sleep(0.1)
    raise RuntimeError("server did not start")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("share-data")
    asyncio.run(_seed(data_dir))
    port = _free_port()
    env = {**os.environ, "CODEAGENT_DATA_DIR": str(data_dir), "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent_server.main:app", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(port, proc)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


@pytest.fixture
async def page(live, tmp_path):
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:  # no browser here means nothing to test against
            pytest.skip(f"no Playwright browser available: {exc}")
        ctx = await browser.new_context(
            viewport={"width": 1500, "height": 1000}, accept_downloads=True)
        pg = await ctx.new_page()
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await pg.goto(f"{live}/prompts", wait_until="networkidle")
        await pg.wait_for_selector("#prompt-picker")
        pg._errors = errors
        try:
            yield pg
        finally:
            await browser.close()


async def _download_text(caught) -> str:
    """The file the browser actually saved. `Download.path()` is async."""
    download = await caught.value
    return Path(await download.path()).read_text()


async def _select(pg, name):
    await pg.select_option("#prompt-picker", f"system:{name}")
    await pg.wait_for_load_state("networkidle")
    await pg.wait_for_selector("#prompt-picker")


# ── Export, by clicking Export ───────────────────────────────────────────────

async def test_the_export_button_downloads_a_bundle(page, tmp_path):
    """The test that would have caught it. The picker's value is
    `system:<name>`; sending that straight to the server asks for a profile that
    does not exist."""
    await _select(page, "shipper")
    async with page.expect_download() as caught:
        await page.click("button:has-text('Export')")
    download = await caught.value

    target = tmp_path / "bundle.json"
    await download.save_as(target)
    assert download.suggested_filename.endswith(".json")

    bundle = json.loads(target.read_text())
    assert bundle["format"] == "myriadcode.profile-bundle"
    assert bundle["profile"]["name"] == "shipper"
    assert bundle["profile"]["body"] == "You ship things carefully."
    assert bundle["profile"]["compaction_body"] == "Summarise tersely."


async def test_the_downloaded_bundle_carries_the_right_tools(page, tmp_path):
    await _select(page, "shipper")
    async with page.expect_download() as caught:
        await page.click("button:has-text('Export')")
    bundle = json.loads(await _download_text(caught))

    names = {t["name"] for t in bundle["tools"]}
    assert names == {"deploy_check"}, f"expected only deploy_check, got {names}"
    assert bundle["tools"][0]["script"].strip().endswith("echo checking")


async def test_exporting_every_profile_on_the_page_works(page, tmp_path):
    """Not just the selected one: the bug hit every profile equally."""
    names = await page.evaluate(
        "() => [...document.querySelectorAll('#prompt-picker option')].map(o => o.value)")
    assert len(names) >= 2
    for key in names:
        await page.select_option("#prompt-picker", key)
        await page.wait_for_load_state("networkidle")
        async with page.expect_download() as caught:
            await page.click("button:has-text('Export')")
        bundle = json.loads(await _download_text(caught))
        assert bundle["profile"]["name"] == key.split(":", 1)[1]


# ── Import, through the review the user is shown ─────────────────────────────

async def test_importing_shows_the_scripts_before_writing_anything(page, tmp_path):
    await _select(page, "shipper")
    async with page.expect_download() as caught:
        await page.click("button:has-text('Export')")
    raw = await _download_text(caught)

    shown = await page.evaluate("""async (raw) => {
        const form = new FormData();
        form.append('bundle', new File([raw], 'b.json', {type: 'application/json'}));
        const resp = await fetch('/_prompts/inspect', {method: 'POST', body: form});
        const data = await resp.json();
        if (!data.ok) return {error: data.error};
        pendingBundle = data.bundle;
        renderBundle(data.summary);
        return {
            open: !document.getElementById('bundle-modal').hidden,
            heading: document.querySelector('#bundle-summary p').textContent,
            scripts: [...document.querySelectorAll('#bundle-summary pre')].map(p => p.textContent),
            warning: document.getElementById('bundle-warning').textContent,
            rename: document.getElementById('bundle-rename').value,
        };
    }""", raw)

    assert not shown.get("error"), shown.get("error")
    assert shown["open"], "the review never appeared"
    assert "already exists" in shown["heading"]
    assert any("echo checking" in s for s in shown["scripts"]), (
        "the script was not put on screen before importing it"
    )
    assert "run on this machine" in shown["warning"]
    assert shown["rename"] == "shipper"


async def test_importing_under_a_new_name_leaves_the_original_alone(page):
    await _select(page, "shipper")
    async with page.expect_download() as caught:
        await page.click("button:has-text('Export')")
    raw = await _download_text(caught)

    result = await page.evaluate("""async (raw) => {
        const form = new FormData();
        form.append('bundle', new File([raw], 'b.json', {type: 'application/json'}));
        const data = await (await fetch('/_prompts/inspect', {method:'POST', body: form})).json();
        const resp = await fetch('/_prompts/import', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({bundle: data.bundle, rename: 'shipper (copy)'}),
        });
        return await resp.json();
    }""", raw)
    assert result["ok"], result
    assert result["name"] == "shipper (copy)"

    await page.goto(page.url.split("?")[0], wait_until="networkidle")
    options = await page.evaluate(
        "() => [...document.querySelectorAll('#prompt-picker option')].map(o => o.textContent.trim())")
    assert "shipper" in options
    assert "shipper (copy)" in options


async def test_a_file_that_is_not_a_bundle_is_refused_readably(page):
    message = await page.evaluate("""async () => {
        const form = new FormData();
        form.append('bundle', new File(['{"format":"nope"}'], 'x.json'));
        const resp = await fetch('/_prompts/inspect', {method: 'POST', body: form});
        return (await resp.json()).error;
    }""")
    assert "not a MyriadCode profile bundle" in message


async def test_the_page_raises_no_errors_doing_any_of_this(page):
    await _select(page, "shipper")
    async with page.expect_download() as caught:
        await page.click("button:has-text('Export')")
    await caught.value
    # The deliberate 400 above is a console message on other tests, not here.
    assert page._errors == []
