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

async def test_every_row_starts_its_text_at_the_same_left_edge(page):
    """Tool labels hung 12px right of the assistant text because the spinner was
    a flow item with a gap after it. The marker belongs in the gutter."""
    await _fire(page, "{ type: 'tool_start', tool_call_id: 'g1', name: 'bash',"
                      " args: { command: 'pytest -q' } }")
    await _fire(page, "{ type: 'reasoning', text: 'Considering it.' }")
    await page.evaluate("() => showStatus('Waiting for the model')")
    await page.wait_for_timeout(140)

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

async def test_expanded_blocks_line_up_with_the_prose(page):
    """Measured on a live session before the fix: prose 122, tool output 134,
    thinking 135. A transcript of alternating sentences and tool output read
    with a ragged left margin."""
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

    edges = {
        "prose": await _glyph_left(page, ".message.assistant .content-text"),
        "tool summary": await _glyph_left(page, ".message.tool .tool-summary"),
        "tool output": await _glyph_left(page, ".message.tool .tool-raw"),
        "diff": await _glyph_left(page, ".message.tool .diff-block"),
        "thinking": await _glyph_left(page, ".message.thinking .reasoning-summary"),
    }
    present = {k: v for k, v in edges.items() if v is not None}
    assert len(present) >= 4, f"not enough blocks rendered to compare: {edges}"
    spread = max(present.values()) - min(present.values())
    assert spread <= TOLERANCE, f"blocks start their text at different x: {present}"


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
    edges = {
        "prose": await _glyph_left(page, ".message.assistant .content-text"),
        "tool label": await _glyph_left(page, ".message.tool .tool-label"),
        "tool output": await _glyph_left(page, ".message.tool .tool-raw"),
        # The block's first glyph is the line number. Its *code* sits further
        # right by the width of the number gutter, which is the point of a
        # gutter -- so it is the numbers that line up with the prose.
        "diff": await _glyph_left(page, ".message.tool .diff-block"),
        "thinking": await _glyph_left(page, ".message .reasoning-summary"),
    }
    present = {k: v for k, v in edges.items() if v is not None}
    assert len(present) >= 4, f"the seeded transcript did not render: {edges}"
    spread = max(present.values()) - min(present.values())
    assert spread <= TOLERANCE, f"server-rendered blocks are ragged: {present}"


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
