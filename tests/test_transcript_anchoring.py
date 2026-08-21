"""The transcript must not move under the reader.

These run in a real browser because the thing under test is a property of the
rendered layout, and no amount of backend testing can see it: the server cannot
tell you whether a header stayed where it was when the block beneath it opened.

Every assertion here is a *number* -- a y coordinate before an action and after
it. None of them look at wording, colour, spacing, or where anything sits on the
page, so restyling and rewriting the UI cannot break them. The only thing that
can is the bug coming back, which is the whole point.

They need a browser. If Playwright's Chromium is not installed the module skips
rather than fails, so a fresh checkout still gets a green suite.
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

REPO = Path(__file__).resolve().parent.parent
VIEWPORT = {"width": 1400, "height": 950}

# Enough output that an expanded block is unmistakably taller than a collapsed
# one; the exact height is never asserted, only that it did not move anything.
TOOL_OUTPUT = "\n".join(f"output line {n}" for n in range(1, 60))
DIFF = "\n".join(f"-old line {i}\n+new line {i}" for i in range(1, 30))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _seed(data_dir: Path) -> str:
    """A transcript longer than the window, so the scroller really scrolls."""
    from agent_server import database as db

    original = db.DB_PATH
    db.DB_PATH = data_dir / "agent.db"
    try:
        await db.init_db()
        session = await db.create_session(name="anchoring", project_dir=str(REPO))
        await db.add_message(session["id"], "user", "Audit the file routes.")
        # Past the transcript window on purpose, so these run against a
        # transcript that is drawn in a window with a "show earlier" control --
        # which is what a real long session looks like.
        for i in range(40):
            await db.add_message(
                session["id"], "tool", TOOL_OUTPUT,
                tool_call_id=f"c{i}", tool_name=["read", "grep", "bash"][i % 3],
                tool_title=f"read agent_server/routes/part_{i}.py ({40 + i} lines)",
                duration_ms=900 + i,
            )
            await db.add_message(session["id"], "assistant", f"Checked part {i}.")
        return session["id"]
    finally:
        await db.close()
        db.DB_PATH = original


@pytest.fixture(scope="module")
def live_ui(tmp_path_factory):
    """A real server with a real transcript, and the session id to open."""
    data_dir = tmp_path_factory.mktemp("ui-data")
    session_id = asyncio.run(_seed(data_dir))
    port = _free_port()
    env = {**os.environ, "CODEAGENT_DATA_DIR": str(data_dir), "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent_server.main:app", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/sessions/{session_id}"
    try:
        _wait_for(port, proc)
        yield url
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


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


@pytest.fixture
async def page(live_ui):
    """A page with the transcript loaded and scrolled to the bottom.

    The bottom is where a jump is worst -- it is where the reader is watching --
    so every test starts from there unless it says otherwise.
    """
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:  # any launch failure means there is no browser to use
            pytest.skip(f"no Playwright browser available: {exc}")
        errors: list[str] = []
        pg = await browser.new_page(viewport=VIEWPORT)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await pg.goto(live_ui, wait_until="networkidle")
        await pg.wait_for_selector("#messages details.tool-details")
        await _to_bottom(pg)
        pg._console_errors = errors
        try:
            yield pg
        finally:
            await browser.close()


async def _to_bottom(pg) -> None:
    await pg.evaluate("() => { const b = document.getElementById('chat-container');"
                      " b.scrollTop = b.scrollHeight; }")
    await pg.wait_for_timeout(150)


async def _tops(pg) -> list[float]:
    """Every visible row's on-screen y. What the eye would notice, in numbers."""
    return await pg.evaluate("""
    () => [...document.querySelectorAll('#messages .message')]
            .filter(r => !r.hidden)
            .map(r => Math.round(r.getBoundingClientRect().top * 100) / 100)
    """)


async def _onscreen_tops(pg) -> dict[str, float]:
    """Only the rows the reader can actually see, keyed so the pairing survives
    rows entering or leaving the viewport.

    This distinction matters. When a block *above* the viewport grows, the
    browser adds the same amount to scrollTop to hold the visible content
    still -- which means every row above the growth legitimately shifts in
    viewport coordinates while nothing on screen moves at all. Comparing rows
    the reader cannot see would fail a page that is behaving perfectly.
    """
    return await pg.evaluate("""
    () => {
      const view = document.getElementById('chat-container').getBoundingClientRect();
      const out = {};
      document.querySelectorAll('#messages .message').forEach((r, i) => {
        const b = r.getBoundingClientRect();
        if (!r.hidden && b.bottom > view.top && b.top < view.bottom) {
          out['row' + i] = Math.round(b.top * 100) / 100;
        }
      });
      return out;
    }
    """)


async def _a_visible_block(pg) -> int:
    """Index of a tool block sitting in the viewport with room to open below it.

    Tests that assert on a block's own header have to use one the reader can
    see, for the scroll-anchoring reason above.
    """
    idx = await pg.evaluate("""
    () => {
      const view = document.getElementById('chat-container').getBoundingClientRect();
      return [...document.querySelectorAll('#messages details.tool-details')]
        .findIndex(d => { const r = d.getBoundingClientRect();
                          return r.top > view.top + 40 && r.top < view.bottom - 120; });
    }
    """)
    assert idx >= 0, "no tool block visible on screen to test against"
    return idx


async def _page_height(pg) -> int:
    return await pg.evaluate("() => document.getElementById('chat-container').scrollHeight")


def _worst_shift(before: list[float], after: list[float]) -> float:
    """The largest distance any row moved. Zero means nothing moved at all."""
    return max((abs(b - a) for a, b in zip(before, after, strict=False)), default=0.0)


# A pixel of slack: sub-pixel layout rounding, and removing the spinner dot from
# a finished call legitimately changes a row by a fraction.
TOLERANCE = 1.5


# ── A block opening under the reader ─────────────────────────────────────────

async def test_expanding_a_block_does_not_move_its_own_header(page):
    """The disclosure grows downward. The line you clicked stays under the
    cursor -- otherwise the click appears to scroll the page."""
    idx = await _a_visible_block(page)
    head = ("(i) => document.querySelectorAll('#messages details.tool-details')[i]"
            ".querySelector('summary').getBoundingClientRect().top")
    before = await page.evaluate(head, idx)
    await page.evaluate(
        "(i) => { document.querySelectorAll('#messages details.tool-details')[i].open = true; }", idx)
    await page.wait_for_timeout(200)
    after = await page.evaluate(head, idx)
    assert abs(after - before) <= TOLERANCE, f"header moved {after - before:+.2f}px"


async def test_expanding_a_block_above_the_viewport_moves_nothing_on_screen(page):
    """Growth off the top of the screen is absorbed by scroll anchoring.

    The rows above it do shift in viewport coordinates -- they have to, the
    window slid -- but the reader is looking at the bottom of a running
    transcript and must see none of it.
    """
    before = await _onscreen_tops(page)
    await page.evaluate(
        "() => { document.querySelectorAll('#messages details.tool-details')[0].open = true; }")
    await page.wait_for_timeout(250)
    after = await _onscreen_tops(page)
    shared = set(before) & set(after)
    assert shared, "nothing stayed on screen to compare"
    worst = max(abs(after[k] - before[k]) for k in shared)
    assert worst <= TOLERANCE, f"visible content moved {worst:.2f}px"


async def test_expanding_the_last_block_moves_nothing_above_it(page):
    """The last block is the one with the least room to grow, so it is the one
    that would push the transcript if anything did. It grows into the tail
    padding instead."""
    n = await page.evaluate("() => document.querySelectorAll('#messages details.tool-details').length")
    before = await _tops(page)
    await page.evaluate("(i) => { document.querySelectorAll('#messages details.tool-details')[i].open = true; }",
                        n - 1)
    await page.wait_for_timeout(250)
    after = await _tops(page)
    # Only the rows *below* the one that opened may move; it is the last block,
    # so that is nothing at all.
    assert _worst_shift(before[:n], after[:n]) <= TOLERANCE


async def test_collapsing_a_block_puts_the_transcript_back_exactly(page):
    """Round-tripping must be lossless, or repeated toggling walks the page."""
    before = await _tops(page)
    for state in (True, False):
        await page.evaluate(
            "(open) => { document.querySelectorAll('#messages details.tool-details')[2].open = open; }",
            state)
        await page.wait_for_timeout(200)
    after = await _tops(page)
    assert _worst_shift(before, after) <= TOLERANCE


# ── The live overlay ─────────────────────────────────────────────────────────

async def test_a_streaming_thinking_block_costs_one_line_however_long_it_gets(page):
    """Thinking arrives a token at a time and runs to hundreds of lines. In the
    flow every one of those tokens moved the conversation."""
    result = await page.evaluate("""
    () => {
      const box = document.getElementById('chat-container');
      const summary = appendReasoning();
      const row = summary.closest('.message.thinking');
      // Baseline *after* the empty row exists: the row itself is one line of
      // honest layout. What must not cost anything is the thinking inside it.
      const heightBefore = box.scrollHeight;
      const topEmpty = row.getBoundingClientRect().top;
      for (let i = 0; i < 80; i++) summary.textContent += `thinking line ${i}\\n`;
      return { heightBefore, heightAfter: box.scrollHeight,
               topEmpty, topFull: row.getBoundingClientRect().top,
               rowHeight: row.getBoundingClientRect().height };
    }
    """)
    assert result["heightAfter"] == result["heightBefore"], "80 lines of thinking changed the page height"
    assert abs(result["topFull"] - result["topEmpty"]) <= TOLERANCE
    assert result["rowHeight"] <= 40, "the live block should cost about one line"


async def test_collapsing_a_streamed_thinking_block_moves_nothing(page):
    """It leaves the overlay at the same one line it was already occupying, so
    dropping back into the flow is free."""
    await page.evaluate("""
    () => { const s = appendReasoning();
            for (let i = 0; i < 80; i++) s.textContent += `thinking line ${i}\\n`; }
    """)
    await page.wait_for_timeout(200)
    before = await _tops(page)
    await page.evaluate(
        "() => collapseReasoning(document.querySelector('.message.thinking.live .reasoning-summary'))")
    await page.wait_for_timeout(200)
    after = await _tops(page)
    assert _worst_shift(before, after) <= TOLERANCE


async def test_a_finished_result_costs_one_line_with_the_shipped_defaults(page):
    """The case that used to lurch twice per tool call, solved by not opening.

    A tool result cannot stream -- the diff only exists once the call has
    finished -- so an *auto-expanded* `edit` arrives at full height in one frame
    and `hide_tool_calls` takes it away at full height in the next. There used
    to be machinery to hide that; it was removed in favour of shipping `edit`
    and `write` collapsed, which is this test.
    """
    await page.evaluate("""
    () => { App.expandTools = []; App.hideToolCalls = true;
            window._s = { assistantEl: null, contentEl: null, text: '', reasoningEl: null };
            handleEvent({ type: 'tool_start', tool_call_id: 'e1', name: 'edit',
                          args: { path: 'agent_server/permissions.py' } }, window._s); }
    """)
    await page.wait_for_timeout(200)
    await _to_bottom(page)

    before, height_before = await _tops(page), await _page_height(page)

    await page.evaluate("(d) => handleEvent({ type: 'tool_end', tool_call_id: 'e1',"
                        " title: 'edit permissions.py', diff: d, lang: 'python' }, window._s)", DIFF)
    await page.wait_for_timeout(300)
    landed, height_landed = await _tops(page), await _page_height(page)

    shown = await page.evaluate("""
    () => { const n = document.querySelector('.message.tool[data-tool-call-id="e1"]');
            return { open: n.querySelector('details').open,
                     rowHeight: n.getBoundingClientRect().height }; }
    """)
    assert not shown["open"], "with the shipped defaults a result stays collapsed"
    assert shown["rowHeight"] <= 40, "a collapsed result is one line"
    assert abs(height_landed - height_before) <= TOLERANCE
    assert _worst_shift(before, landed) <= TOLERANCE

    # The next call starts and the finished one is hidden. It is the most recent
    # completed call, so it stays on screen -- and either way nothing moves.
    await page.evaluate("() => handleEvent({ type: 'tool_start', tool_call_id: 'b1',"
                        " name: 'bash', args: { command: 'pytest -q' } }, window._s)")
    await page.wait_for_timeout(300)
    assert _worst_shift(landed, await _tops(page)) <= TOLERANCE


async def test_the_foot_holds_one_tool_row_and_one_live_line(page):
    """With past calls hidden the foot of the transcript is two rows of the same
    height for the whole turn: the most recent call, and what is happening now.

    A finished call is not hidden when it finishes -- only when the next one
    starts, in the same handler that adds the replacement. So the row going out
    and the row coming in cancel, and the height never changes. Rounds are
    driven here one at a time and the foot measured after each.
    """
    await page.evaluate("""
    () => { App.expandTools = []; App.hideToolCalls = true;
            window._s = { assistantEl: null, contentEl: null, text: '', reasoningEl: null };
            handleEvent({ type: 'turn_start', user_message_id: null }, window._s); }
    """)
    await page.wait_for_timeout(150)

    async def foot():
        return await page.evaluate("""
        () => {
          const rows = [...document.querySelectorAll('#messages .message')].filter(r => !r.hidden);
          const tools = rows.filter(r => r.classList.contains('tool'));
          return { tools: tools.length,
                   newest: tools.length ? tools[tools.length - 1].dataset.toolCallId : null,
                   liveLine: rows.filter(r => r.classList.contains('status-line')).length,
                   height: Math.round(document.getElementById('chat-container').scrollHeight) };
        }
        """)

    heights = []
    for n in range(1, 4):
        await page.evaluate("""
        (n) => handleEvent({ type: 'tool_start', tool_call_id: `t${n}`, name: 'read',
                             args: { path: `f${n}.py` } }, window._s)
        """, n)
        await page.wait_for_timeout(160)
        running = await foot()
        assert running["tools"] == 1, f"round {n}: {running['tools']} tool rows on screen, want 1"
        assert running["newest"] == f"t{n}"
        assert running["liveLine"] == 1, "the live line must never leave mid-turn"

        await page.evaluate("""
        (n) => handleEvent({ type: 'tool_end', tool_call_id: `t${n}`,
                             title: `read f${n}.py`, output: 'ok' }, window._s)
        """, n)
        await page.wait_for_timeout(160)
        finished = await foot()
        assert finished["tools"] == 1, "the call that just finished stays on screen"
        assert finished["newest"] == f"t{n}"
        assert finished["liveLine"] == 1
        assert finished["height"] == running["height"], (
            f"round {n}: finishing a call changed the page height by "
            f"{finished['height'] - running['height']}px")
        heights.append(finished["height"])

    assert len(set(heights)) == 1, (
        f"the foot changed height between rounds: {heights}")


async def test_streaming_command_output_does_not_move_the_page(page):
    """A running command's output grows continuously, which is the thinking
    block's problem exactly: in the flow, every frame would shove the
    conversation the reader is trying to read."""
    await page.evaluate("""
    () => { App.expandTools = []; App.hideToolCalls = false;
            window._s = { assistantEl: null, contentEl: null, text: '', reasoningEl: null };
            handleEvent({ type: 'tool_start', tool_call_id: 'b9', name: 'bash',
                          args: { command: 'pytest -q' } }, window._s); }
    """)
    await page.wait_for_timeout(200)
    await _to_bottom(page)
    before, height_before = await _tops(page), await _page_height(page)

    # Frames carry the whole tail, not a delta, exactly as the server sends them.
    for count in (10, 40, 120):
        await page.evaluate("""
        (n) => { let text = '';
                 for (let i = 0; i < n; i++) text += `running check ${i}\\n`;
                 handleEvent({ type: 'tool_output', tool_call_id: 'b9', text }, window._s); }
        """, count)
        await page.wait_for_timeout(120)

    assert abs(await _page_height(page) - height_before) <= TOLERANCE
    assert _worst_shift(before, await _tops(page)) <= TOLERANCE

    shown = await page.evaluate("""
    () => { const n = document.querySelector('.message.tool[data-tool-call-id="b9"]');
            return { rowHeight: n.getBoundingClientRect().height,
                     painted: n.querySelector('.msg-content').getBoundingClientRect().height,
                     text: n.querySelector('.tool-stream').textContent.trim().split('\\n').pop() }; }
    """)
    assert shown["rowHeight"] <= 40, "streaming output should cost one line of layout"
    assert shown["painted"] > 100, "but it should be visible while it runs"
    assert shown["text"] == "running check 119", "the newest frame should be what is shown"

    # The output starts at the top and stays there. It used to chase its newest
    # line, which put the reader in the middle of a log that moved every frame
    # and pushed the block's own first line off the top. The beginning of the
    # thing is what says what the thing is, so that is what stays on screen.
    anchored = await page.evaluate("""
    () => { const pre = document.querySelector('.message.tool[data-tool-call-id="b9"] .tool-stream');
            return { scrollTop: pre.scrollTop, scrollable: pre.scrollHeight > pre.clientHeight,
                     firstLine: pre.textContent.split('\\n')[0] }; }
    """)
    assert anchored["scrollable"], "the output should have overflowed its box by now"
    assert anchored["scrollTop"] == 0, (
        "the streaming box scrolled away from the top on its own")
    assert anchored["firstLine"] == "running check 0"

    # ...unless the reader scrolls it to the bottom themselves, which opts into
    # following. Anything else leaves the view where they put it.
    followed = await page.evaluate("""
    async () => {
      const pre = document.querySelector('.message.tool[data-tool-call-id="b9"] .tool-stream');
      pre.scrollTop = pre.scrollHeight;                       // the reader asks to follow
      pre.dispatchEvent(new Event('scroll'));
      let text = '';
      for (let i = 0; i < 160; i++) text += `running check ${i}\\n`;
      handleEvent({ type: 'tool_output', tool_call_id: 'b9', text }, window._s);
      await new Promise(r => setTimeout(r, 120));
      return { atBottom: pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24 };
    }
    """)
    assert followed["atBottom"], (
        "a box the reader scrolled to the bottom should keep following the output")

    # And the label stays put while the output arrives under it. Scrolling the
    # whole overlay to follow the output took the summary -- which command is
    # running, and for how long -- off the top of the block.
    label = await page.evaluate("""
    () => {
      const n = document.querySelector('.message.tool[data-tool-call-id="b9"]');
      const box = n.querySelector('.msg-content').getBoundingClientRect();
      const s = n.querySelector('summary').getBoundingClientRect();
      return { summaryTop: s.top, summaryBottom: s.bottom,
               boxTop: box.top, boxBottom: box.bottom };
    }
    """)
    assert label["summaryTop"] >= label["boxTop"] - 2, (
        "the block's label scrolled out of view while its output streamed")
    assert label["summaryBottom"] <= label["boxBottom"] + 2


async def test_the_streamed_tail_is_replaced_by_the_real_result(page):
    """The tail is the last few thousand characters; the finished result is all
    of it, and both being present at once would show the end twice."""
    await page.evaluate("""
    () => { App.expandTools = ['bash']; App.hideToolCalls = false;
            window._s = { assistantEl: null, contentEl: null, text: '', reasoningEl: null };
            handleEvent({ type: 'tool_start', tool_call_id: 'b8', name: 'bash',
                          args: { command: 'seq 1 5' } }, window._s);
            handleEvent({ type: 'tool_output', tool_call_id: 'b8', text: '3\\n4\\n5\\n' }, window._s); }
    """)
    await page.wait_for_timeout(200)
    assert await page.evaluate(
        "() => !!document.querySelector('.message.tool[data-tool-call-id=\"b8\"] .tool-stream')")

    await page.evaluate("""
    () => handleEvent({ type: 'tool_end', tool_call_id: 'b8', title: 'bash seq 1 5',
                        content: '1\\n2\\n3\\n4\\n5\\n' }, window._s)
    """)
    await page.wait_for_timeout(250)
    state = await page.evaluate("""
    () => { const n = document.querySelector('.message.tool[data-tool-call-id="b8"]');
            return { stream: !!n.querySelector('.tool-stream'),
                     live: n.classList.contains('live') }; }
    """)
    assert not state["stream"], "the live tail outlived the call"
    # hide_tool_calls is off here, so a finished result belongs in the flow.
    assert not state["live"], "the overlay was not handed back when the call ended"


# ── Loading the rest of a long transcript ────────────────────────────────────

async def test_the_transcript_arrives_windowed(page):
    """Only the tail is drawn. A switch into a session re-renders all of this,
    and with several sessions open that happens constantly."""
    state = await page.evaluate("""
    () => ({ rows: document.querySelectorAll('#messages .message').length,
             control: !!document.querySelector('.load-earlier'),
             older: (document.querySelector('.load-earlier .hint') || {}).textContent || '' })
    """)
    assert state["control"], "a transcript past the window should offer the rest"
    assert state["rows"] < 81, "the whole history was drawn"
    assert "older" in state["older"]


async def test_loading_earlier_messages_does_not_move_the_viewport(page):
    """Height is added *above* the reader, which is the one direction that
    moves what they are looking at unless the scroller is corrected for it."""
    added = await page.evaluate("""
    async () => {
      const box = document.getElementById('chat-container');
      const control = document.querySelector('.load-earlier');
      box.scrollTop = 0;
      await new Promise(r => requestAnimationFrame(r));
      const view = box.getBoundingClientRect();
      // A row the reader can actually see, to check against afterwards.
      const anchor = [...document.querySelectorAll('#messages .message')]
        .find(r => r.getBoundingClientRect().top > view.top + 20);
      const before = anchor.getBoundingClientRect().top;
      const rowsBefore = document.querySelectorAll('#messages .message').length;
      await loadEarlierMessages(control);
      await new Promise(r => requestAnimationFrame(r));
      return { moved: anchor.getBoundingClientRect().top - before,
               added: document.querySelectorAll('#messages .message').length - rowsBefore };
    }
    """)
    assert added["added"] > 0, "nothing was loaded"
    assert abs(added["moved"]) <= TOLERANCE, (
        f"the viewport moved {added['moved']:+.2f}px when earlier messages loaded")


async def test_walking_back_reaches_the_start_and_stops_offering(page):
    """Batches must tile: no gaps, no repeats, and the control goes away."""
    result = await page.evaluate("""
    async () => {
      let clicks = 0;
      while (document.querySelector('.load-earlier') && clicks < 30) {
        await loadEarlierMessages(document.querySelector('.load-earlier'));
        clicks++;
      }
      const ids = [...document.querySelectorAll('#messages .message[id]')].map(r => r.id);
      return { clicks, rows: document.querySelectorAll('#messages .message').length,
               control: !!document.querySelector('.load-earlier'),
               unique: new Set(ids).size === ids.length };
    }
    """)
    assert result["control"] is False, "still offering to load with nothing older"
    assert result["rows"] == 81, f"expected the whole 81-message history, got {result['rows']}"
    assert result["unique"], "a message was rendered twice"


async def test_a_new_turn_still_lands_at_the_bottom_of_a_windowed_transcript(page):
    """Streaming appends; the window is about what arrives with the page."""
    state = await page.evaluate("""
    async () => {
      const before = document.querySelectorAll('#messages .message').length;
      window._s = { assistantEl: null, contentEl: null, text: '', reasoningEl: null };
      handleEvent({ type: 'tool_start', tool_call_id: 'nw', name: 'bash',
                    args: { command: 'ls' } }, window._s);
      handleEvent({ type: 'tool_end', tool_call_id: 'nw', title: 'bash ls',
                    content: 'a\\nb\\n' }, window._s);
      await new Promise(r => requestAnimationFrame(r));
      const rows = [...document.querySelectorAll('#messages .message')];
      // The live line is a row of its own and sits below the transcript for the
      // whole turn, so the streamed call is the last *message* row rather than
      // the last row outright.
      const messages = rows.filter(r => !r.classList.contains('status-line'));
      return { added: messages.length - before,
               lastIsNew: messages[messages.length - 1].dataset.toolCallId === 'nw',
               liveLineIsLast: rows[rows.length - 1].classList.contains('status-line'),
               control: !!document.querySelector('.load-earlier') };
    }
    """)
    assert state["added"] == 1
    assert state["lastIsNew"], "a streamed row did not land at the end"
    assert state["liveLineIsLast"], "the live line must stay at the foot of the transcript"
    assert state["control"], "streaming removed the way back to the rest of the session"


# ── Collapse all ─────────────────────────────────────────────────────────────

async def test_collapse_all_shuts_every_block_including_the_live_one(page):
    """An explicit instruction outranks the auto-expand settings and the live
    block's own rule, or the button does not mean what it says."""
    await page.evaluate("""(d) => {
      App.expandTools = ['write', 'edit']; App.hideToolCalls = true;
      document.querySelectorAll('#messages details.tool-details')
              .forEach((x, i) => { if (i % 3 === 0) x.open = true; });
      const s = appendReasoning();
      for (let i = 0; i < 40; i++) s.textContent += `thinking ${i}\\n`;
      window._s = { assistantEl: null, contentEl: null, text: '', reasoningEl: null };
      handleEvent({ type: 'tool_start', tool_call_id: 'z1', name: 'edit',
                    args: { path: 'x.py' } }, window._s);
      handleEvent({ type: 'tool_end', tool_call_id: 'z1', title: 'edit x.py',
                    diff: d, lang: 'python' }, window._s);
    }""", DIFF)
    await page.wait_for_timeout(300)
    assert await page.evaluate("() => document.querySelectorAll('#messages details[open]').length") > 0

    # Through the menu, the way it is actually reached.
    await page.click("#session-meta .dropdown > button.icon-btn")
    await page.click("#session-meta .dropdown-menu button:text-is('Collapse all blocks')")
    await page.wait_for_timeout(250)

    assert await page.evaluate("() => document.querySelectorAll('#messages details[open]').length") == 0
    assert await page.evaluate("() => document.querySelectorAll('#messages .message.live').length") == 0
    assert await page.evaluate("() => document.querySelector('#session-meta .dropdown-menu').hidden")


async def test_the_transcript_renders_without_console_errors(page):
    """A cheap guard on the whole page: every test above drives real handlers,
    so a thrown exception anywhere in app.js surfaces here."""
    await page.wait_for_timeout(200)
    assert page._console_errors == []
