"""The file manager, driven in a browser.

Three things reported from use, none of which a unit test can see:

  * a project root is mostly dot-directories, and they crowded out everything
    worth clicking;
  * double-clicking a row to open it flashed the name highlighted on the way,
    because the browser selects the word under the pointer before the
    navigation happens;
  * opening a picture from the manager put the picture *behind* it. The manager
    is a `<dialog>` shown with `showModal()`, so it lives in the browser's top
    layer, and no z-index on an ordinary element can reach past that.

Plus the icons: a listing should be readable at a glance rather than by
squinting at extensions.
"""

import asyncio
import base64
import contextlib
import math
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

REPO = Path(__file__).resolve().parent.parent

# The smallest valid PNG, so the preview has something real to decode.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

VISIBLE = ["shot.png", "tone.wav", "clip.mp4", "code.py", "data.json",
           "book.pdf", "sheet.csv", "bundle.zip", "font.ttf", "notes.txt", "README"]


def _wav_bytes(seconds: int = 1, rate: int = 8000) -> bytes:
    """A real, playable WAV. A stub would load as an error and prove nothing."""
    frames = b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(seconds * rate)
    )
    return (
        b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(frames)) + frames
    )


HIDDEN = [".hidden_a", ".hidden_b", ".config"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _seed(data_dir: Path, project: Path) -> str:
    from agent_server import database as db

    original = db.DB_PATH
    db.DB_PATH = data_dir / "agent.db"
    try:
        await db.init_db()
        session = await db.create_session(name="files", project_dir=str(project))
        return session["id"]
    finally:
        await db.close()
        db.DB_PATH = original


@pytest.fixture(scope="module")
def live_ui(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("fm-data")
    project = tmp_path_factory.mktemp("fm-project")
    (project / "shot.png").write_bytes(ONE_PIXEL_PNG)
    (project / "tone.wav").write_bytes(_wav_bytes())
    (project / "book.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    for name in VISIBLE:
        if name not in ("shot.png", "tone.wav", "book.pdf"):
            (project / name).write_text("x")
    for name in HIDDEN:
        (project / name).write_text("x")
    (project / ".git").mkdir()
    (project / "subdir").mkdir()

    session_id = asyncio.run(_seed(data_dir, project))
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
        yield f"http://127.0.0.1:{port}/sessions/{session_id}"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


@pytest.fixture
async def manager(live_ui):
    """A page with the file manager open on the project directory."""
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no Playwright browser available: {exc}")
        pg = await browser.new_page(viewport={"width": 1300, "height": 880})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await pg.goto(live_ui, wait_until="networkidle")
        # Each test starts from the shipped default, not whatever the last one left.
        await pg.evaluate("() => { try { localStorage.removeItem('fb-show-all'); } catch {} }")
        await pg.evaluate("() => FileBrowser.open(null, { attach: false })")
        await pg.wait_for_selector(".fb-row")
        await pg.wait_for_timeout(250)
        pg._console_errors = errors
        try:
            yield pg
        finally:
            await browser.close()


async def _names(pg):
    return await pg.evaluate(
        "() => [...document.querySelectorAll('.fb-name')].map(n => n.textContent)")


# ── Hidden entries ───────────────────────────────────────────────────────────

async def test_dotfiles_are_hidden_to_begin_with(manager):
    names = await _names(manager)
    assert set(names) >= set(VISIBLE), f"an ordinary file went missing: {names}"
    assert not [n for n in names if n.startswith(".")], (
        f"hidden entries are showing by default: {[n for n in names if n.startswith('.')]}")


async def test_the_box_is_unticked_and_brings_them_back(manager):
    box = await manager.query_selector("[data-fb=showall]")
    assert box, "there is no Show all control"
    assert not await box.is_checked(), "hidden entries should be off by default"

    await box.click()
    await manager.wait_for_timeout(600)
    names = await _names(manager)
    for name in HIDDEN + [".git"]:
        assert name in names, f"{name} did not come back: {names}"
    assert set(names) >= set(VISIBLE), "the ordinary files went away"


async def test_the_choice_survives_navigating(manager):
    await manager.click("[data-fb=showall]")
    await manager.wait_for_timeout(600)
    await manager.evaluate("() => FileBrowser.open(null, { attach: false })")
    await manager.wait_for_timeout(600)
    assert ".config" in await _names(manager), "the setting did not stick"


# ── Double-click ─────────────────────────────────────────────────────────────

async def test_double_clicking_a_row_selects_no_text(manager):
    """The name used to flash highlighted for the instant before the directory
    changed. Cancelling selection on the second click of a double leaves
    click-and-drag selection working, which `user-select: none` would not."""
    selected = await manager.evaluate("""
    async () => {
      const row = [...document.querySelectorAll('.fb-row')][0];
      const name = row.querySelector('.fb-name');
      const r = name.getBoundingClientRect();
      const at = { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
      for (const detail of [1, 2]) {
        for (const type of ['mousedown', 'mouseup', 'click']) {
          name.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, detail, ...at }));
        }
      }
      await new Promise(r => setTimeout(r, 120));
      return String(window.getSelection());
    }
    """)
    assert selected == "", f"double-clicking selected {selected!r}"


# ── Icons ────────────────────────────────────────────────────────────────────

async def test_every_kind_of_file_gets_its_own_mark(manager):
    marks = await manager.evaluate("""
    () => Object.fromEntries([...document.querySelectorAll('.fb-row')].map(r => [
        r.querySelector('.fb-name').textContent,
        r.querySelector('.fb-icon').textContent]))
    """)
    assert marks.get("subdir"), "a directory lost its arrow"
    for name in ("shot.png", "tone.wav", "clip.mp4", "code.py", "bundle.zip"):
        assert marks.get(name), f"{name} has no mark"

    kinds = {marks[n] for n in ("shot.png", "tone.wav", "clip.mp4", "bundle.zip", "subdir")}
    assert len(kinds) == 5, f"different kinds share a mark: {marks}"


async def test_a_file_with_no_extension_still_gets_one(manager):
    """Otherwise the column jumps about and an unknown file reads as nothing."""
    mark = await manager.evaluate("""
    () => { const r = [...document.querySelectorAll('.fb-row')]
              .find(x => x.querySelector('.fb-name').textContent === 'README');
            return r ? r.querySelector('.fb-icon').textContent : null; }
    """)
    assert mark, "a file without an extension has no mark"


async def test_the_names_all_start_at_the_same_x(manager):
    """A fixed icon column, or a listing shuffles sideways row by row."""
    lefts = await manager.evaluate("""
    () => [...new Set([...document.querySelectorAll('.fb-name')]
            .map(n => Math.round(n.getBoundingClientRect().left)))]
    """)
    assert len(lefts) == 1, f"names start at different x: {sorted(lefts)}"


# ── The picture goes on top ──────────────────────────────────────────────────

async def test_a_picture_opens_above_the_file_manager(manager):
    """The manager is a modal dialog and therefore in the browser's top layer.
    An ordinary element cannot be drawn above that at any z-index, so the
    preview has to be a dialog too."""
    await manager.evaluate("""
    () => { const row = [...document.querySelectorAll('.fb-row')]
              .find(r => r.dataset.path.endsWith('shot.png'));
            row.dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); }
    """)
    await manager.wait_for_timeout(900)

    state = await manager.evaluate("""
    () => {
      const preview = document.querySelector('.image-preview');
      const browser = document.getElementById('file-browser');
      if (!preview) return { missing: true };
      const r = preview.getBoundingClientRect();
      const hit = (x, y) => {
        const el = document.elementFromPoint(x, y);
        return !!(el && el.closest('.image-preview'));
      };
      return {
        previewOpen: preview.open === true,
        managerStillOpen: !!browser && browser.open,
        coversViewport: r.width >= window.innerWidth - 1 && r.height >= window.innerHeight - 1,
        onTopInTheMiddle: hit(window.innerWidth / 2, window.innerHeight / 2),
        onTopInTheCorner: hit(6, 6),
      };
    }
    """)
    assert not state.get("missing"), "the preview never opened"
    assert state["previewOpen"], "the preview is not showing"
    assert state["managerStillOpen"], (
        "the manager closed -- the point is that it does not have to")
    assert state["coversViewport"], "the preview does not cover the window"
    assert state["onTopInTheMiddle"] and state["onTopInTheCorner"], (
        "the file manager is painted over the picture")


async def test_closing_the_picture_leaves_the_manager_usable(manager):
    await manager.evaluate("""
    () => { const row = [...document.querySelectorAll('.fb-row')]
              .find(r => r.dataset.path.endsWith('shot.png'));
            row.dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); }
    """)
    await manager.wait_for_timeout(800)
    await manager.keyboard.press("Escape")
    await manager.wait_for_timeout(500)

    after = await manager.evaluate("""
    () => {
      const preview = document.querySelector('.image-preview');
      const browser = document.getElementById('file-browser');
      return { previewOpen: !!preview && preview.open === true,
               bitmapDropped: !!preview && !preview.querySelector('img').getAttribute('src'),
               managerOpen: !!browser && browser.open,
               rows: document.querySelectorAll('.fb-row').length };
    }
    """)
    assert not after["previewOpen"], "Escape did not close the picture"
    assert after["bitmapDropped"], (
        "the decoded image was left in memory -- a dialog closed with Escape "
        "does not run the code that dismissing it by click would")
    assert after["managerOpen"] and after["rows"], "the manager did not survive"


async def test_none_of_this_raises_in_the_console(manager):
    await manager.click("[data-fb=showall]")
    await manager.wait_for_timeout(500)
    assert manager._console_errors == []


# ── Sound and video ──────────────────────────────────────────────────────────

async def _open(pg, suffix):
    await pg.evaluate(
        """(suffix) => { const row = [...document.querySelectorAll('.fb-row')]
             .find(r => r.dataset.path.endsWith(suffix));
             row.dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); }""",
        suffix)
    await pg.wait_for_timeout(900)


async def test_a_sound_file_opens_a_player_over_the_manager(manager):
    await _open(manager, "tone.wav")
    state = await manager.evaluate("""
    () => {
      const d = document.querySelector('.media-preview');
      if (!d) return { missing: true };
      const m = d.querySelector('audio, video');
      const r = d.getBoundingClientRect();
      const mid = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
      return { open: d.open === true, tag: m && m.tagName, controls: m && m.controls,
               covers: r.width >= window.innerWidth - 1 && r.height >= window.innerHeight - 1,
               onTop: !!(mid && mid.closest('.media-preview')),
               managerStillOpen: document.getElementById('file-browser').open };
    }
    """)
    assert not state.get("missing"), "no player appeared"
    assert state["open"] and state["tag"] == "AUDIO"
    assert state["controls"], "a player with no controls cannot be played"
    assert state["covers"] and state["onTop"], "the manager is painted over the player"
    assert state["managerStillOpen"], "the manager should not have to close"


async def test_a_video_gets_a_video_element(manager):
    await _open(manager, "clip.mp4")
    tag = await manager.evaluate(
        "() => { const m = document.querySelector('.media-preview audio, .media-preview video');"
        " return m && m.tagName; }")
    assert tag == "VIDEO", f"a video opened as {tag}"


async def test_escape_stops_the_sound(manager):
    """A modal dialog closes on Escape by itself, without running the code that
    dismissing it by click would -- so the tidying hangs off `close`. Left
    undone, the sound goes on playing to an empty screen."""
    await _open(manager, "tone.wav")
    await manager.keyboard.press("Escape")
    await manager.wait_for_timeout(500)
    after = await manager.evaluate("""
    () => { const d = document.querySelector('.media-preview');
            return { open: d.open === true,
                     stillPlaying: !!d.querySelector('audio, video'),
                     managerOpen: document.getElementById('file-browser').open }; }
    """)
    assert not after["open"], "Escape did not close the player"
    assert not after["stillPlaying"], "the media element was left in the page, still loaded"
    assert after["managerOpen"], "the manager did not survive"


async def test_a_pdf_is_handed_to_a_new_tab(manager):
    """The browser renders one better than anything here would.

    What is asserted is the decision -- the URL handed to `window.open` -- not
    the resulting tab: headless Chromium has no PDF viewer, so the tab it opens
    never finishes loading and waiting on it can only time out.
    """
    asked = await manager.evaluate("""
    async () => {
      const calls = [];
      const real = window.open;
      window.open = (url, ...rest) => { calls.push({ url, rest }); return null; };
      try {
        const row = [...document.querySelectorAll('.fb-row')]
          .find(r => r.dataset.path.endsWith('book.pdf'));
        row.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
        await new Promise(r => setTimeout(r, 300));
      } finally {
        window.open = real;
      }
      return calls;
    }
    """)
    assert len(asked) == 1, f"a PDF should open exactly one tab, got {asked}"
    url = asked[0]["url"]
    assert "/api/files/media" in url, f"the tab was not pointed at the file: {url}"
    assert "book.pdf" in url
    assert "_blank" in asked[0]["rest"], "the PDF should go to a new tab"

    still_open = await manager.evaluate(
        "() => document.getElementById('file-browser').open")
    assert still_open, "the manager should stay open behind the new tab"


async def test_a_text_file_still_opens_the_editor(manager):
    """The fall-through has to keep working: everything above is a special case
    layered on top of it."""
    opened = await manager.evaluate("""
    async () => {
      const row = [...document.querySelectorAll('.fb-row')]
        .find(r => r.dataset.path.endsWith('notes.txt'));
      row.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
      await new Promise(r => setTimeout(r, 600));
      return { player: !!document.querySelector('.media-preview[open]'),
               picture: !!document.querySelector('.image-preview[open]') };
    }
    """)
    assert not opened["player"] and not opened["picture"], (
        "a text file opened a media surface")


async def test_the_media_route_serves_byte_ranges(manager):
    """Not optional for video: Chrome will not scrub, and often will not start,
    a `<video>` whose source cannot answer a Range request with a 206."""
    result = await manager.evaluate("""
    async () => {
      const row = [...document.querySelectorAll('.fb-row')]
        .find(r => r.dataset.path.endsWith('tone.wav'));
      const url = `/api/files/media?path=${encodeURIComponent(row.dataset.path)}`;
      const resp = await fetch(url, { headers: { Range: 'bytes=0-99' } });
      return { status: resp.status,
               range: resp.headers.get('Content-Range'),
               accepts: resp.headers.get('Accept-Ranges') };
    }
    """)
    assert result["status"] == 206, f"a Range request got {result['status']}, not 206"
    assert result["range"] and result["range"].startswith("bytes 0-99/")
    assert result["accepts"] == "bytes"


async def test_the_media_route_refuses_anything_else(manager):
    """The allowlist is what stops this being a general file read."""
    status = await manager.evaluate("""
    async () => {
      const row = [...document.querySelectorAll('.fb-row')]
        .find(r => r.dataset.path.endsWith('code.py'));
      const resp = await fetch(
        `/api/files/media?path=${encodeURIComponent(row.dataset.path)}`);
      return resp.status;
    }
    """)
    assert status == 403, f"a .py file was served as media ({status})"


# ── Bigger marks, same rows ──────────────────────────────────────────────────

async def test_the_marks_are_larger_than_the_text_but_cost_no_height(manager):
    """They are drawn about twice the size of the name and allowed to spill out
    of their box, so a listing of 200 files is exactly as tall as it was."""
    sizes = await manager.evaluate("""
    () => {
      const row = document.querySelector('.fb-row');
      const icon = row.querySelector('.fb-icon');
      const rows = [...document.querySelectorAll('.fb-row')]
        .map(r => Math.round(r.getBoundingClientRect().height));
      return { iconFont: parseFloat(getComputedStyle(icon).fontSize),
               rowFont: parseFloat(getComputedStyle(row).fontSize),
               iconBoxHeight: Math.round(icon.getBoundingClientRect().height),
               rowHeights: [...new Set(rows)] };
    }
    """)
    assert sizes["iconFont"] >= sizes["rowFont"] * 1.4, (
        f"the marks are not noticeably bigger: {sizes}")
    assert sizes["iconBoxHeight"] == 0, (
        f"the mark's box is {sizes['iconBoxHeight']}px tall, so it is pushing the row open")
    assert len(sizes["rowHeights"]) == 1, (
        f"rows ended up different heights: {sizes['rowHeights']}")
