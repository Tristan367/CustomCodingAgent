"""The browser tool, driven against a real Chromium.

These are slow-ish but they are the only kind that count here: the thing being
replaced was three implementations that each worked in isolation and did not
share state, which no unit test would have caught.
"""

import asyncio

import pytest

from agent_server import browser as engine
from agent_server.tools.base import ToolContext
from agent_server.tools.browser import browser

PAGE = """<!doctype html><html><head><title>Demo</title></head><body>
<h1>Demo</h1>
<form id="f">
  <label for="e">Email</label><input id="e" type="email">
  <button type="submit">Sign in</button>
</form>
<div id="dash" hidden><h2>Dashboard</h2></div>
<script>
document.getElementById('f').addEventListener('submit', (ev) => {
  ev.preventDefault();
  document.getElementById('f').hidden = true;
  document.getElementById('dash').hidden = false;
});
</script></body></html>"""

BROKEN = """<!doctype html><html><body><button id="b">Go</button>
<script>document.getElementById('b').onclick = () => { window.missing.x = 1; };</script>
</body></html>"""


@pytest.fixture
def page(tmp_path):
    path = tmp_path / "app.html"
    path.write_text(PAGE)
    return f"file://{path}"


@pytest.fixture
def broken(tmp_path):
    path = tmp_path / "broken.html"
    path.write_text(BROKEN)
    return f"file://{path}"


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    # The saved-login state goes to a per-test dir, so a run never leaves
    # `testbrow.json` behind in the real data dir or leaks a login across runs.
    state_dir = tmp_path / "browser_state"
    state_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(engine, "BROWSER_STATE_DIR", state_dir)
    context = ToolContext(
        session_id="test-browser", project_dir=str(tmp_path), abort=asyncio.Event()
    )
    yield context
    # The whole browser, not just the context. Playwright objects are bound to
    # the event loop that created them, and every test gets a fresh one, so a
    # Chromium held over from the previous test hangs on first use. The server
    # runs one loop for its lifetime, so this is a test concern only.
    await engine.close_all()


async def test_a_whole_flow_runs_in_one_call(ctx, page):
    result = await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "fill", "at": "label=Email", "text": "a@b.c"},
        {"action": "click", "at": 'role=button[name="Sign in"]'},
        {"action": "expect", "visible": "text=Dashboard"},
    ])
    assert not result.is_error, result.output
    assert "4 step(s) completed" in result.output


async def test_state_survives_between_calls(ctx, page):
    """A flow spans several tool calls. The old tools had one stateless and one
    stateful implementation, so logging in through one was invisible to the
    other -- which is the bug this whole rewrite exists for."""
    await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "click", "at": "text=Sign in"},
    ])
    result = await browser(ctx, steps=[{"action": "expect", "visible": "text=Dashboard"}])
    assert not result.is_error, result.output


async def test_reset_throws_the_state_away(ctx, page):
    await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "click", "at": "text=Sign in"},
    ])
    result = await browser(ctx, reset=True, steps=[
        {"action": "goto", "url": page},
        {"action": "expect", "visible": "text=Sign in"},
    ])
    assert not result.is_error, result.output


async def test_a_failed_assertion_fails_the_call(ctx, page):
    """A model cannot narrate its way past this, which is the point."""
    result = await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "expect", "visible": "text=Nothing here", "timeout_ms": 700},
    ])
    assert result.is_error
    assert "failed at step 2" in result.title


async def test_execution_stops_at_the_first_failure(ctx, page):
    result = await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "click", "at": "text=Does not exist", "timeout_ms": 500},
        {"action": "fill", "at": "label=Email", "text": "should not run"},
    ])
    assert result.is_error
    assert "should not run" not in result.output


async def test_console_errors_are_captured_and_attributed(ctx, broken):
    """"The button did nothing" is not a finding. The old tools could not see
    the console at all."""
    result = await browser(ctx, steps=[
        {"action": "goto", "url": broken},
        {"action": "click", "at": "text=Go"},
        {"action": "expect", "console_clean": True},
    ])
    assert result.is_error
    assert "pageerror" in result.output
    # The actual fault, not a restatement of the symptom.
    assert "Cannot set properties of undefined" in result.output, result.output
    # And it is attributed to the click, not dumped at the end.
    lines = result.output.splitlines()
    click_at = next(i for i, line in enumerate(lines) if "click text=Go" in line)
    assert "pageerror" in lines[click_at + 1]


async def test_a_clean_page_passes_the_console_check(ctx, page):
    result = await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "expect", "console_clean": True},
    ])
    assert not result.is_error, result.output


async def test_a_failure_reports_the_page_without_being_asked(ctx, page):
    """A failure the model needs three more calls to understand is a failure
    reported badly."""
    result = await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "click", "at": "text=Missing", "timeout_ms": 500},
    ])
    assert "diagnostics" in result.output
    assert "Accessibility tree" in result.output
    assert "Screenshot at failure" in result.output
    # And the tree names what *is* there, so the next attempt can be right.
    assert "Sign in" in result.output


async def test_the_snapshot_names_things_the_way_they_can_be_addressed(ctx, page):
    """The snapshot is what removes the guessing: roles and names read off it
    go straight back in as role=/label= targets."""
    result = await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "snapshot"},
    ])
    assert not result.is_error, result.output
    assert 'button "Sign in"' in result.output
    assert 'textbox "Email"' in result.output


async def test_screenshots_are_written_to_disk_and_their_paths_returned(ctx, page):
    """The old tools described a frame and threw the bytes away, so nothing
    could be compared or re-examined without redoing the whole flow."""
    from pathlib import Path

    result = await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "shoot"},
    ])
    paths = [
        line.split("-> ")[1].strip()
        for line in result.output.splitlines() if "-> " in line
    ]
    assert paths, result.output
    assert Path(paths[0]).is_file()


async def test_a_burst_captures_several_frames(ctx, page):
    result = await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "record", "count": 3, "interval_ms": 30},
    ])
    assert not result.is_error, result.output
    assert result.output.count("-> ") == 3


async def test_friendly_targets_resolve(ctx, page):
    """label= and placeholder= are not Playwright selector engines; they came
    back as 'Unknown engine "label"' until they were translated."""
    result = await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "fill", "at": "label=Email", "text": "x@y.z"},
        {"action": "expect", "count": 1, "at": 'role=button[name="Sign in"]'},
        {"action": "expect", "count": 1, "at": "#e"},
    ])
    assert not result.is_error, result.output


async def test_eval_is_an_escape_hatch(ctx, page):
    result = await browser(ctx, steps=[
        {"action": "goto", "url": page},
        {"action": "eval", "js": "document.title"},
    ])
    assert not result.is_error, result.output
    assert "Demo" in result.output


async def test_network_reports_the_requests(ctx, page):
    """The network tab: did the click actually hit the endpoint, with what status."""
    await browser(ctx, steps=[{"action": "goto", "url": page}])
    result = await browser(ctx, steps=[{"action": "network"}])
    assert not result.is_error, result.output
    assert "GET" in result.output
    assert "200" in result.output
    assert "app.html" in result.output


async def test_network_filter_narrows_to_matching_urls(ctx, page):
    await browser(ctx, steps=[{"action": "goto", "url": page}])
    result = await browser(ctx, steps=[
        {"action": "network", "filter": "app.html", "count": 10},
    ])
    assert not result.is_error, result.output
    assert "app.html" in result.output


async def test_an_unknown_action_says_what_is_available(ctx, page):
    result = await browser(ctx, steps=[{"action": "teleport"}])
    assert result.is_error
    assert "unknown action" in result.output
    assert "snapshot" in result.output


async def test_no_steps_is_refused_with_an_example(ctx):
    result = await browser(ctx, steps=[])
    assert result.is_error
    assert "goto" in result.output


async def test_too_many_steps_is_refused(ctx):
    result = await browser(ctx, steps=[{"action": "wait", "ms": 1}] * 100)
    assert result.is_error
    assert "keeps its state between calls" in result.output


async def test_two_sessions_do_not_share_a_browser(ctx, page):
    """One chat's cookies have no business in another's. The old tools shared
    a single global page across every session."""
    other = ToolContext(
        session_id="test-browser-other", project_dir=ctx.project_dir, abort=asyncio.Event()
    )
    try:
        await browser(ctx, steps=[
            {"action": "goto", "url": page},
            {"action": "click", "at": "text=Sign in"},
        ])
        result = await browser(other, steps=[
            {"action": "goto", "url": page},
            {"action": "expect", "visible": "text=Sign in"},
        ])
        assert not result.is_error, result.output
    finally:
        await engine.close_session(other.session_id)


async def test_a_shot_returns_its_path_and_nothing_else(monkeypatch):
    """`browser` drives and asserts. It saves a frame and hands back the path;
    what anything makes of that image afterwards is not this tool's business,
    and wiring an image-describing tool into it bought one round trip for a
    permanent coupling to something the app does not ship."""
    from agent_server.tools import browser as browser_tool

    assert not hasattr(browser_tool, "_describe")
    assert not hasattr(browser_tool, "_with_comparisons")
