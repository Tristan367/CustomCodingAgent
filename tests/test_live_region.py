"""The foot of the transcript, where the agent's current activity is drawn.

Four faults were reported here at once, and they had one thing in common: each
was a *difference* between two states that replace each other many times a turn.

  1. The four transient rows measured 28, 31, 30 and 40px, so every swap between
     them moved the whole transcript by the difference.
  2. Tool labels sat 12px right of the assistant text beside them, because the
     spinner dot was a flow item -- and removing it when the call finished moved
     the label sideways.
  3. The live line was removed on every event and put back by only some, so the
     foot dropped a row and regained it between phases.
  4. Thinking blocks and tool blocks each took the out-of-flow overlay without
     checking whether the other had it. Two overlays 30px apart, both up to
     22vh tall, painted over each other: a streaming thinking block covered 86%
     of the diff above it, which stayed open and visible underneath.

Two more were then introduced by the fixes above and reported from a real
session rather than caught here, so they have guards of their own at the bottom:
a block pulled into the gutter with a negative margin was sliced down its left
side by its own scroll box, and the marker moved in to clear the role label
ended up 2px from the tool's own name. It lives at the right-hand end now.

Every assertion here is a measurement, and each one failed before the fix.
"""

import asyncio
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
VIEWPORT = {"width": 1400, "height": 950}
DIFF = "\n".join(f"-old line {i}\n+new line {i}" for i in range(30))

# Sub-pixel layout rounding only. Anything larger is a real difference.
TOLERANCE = 1.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _seed(data_dir: Path) -> str:
    from agent_server import database as db

    original = db.DB_PATH
    db.DB_PATH = data_dir / "agent.db"
    try:
        await db.init_db()
        session = await db.create_session(name="live", project_dir=str(REPO))
        await db.add_message(session["id"], "user", "Do the thing.")
        for i in range(6):
            await db.add_message(session["id"], "assistant", f"Let me get on that, step {i}.")
        # Rows rendered by the *server*, which is what a reload shows. The rest
        # of this module drives `handleEvent` and so only ever exercises the
        # markup app.js builds; the two templates are separate and can drift.
        await db.add_message(
            session["id"], "tool", "collected 40 items\nall passed",
            tool_call_id="seed-bash", tool_name="bash",
            tool_title="bash pytest -q", duration_ms=1200)
        await db.add_message(
            session["id"], "tool", "edited", tool_call_id="seed-edit", tool_name="edit",
            tool_title="edit agent_server/agent.py", diff=DIFF, lang="python",
            duration_ms=800)
        await db.add_message(session["id"], "assistant", "That is done.",
                             reasoning_content="I considered several options.\nThen chose one.")
        return session["id"]
    finally:
        await db.close()
        db.DB_PATH = original


@pytest.fixture(scope="module")
def live_ui(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("live-data")
    session_id = asyncio.run(_seed(data_dir))
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
async def page(live_ui):
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no Playwright browser available: {exc}")
        pg = await browser.new_page(viewport=VIEWPORT)
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await pg.goto(live_ui, wait_until="networkidle")
        await pg.wait_for_selector("#messages .message")
        # The configuration the design targets: past thinking and past tool
        # calls hidden, nothing opening itself.
        await pg.evaluate("""
        () => { App.expandTools = []; App.hideToolCalls = true; App.hideThinking = true;
                window._s = { assistantEl: null, contentEl: null, text: '', reasoningEl: null };
                const c = document.getElementById('chat-container');
                c.scrollTop = c.scrollHeight; }
        """)
        await pg.wait_for_timeout(150)
        pg._console_errors = errors
        try:
            yield pg
        finally:
            await browser.close()


async def _fire(pg, event: str) -> None:
    await pg.evaluate(f"() => handleEvent({event}, window._s)")
    await pg.wait_for_timeout(140)


async def _glyph_left(pg, selector):
    """Where the first character actually lands, not where its box starts.

    These differ, and the difference is the bug. An expanded block is an inset
    box: border, padding and a line-number gutter all sit between its left edge
    and its first character. Aligning the boxes is not the same as aligning the
    text, and it is the text the eye follows down the page -- so the boxes are
    pulled left by their own inset and it is the glyphs that must line up.

    Skips hidden rows: with past tool calls hidden, a plain `querySelector`
    finds one of those first and measures a rect of zero size.
    """
    return await pg.evaluate("""
    (sel) => {
      for (const el of document.querySelectorAll(sel)) {
        if (el.closest('[hidden]')) continue;
        const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        let n;
        while ((n = w.nextNode())) {
          if (!n.textContent.trim()) continue;
          const r = document.createRange();
          r.selectNodeContents(n);
          const b = r.getBoundingClientRect();
          if (b.width || b.height) return Math.round(b.left);
        }
      }
      return null;
    }
    """, selector)


# ── One height for every transient row ───────────────────────────────────────

async def test_every_transient_row_is_the_same_height(page):
    """The live line, the progress counter, a running call and a one-line
    thinking block replace each other constantly. They measured 28/31/30/40px,
    so each swap shoved the transcript by up to 12px, several times a second."""
    heights: dict[str, float] = {}

    await page.evaluate("() => showStatus('Waiting for the model')")
    await page.wait_for_timeout(140)
    heights["live line"] = await page.evaluate(
        "() => document.querySelector('.message.status-line').getBoundingClientRect().height")

    await _fire(page, "{ type: 'tool_progress', calls: [{ name: 'bash', chars: 2048 }] }")
    heights["progress counter"] = await page.evaluate(
        "() => document.querySelector('.message.tool-progress').getBoundingClientRect().height")

    await _fire(page, "{ type: 'tool_start', tool_call_id: 'b1', name: 'bash',"
                      " args: { command: 'pytest -q' } }")
    heights["running call"] = await page.evaluate(
        "() => document.querySelector('.message.tool[data-tool-call-id=\"b1\"]')"
        ".getBoundingClientRect().height")

    await _fire(page, "{ type: 'reasoning', text: 'Thinking about it.' }")
    heights["thinking, one line"] = await page.evaluate(
        "() => document.querySelector('.message.thinking').getBoundingClientRect().height")

    spread = max(heights.values()) - min(heights.values())
    assert spread <= TOLERANCE, f"transient rows differ in height: {heights}"


async def test_a_call_finishing_does_not_change_its_row_height(page):
    """The spinner dot was removed when the call ended, taking 12px of row with
    it."""
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'r1', name: 'read',"
                      " args: { path: 'agent_server/agent.py' } }")
    running = await page.evaluate(
        "() => document.querySelector('.message.tool[data-tool-call-id=\"r1\"]')"
        ".getBoundingClientRect().height")
    await _fire(page, "{ type: 'tool_end', tool_call_id: 'r1',"
                      " title: 'read agent.py (400 lines)', output: 'ok' }")
    finished = await page.evaluate(
        "() => document.querySelector('.message.tool[data-tool-call-id=\"r1\"]')"
        ".getBoundingClientRect().height")
    assert abs(running - finished) <= TOLERANCE, (
        f"the row changed height when the call finished: {running} -> {finished}")


# ── One left edge for every row ──────────────────────────────────────────────

async def test_every_one_line_row_starts_its_text_at_the_same_left_edge(page):
    """Tool labels hung 12px right of the assistant text because the spinner was
    a flow item with a gap after it. The marker sits at the right-hand end now,
    beside the elapsed time, where there is nothing for it to push or collide
    with -- see the note on .spinner-dot.

    One-line rows only. An expanded block is a box and insets its contents, the
    same as any code block on the page; two attempts to change that made things
    worse and are described in style.css.
    """
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'g1', name: 'bash',"
                      " args: { command: 'pytest -q' } }")
    await _fire(page, "{ type: 'reasoning', text: 'Considering it.' }")
    await page.evaluate("() => showStatus('Waiting for the model')")
    await page.wait_for_timeout(140)

    await page.evaluate(
        "() => document.querySelectorAll('#messages .reasoning-details')"
        ".forEach(d => { d.open = false; })")
    await page.wait_for_timeout(150)
    lefts = {
        "assistant": await _glyph_left(page, ".message.assistant .content-text"),
        "toolLabel": await _glyph_left(page, ".message.tool .tool-label"),
        "thinking": await _glyph_left(page, ".message.thinking .reasoning-summary"),
        "liveLine": await _glyph_left(page, ".message.status-line .status-text"),
    }
    assert all(v is not None for v in lefts.values()), lefts
    spread = max(lefts.values()) - min(lefts.values())
    assert spread <= TOLERANCE, f"rows start their text at different x: {lefts}"


async def test_finishing_a_call_does_not_move_its_label_sideways(page):
    await _fire(page, "{ type: 'tool_start', tool_call_id: 's1', name: 'bash',"
                      " args: { command: 'ls' } }")
    before = await page.evaluate(
        "() => document.querySelector('.message.tool[data-tool-call-id=\"s1\"] .tool-label')"
        ".getBoundingClientRect().left")
    await _fire(page, "{ type: 'tool_end', tool_call_id: 's1', title: 'bash ls', output: 'a' }")
    after = await page.evaluate(
        "() => document.querySelector('.message.tool[data-tool-call-id=\"s1\"] .tool-label')"
        ".getBoundingClientRect().left")
    assert abs(before - after) <= TOLERANCE, f"the label moved {after - before}px sideways"


# ── One block holds the overlay ──────────────────────────────────────────────

async def test_a_streaming_thinking_block_does_not_cover_the_block_above_it(page):
    """The reported fault, measured.

    An auto-expanded `edit` took the overlay; the model then started thinking
    and the thinking block took one too. Both are positioned from their own
    row's top, 30px apart, and both grow to the same height -- so the thinking
    block painted over 86% of the diff, which stayed open underneath it.
    """
    await page.evaluate("() => { App.expandTools = ['write', 'edit']; }")
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'e1', name: 'edit',"
                      " args: { path: 'agent_server/permissions.py' } }")
    await page.evaluate("(d) => handleEvent({ type: 'tool_end', tool_call_id: 'e1',"
                        " title: 'edit permissions.py', diff: d, lang: 'python' }, window._s)", DIFF)
    await page.wait_for_timeout(250)

    for i in range(10):
        await page.evaluate("(t) => handleEvent({ type: 'reasoning', text: t }, window._s)",
                            f"Considering approach {i}, at some length.\n")
        await page.wait_for_timeout(40)
    await page.wait_for_timeout(250)

    boxes = await page.evaluate("""
    () => {
      const r = (sel) => { const n = document.querySelector(sel);
                           const b = n.getBoundingClientRect();
                           return { top: b.top, bottom: b.bottom, height: b.height }; };
      const edit = document.querySelector('.message.tool[data-tool-call-id="e1"]');
      return { edit: r('.message.tool[data-tool-call-id="e1"] .msg-content'),
               thinking: r('.message.thinking .msg-content'),
               editLive: edit.classList.contains('live'),
               editOpen: !!edit.querySelector('details[open]'),
               thinkingLive: document.querySelector('.message.thinking').classList.contains('live') };
    }
    """)
    overlap = (min(boxes["edit"]["bottom"], boxes["thinking"]["bottom"])
               - max(boxes["edit"]["top"], boxes["thinking"]["top"]))
    assert overlap <= TOLERANCE, (
        f"the thinking block covers {overlap:.0f}px of the block above it")
    assert boxes["thinkingLive"], "the streaming block should hold the overlay"
    assert not boxes["editLive"], "only one block may hold the overlay"
    # The diff stays open: `edit` was asked to expand, and a finished result now
    # lives in the flow rather than being propped up out of it. Open is fine.
    # Open *and painted over* was the bug, and that is the assertion above.
    assert boxes["editOpen"]
    assert boxes["thinking"]["top"] >= boxes["edit"]["bottom"] - TOLERANCE, (
        "the thinking block should begin below the diff, not on top of it")


async def test_only_one_block_holds_the_overlay_however_many_compete(page):
    await page.evaluate("() => { App.expandTools = ['write', 'edit', 'bash']; }")
    for n in range(3):
        await _fire(page, f"{{ type: 'tool_start', tool_call_id: 'm{n}', name: 'bash',"
                          f" args: {{ command: 'echo {n}' }} }}")
        await _fire(page, f"{{ type: 'tool_output', tool_call_id: 'm{n}', text: 'line {n}' }}")
    await _fire(page, "{ type: 'reasoning', text: 'And now thinking.' }")
    holders = await page.evaluate(
        "() => [...document.querySelectorAll('.message.live')].map(n => n.className)")
    assert len(holders) == 1, f"{len(holders)} blocks hold the overlay at once: {holders}"


# ── The live line never leaves mid-turn ──────────────────────────────────────

async def test_the_live_line_survives_every_event_of_a_turn(page):
    """It used to be removed by every event and restored by only a few, so the
    foot lost a row and got it back between phases."""
    await _fire(page, "{ type: 'turn_start', user_message_id: null }")
    sequence = [
        "{ type: 'working' }",
        "{ type: 'reasoning', text: 'Hmm.' }",
        "{ type: 'tool_start', tool_call_id: 'k1', name: 'bash', args: { command: 'ls' } }",
        "{ type: 'tool_output', tool_call_id: 'k1', text: 'a\\nb' }",
        "{ type: 'tool_end', tool_call_id: 'k1', title: 'bash ls', output: 'a\\nb' }",
        "{ type: 'usage', prompt_tokens: 10 }",
        "{ type: 'working' }",
    ]
    for event in sequence:
        await _fire(page, event)
        present = await page.evaluate(
            "() => document.querySelectorAll('.message.status-line').length")
        assert present == 1, f"the live line vanished after {event}"


async def test_the_live_line_keeps_its_height_when_it_has_nothing_to_say(page):
    """Blanked, not removed -- the reserved slot is the point."""
    await _fire(page, "{ type: 'turn_start', user_message_id: null }")
    talking = await page.evaluate(
        "() => document.querySelector('.message.status-line').getBoundingClientRect().height")
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'q1', name: 'bash',"
                      " args: { command: 'sleep 1' } }")
    quiet = await page.evaluate("""
    () => { const n = document.querySelector('.message.status-line');
            return { height: n.getBoundingClientRect().height,
                     text: n.querySelector('.status-text').textContent,
                     idle: n.classList.contains('idle') }; }
    """)
    assert quiet["idle"] and quiet["text"] == "", "a claimed slot should say nothing"
    assert abs(quiet["height"] - talking) <= TOLERANCE, (
        f"the live line changed height when it went quiet: {talking} -> {quiet['height']}")


async def test_the_live_line_goes_when_the_turn_does(page):
    await _fire(page, "{ type: 'turn_start', user_message_id: null }")
    assert await page.evaluate("() => !!document.querySelector('.message.status-line')")
    await _fire(page, "{ type: 'done', changes: null }")
    assert await page.evaluate("() => !document.querySelector('.message.status-line')"), (
        "the live line outlived the turn")


# ── Streaming starts at the top ──────────────────────────────────────────────

async def test_a_streaming_thinking_block_shows_its_beginning(page):
    """Thinking arrives faster than anyone reads it. Chasing the newest token
    showed a blur with the block's own first line scrolled off the top."""
    for i in range(30):
        await page.evaluate("(t) => handleEvent({ type: 'reasoning', text: t }, window._s)",
                            f"Thought number {i}.\n")
    await page.wait_for_timeout(300)
    shown = await page.evaluate("""
    () => { const box = document.querySelector('.message.thinking .msg-content');
            const sum = document.querySelector('.message.thinking .reasoning-summary');
            return { boxScroll: box.scrollTop, sumScroll: sum.scrollTop,
                     first: sum.textContent.split('\\n')[0] }; }
    """)
    assert shown["first"] == "Thought number 0.", "the block is not showing its beginning"
    assert shown["boxScroll"] == 0 and shown["sumScroll"] == 0, (
        "the thinking block scrolled away from the top on its own")


async def test_the_page_raises_no_console_errors_driving_all_of_this(page):
    await _fire(page, "{ type: 'turn_start', user_message_id: null }")
    await _fire(page, "{ type: 'reasoning', text: 'Thinking.' }")
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'z1', name: 'bash',"
                      " args: { command: 'ls' } }")
    await _fire(page, "{ type: 'tool_end', tool_call_id: 'z1', title: 'bash ls', output: 'a' }")
    await _fire(page, "{ type: 'done', changes: null }")
    assert page._console_errors == []


# ── Expanded blocks share the left edge too ──────────────────────────────────

async def test_expanded_blocks_start_their_text_on_the_margin(page):
    """Every block's first character lands where the prose does.

    Two earlier attempts at this failed and are worth not repeating. A negative
    margin alone was clipped: a streaming block sits in `.msg-content`, which
    has `overflow-y: auto`, and CSS computes the other axis to `auto` as soon as
    one is not `visible`. Stripping the padding instead left diff line numbers
    jammed against the edge of their own background.

    What works is both together -- the block keeps its padding and reaches back
    into the gutter by exactly that much, and the overlay is shifted by the same
    amount so there is room inside it to reach into.
    """
    await page.evaluate("() => { App.expandTools = ['bash', 'edit']; }")
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'x1', name: 'bash',"
                      " args: { command: 'pytest -q' } }")
    await _fire(page, "{ type: 'tool_end', tool_call_id: 'x1', title: 'bash pytest',"
                      " output: 'collected 40 items\\nall passed' }")
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'x2', name: 'edit',"
                      " args: { path: 'a.py' } }")
    await page.evaluate("(d) => handleEvent({ type: 'tool_end', tool_call_id: 'x2',"
                        " title: 'edit a.py', diff: d, lang: 'python' }, window._s)", DIFF)
    await page.wait_for_timeout(250)
    for i in range(6):
        await page.evaluate("(t) => handleEvent({ type: 'reasoning', text: t }, window._s)",
                            f"Thinking line {i}.\n")
    await page.wait_for_timeout(300)

    async def edges():
        return {
            "prose": await _glyph_left(page, ".message.assistant .content-text"),
            "tool output": await _glyph_left(page, ".message.tool .tool-raw"),
            "diff": await _glyph_left(page, ".message.tool .diff-block"),
            "thinking": await _glyph_left(page, ".message.thinking .reasoning-summary"),
        }

    # While the thinking block is still streaming it is drawn out of the flow,
    # which is the case the earlier attempts got wrong, so measure it twice.
    live = {k: v for k, v in (await edges()).items() if v is not None}
    assert len(live) >= 3, f"not enough blocks rendered to compare: {live}"
    assert max(live.values()) - min(live.values()) <= TOLERANCE, (
        f"blocks do not share a margin while one is streaming: {live}")

    await page.evaluate(
        "() => document.querySelectorAll('.message.live').forEach(n => n.classList.remove('live'))")
    await page.wait_for_timeout(250)
    settled = {k: v for k, v in (await edges()).items() if v is not None}
    assert max(settled.values()) - min(settled.values()) <= TOLERANCE, (
        f"blocks do not share a margin once settled: {settled}")
    assert settled == live, (
        f"a block moved when it stopped streaming: {live} -> {settled}")


# ── The clocks ───────────────────────────────────────────────────────────────

async def test_the_live_line_shows_no_clock_while_something_else_is_running(page):
    """Two elapsed times, counting from different moments and disagreeing by a
    few seconds, next to each other. The row stays for its height; its clock
    does not. Clearing it once was not enough -- the interval fires every second
    and wrote it straight back."""
    await _fire(page, "{ type: 'turn_start', user_message_id: null }")
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'c1', name: 'bash',"
                      " args: { command: 'sleep 30' } }")
    # Long enough for the interval to fire several times.
    await page.wait_for_timeout(3400)
    state = await page.evaluate("""
    () => { const s = document.querySelector('.message.status-line');
            return { idle: s.classList.contains('idle'),
                     elapsed: s.querySelector('.status-elapsed').textContent,
                     text: s.querySelector('.status-text').textContent }; }
    """)
    assert state["idle"], "a running call should own the slot"
    assert state["elapsed"] == "", (
        f"the live line is running a second clock: {state['elapsed']!r}")
    assert state["text"] == ""


async def test_the_animated_dots_do_not_move_the_elapsed_time(page):
    """They animate by swapping their own content between '', '.', '..' and
    '...', which changed the width of the line four times a second and made
    whatever followed dance left and right."""
    await _fire(page, "{ type: 'turn_start', user_message_id: null }")
    await page.wait_for_timeout(2200)  # past the 2s threshold, so it has a value
    positions = []
    for _ in range(12):
        positions.append(await page.evaluate("""
        () => { const e = document.querySelector('.status-elapsed');
                return Math.round(e.getBoundingClientRect().left * 100) / 100; }
        """))
        await page.wait_for_timeout(160)
    spread = max(positions) - min(positions)
    assert spread <= TOLERANCE, (
        f"the elapsed time moved {spread:.1f}px across the dot animation: {sorted(set(positions))}")


async def test_an_elapsed_time_never_wraps_onto_a_second_line(page):
    """"12.4s" is one word. Wrapped, the `s` dropped to a line of its own and
    made the row two lines tall, moving everything below it."""
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'w1', name: 'bash',"
                      " args: { command: 'a command with a long enough name to crowd the row' } }")
    await page.wait_for_timeout(2400)
    lines = await page.evaluate("""
    () => { const e = document.querySelector('.message.tool[data-tool-call-id="w1"] .tool-elapsed');
            return { rects: e.getClientRects().length, text: e.textContent,
                     rowHeight: e.closest('.message').getBoundingClientRect().height }; }
    """)
    assert lines["rects"] == 1, f"the elapsed time wrapped: {lines}"
    assert lines["rowHeight"] <= 34, f"the row grew past one line: {lines}"


# ── The markup the server renders, which is what a reload shows ──────────────

async def test_server_rendered_rows_line_up_with_the_prose(page):
    """Everything above drives `handleEvent`, so it only ever measures the
    markup app.js builds. `chat_messages.html` is a separate template rendering
    the same blocks, and after a refresh it is the one on screen -- so it gets
    the same measurement, or a fix can pass here and be invisible in the app.
    """
    # The fixture hides past calls, which is the right default and the wrong
    # thing here: a hidden row has no box to measure.
    await page.evaluate("""
    () => { App.hideToolCalls = false;
            document.querySelectorAll('#messages .message').forEach(m => { m.hidden = false; });
            document.querySelectorAll('#messages details.tool-details')
                    .forEach(d => { d.open = true; });
            document.querySelectorAll('#messages .reasoning-details')
                    .forEach(d => { d.open = true; }); }
    """)
    await page.wait_for_timeout(250)
    prose = await _glyph_left(page, ".message.assistant .content-text")
    label = await _glyph_left(page, ".message.tool .tool-label")
    assert prose is not None and label is not None, "the seeded transcript did not render"
    assert abs(label - prose) <= TOLERANCE, (
        f"a server-rendered tool label sits {label - prose}px off the prose")

    # And its boxes start on the same margin, like the streamed ones.
    boxes = {
        "tool output": await _glyph_left(page, ".message.tool .tool-raw"),
        # The block's first glyph is the line number. Its *code* sits further
        # right by the width of the number gutter, which is the point of one.
        "diff": await _glyph_left(page, ".message.tool .diff-block"),
    }
    present = {k: v for k, v in boxes.items() if v is not None}
    assert present, "no expanded block rendered"
    off = {k: v - prose for k, v in present.items()}
    assert all(abs(v) <= TOLERANCE for v in off.values()), (
        f"server-rendered blocks are off the margin: {off}")


async def test_a_server_rendered_call_is_the_same_height_as_a_streamed_one(page):
    await page.evaluate("() => { App.hideToolCalls = false; }")
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'cmp', name: 'bash',"
                      " args: { command: 'ls' } }")
    await _fire(page, "{ type: 'tool_end', tool_call_id: 'cmp', title: 'bash ls', output: 'a' }")
    await page.evaluate(
        "() => document.querySelectorAll('#messages .message').forEach(m => { m.hidden = false; })")
    await page.wait_for_timeout(150)
    heights = await page.evaluate("""
    () => {
      const h = (sel) => { const n = document.querySelector(sel);
                           return n ? n.getBoundingClientRect().height : null; };
      return { streamed: h('.message.tool[data-tool-call-id="cmp"]'),
               rendered: h('.message.tool[data-tool-call-id="seed-bash"]') };
    }
    """)
    assert heights["rendered"] is not None, "the seeded call did not render"
    assert abs(heights["streamed"] - heights["rendered"]) <= TOLERANCE, (
        f"the two templates disagree on row height: {heights}")


# ── What a reload restores ───────────────────────────────────────────────────

async def test_a_reattached_call_carries_on_the_servers_clock(page):
    """The elapsed figure was counted from when the row was drawn, so after a
    refresh three subagents that had been working for minutes all read "5.0s"
    five seconds later. The server says how long it has really been."""
    await _fire(page, "{ type: 'attached', inflight: [{ tool_call_id: 'far',"
                      " name: 'task', args: { description: 'corpora' },"
                      " elapsed_ms: 92000 }], queued: [] }")
    await page.wait_for_timeout(400)
    shown = await page.evaluate(
        "() => document.querySelector('.message.tool[data-tool-call-id=\"far\"]"
        " .tool-elapsed').textContent")
    seconds = float(shown.rstrip("s"))
    assert 91 <= seconds <= 96, (
        f"a call the server says is 92s old is showing {shown} -- the page is "
        "timing it from the reload")


async def test_reattaching_does_not_reset_a_clock_that_is_already_right(page):
    await _fire(page, "{ type: 'attached', inflight: [{ tool_call_id: 'dup',"
                      " name: 'bash', args: { command: 'ls' }, elapsed_ms: 40000 }],"
                      " queued: [] }")
    await page.wait_for_timeout(300)
    first = await page.evaluate(
        "() => document.querySelector('.message.tool[data-tool-call-id=\"dup\"]"
        " .tool-elapsed').textContent")
    await _fire(page, "{ type: 'attached', inflight: [{ tool_call_id: 'dup',"
                      " name: 'bash', args: { command: 'ls' }, elapsed_ms: 40000 }],"
                      " queued: [] }")
    await page.wait_for_timeout(300)
    rows = await page.evaluate(
        "() => document.querySelectorAll('.message.tool[data-tool-call-id=\"dup\"]').length")
    assert rows == 1, "reattaching drew the call twice"
    assert float(first.rstrip("s")) >= 39


async def test_a_queued_message_comes_back_after_a_reload(page):
    """It lives on the server until the turn can take it. The page held the only
    copy, so a refresh looked like it had thrown the message away."""
    await _fire(page, "{ type: 'attached', inflight: [], queued: ["
                      "{ id: 'q9', content: 'Also make them public domain.' }] }")
    await page.wait_for_timeout(300)
    shown = await page.evaluate("""
    () => { const n = document.querySelector('.message.user.queued[data-queue-id="q9"]');
            if (!n) return null;
            return { text: n.innerText,
                     undo: !!n.querySelector('.msg-actions button'),
                     visible: n.getBoundingClientRect().height > 0 }; }
    """)
    assert shown, "the queued message was not restored"
    assert "public domain" in shown["text"]
    assert shown["visible"]
    assert shown["undo"], "a restored queued message must still be cancellable"


async def test_a_restored_queued_message_is_not_duplicated(page):
    event = ("{ type: 'attached', inflight: [], queued: ["
             "{ id: 'q7', content: 'Twice?' }] }")
    await _fire(page, event)
    await _fire(page, event)
    await page.wait_for_timeout(200)
    count = await page.evaluate(
        "() => document.querySelectorAll('.message.user.queued[data-queue-id=\"q7\"]').length")
    assert count == 1, f"the queued message was drawn {count} times"


async def test_a_queued_message_is_not_painted_over_by_a_streaming_block(page):
    """An overlay paints over every row that follows it. You type while the
    agent is working, the bubble is added below the live block, and it is simply
    not there."""
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'ov', name: 'task',"
                      " args: { description: 'corpora' } }")
    await _fire(page, "{ type: 'tool_output', tool_call_id: 'ov',"
                      " text: 'searching\\nfound 3 candidates\\nreading them' }")
    await _fire(page, "{ type: 'attached', inflight: [], queued: ["
                      "{ id: 'q5', content: 'And public domain only, please.' }] }")
    await page.wait_for_timeout(400)

    clash = await page.evaluate("""
    () => {
      const q = document.querySelector('.message.user.queued[data-queue-id="q5"]');
      const live = document.querySelector('.message.live .msg-content');
      if (!q) return { missing: true };
      const qr = q.getBoundingClientRect();
      if (!live) return { overlap: 0, height: qr.height };
      const lr = live.getBoundingClientRect();
      const y = Math.min(qr.bottom, lr.bottom) - Math.max(qr.top, lr.top);
      const x = Math.min(qr.right, lr.right) - Math.max(qr.left, lr.left);
      return { overlap: (x > 1 && y > 1) ? Math.round(y) : 0, height: qr.height };
    }
    """)
    assert not clash.get("missing"), "the queued message never rendered"
    assert clash["height"] > 0, "the queued message has no height"
    assert clash["overlap"] == 0, (
        f"a streaming block is painted over {clash['overlap']}px of the queued message")


# ── The two mistakes that shipped ────────────────────────────────────────────
#
# Both were introduced by fixes here and both were reported from a real session
# rather than caught by anything. They are cheap to check and neither had a test.

async def _busy_transcript(pg):
    """A row of every kind, in the states they are actually seen in."""
    await pg.evaluate("() => { App.expandTools = ['bash', 'edit']; App.hideToolCalls = false; }")
    await _fire(pg, "{ type: 'turn_start', user_message_id: null }")
    await _fire(pg, "{ type: 'tool_start', tool_call_id: 'k1', name: 'bash',"
                    " args: { command: 'ruff check .' } }")
    await _fire(pg, "{ type: 'tool_end', tool_call_id: 'k1', title: 'bash ruff check .',"
                    " output: 'All checks passed!' }")
    await _fire(pg, "{ type: 'tool_start', tool_call_id: 'k2', name: 'read',"
                    " args: { path: 'agent_server/agent.py' } }")
    for i in range(5):
        await pg.evaluate("(t) => handleEvent({ type: 'reasoning', text: t }, window._s)",
                          f"Considering option {i}.\n")
    await pg.wait_for_timeout(350)


async def test_nothing_is_cut_off_by_its_own_scroll_box(page):
    """A streaming block is drawn in `.msg-content`, which has `overflow-y:
    auto` -- and CSS computes the other axis to `auto` as soon as one is not
    `visible`. So the box has a hard left edge, and a block given a negative
    margin to pull it into the gutter was silently sliced down its left side.
    """
    await _busy_transcript(page)
    clipped = await page.evaluate("""
    () => {
      const bad = [];
      for (const box of document.querySelectorAll('#messages .msg-content, #messages .tool-raw')) {
        if (getComputedStyle(box).overflowX === 'visible') continue;
        const bb = box.getBoundingClientRect();
        for (const child of box.querySelectorAll('*')) {
          const cr = child.getBoundingClientRect();
          if (!cr.width || !cr.height) continue;
          if (cr.left < bb.left - 0.5)
            bad.push({ el: String(child.className) || child.tagName,
                       cutBy: Math.round(bb.left - cr.left) });
        }
      }
      return bad;
    }
    """)
    assert clipped == [], f"cut off at the left edge of a scroll box: {clipped}"


async def test_the_activity_marker_touches_nothing(page):
    """At the full gutter offset the dot overlapped the role label, which fades
    in on hover. Moved in to clear it, it sat 2px from the tool's own name and
    read as jammed against it. It lives at the right-hand end now."""
    await _busy_transcript(page)
    await page.hover("#messages .message.tool[data-tool-call-id='k2'] .tool-summary")
    await page.wait_for_timeout(250)

    crowding = await page.evaluate("""
    () => {
      const out = [];
      for (const dot of document.querySelectorAll('#messages .spinner-dot')) {
        if (getComputedStyle(dot).visibility === 'hidden') continue;
        const dr = dot.getBoundingClientRect();
        if (!dr.width) continue;
        const row = dot.closest('.message');
        for (const other of row.querySelectorAll('.msg-role, .tool-label, .status-text,'
                                                 + ' .tool-elapsed, .status-elapsed')) {
          const r = other.getBoundingClientRect();
          if (!r.width || !r.height) continue;
          if (getComputedStyle(other).opacity === '0') continue;
          const vertical = Math.min(dr.bottom, r.bottom) - Math.max(dr.top, r.top);
          if (vertical <= 0) continue;
          const gap = dr.left >= r.right ? dr.left - r.right
                    : r.left >= dr.right ? r.left - dr.right
                    : -1;                                  // overlapping outright
          if (gap < 4) out.push({ neighbour: String(other.className), gap: Math.round(gap) });
        }
      }
      return out;
    }
    """)
    assert crowding == [], f"the marker is crowding its neighbours: {crowding}"


async def test_the_tool_label_owns_the_left_edge_of_its_row(page):
    """Whatever else a row carries, the thing you read starts at the margin."""
    await _busy_transcript(page)
    order = await page.evaluate("""
    () => {
      const s = document.querySelector(
        "#messages .message.tool[data-tool-call-id='k2'] .tool-summary");
      const kids = [...s.children].filter(c => c.getBoundingClientRect().width > 0);
      return kids.map(c => ({ cls: String(c.className),
                              left: Math.round(c.getBoundingClientRect().left) }));
    }
    """)
    assert order, "the running call has no summary contents"
    assert "tool-label" in order[0]["cls"], (
        f"something sits left of the label: {order}")


async def test_the_timestamp_stays_out_of_the_content_column(page):
    """A streaming block sets `.msg-content` to `position: absolute`, which
    takes it out of the grid -- and auto-placement then slid `.msg-time` from
    the third column into the second, where it sat at the content's left edge
    behind the overlay. Hovering revealed the sliver of it that cleared the
    overlay: a lone digit of the timestamp against the left margin.
    """
    await _fire(page, "{ type: 'reasoning', text: 'Thinking about it at length.' }")
    await page.wait_for_timeout(250)
    row = ".message.thinking"
    assert await page.evaluate(f"() => document.querySelector('{row}')"
                               ".classList.contains('live')"), "expected a live block"
    await page.hover(f"{row} .reasoning-summary")
    await page.wait_for_timeout(250)

    placement = await page.evaluate("""
    () => {
      const row = document.querySelector('.message.thinking');
      const time = row.querySelector(':scope > .msg-time');
      const content = row.querySelector(':scope > .msg-content');
      if (!time) return { missing: true };
      const t = time.getBoundingClientRect(), c = content.getBoundingClientRect();
      const overlapX = Math.min(t.right, c.right) - Math.max(t.left, c.left);
      const overlapY = Math.min(t.bottom, c.bottom) - Math.max(t.top, c.top);
      return { timeLeft: Math.round(t.left), contentLeft: Math.round(c.left),
               overlaps: overlapX > 1 && overlapY > 1, text: time.textContent.trim() };
    }
    """)
    assert not placement.get("missing"), "the row lost its timestamp"
    assert not placement["overlaps"], (
        f"the timestamp is sitting on top of the content: {placement}")
    assert placement["timeLeft"] > placement["contentLeft"], (
        f"the timestamp is left of the content it belongs beside: {placement}")


async def test_a_streaming_block_is_not_pushed_out_of_place(page):
    """The fix for the timestamp was first written as an explicit `grid-column`
    on every child. That breaks the overlay: an absolutely positioned grid child
    with a definite placement is positioned against its *grid area*, not the
    grid container, so `left` began counting from the content column and every
    streaming block jumped 60px right."""
    await _fire(page, "{ type: 'reasoning', text: 'Weighing it up.' }")
    await page.wait_for_timeout(250)
    live = await _glyph_left(page, ".message.thinking .reasoning-summary")
    prose = await _glyph_left(page, ".message.assistant .content-text")
    assert live is not None and prose is not None
    assert abs(live - prose) <= TOLERANCE, (
        f"a streaming block sits {live - prose}px off the margin")
