"""The tool surface the model is offered.

These exist because parameters were once removed for being unused in a log of
two short sessions. Usage is not evidence of uselessness when almost nothing
has been used yet, so the capabilities are pinned here instead.

Eight overlapping vision and browser tools became two. That is a consolidation,
not a reduction: everything the old surface could do has a home below, and
anything that does not is a capability that was quietly dropped.
"""

import json

from agent_server.tools.registry import TOOLS


def props(name: str) -> set[str]:
    return set(TOOLS[name].schema()["function"]["parameters"]["properties"])


def step_props() -> set[str]:
    schema = TOOLS["browser"].schema()["function"]["parameters"]
    return set(schema["properties"]["steps"]["items"]["properties"])


def actions() -> set[str]:
    schema = TOOLS["browser"].schema()["function"]["parameters"]
    return set(schema["properties"]["steps"]["items"]["properties"]["action"]["enum"])


# ── The consolidation ───────────────────────────────────────────────────────

def test_the_overlapping_browser_tools_are_gone():
    """browser-goto/click/fill/screenshot/steps drove one global page while
    `screenshot` launched a separate browser per call, so a login made through
    one was invisible to the other."""
    for name in ("browser-goto", "browser-click", "browser-fill",
                 "browser-screenshot", "browser-steps", "screenshot"):
        assert name not in TOOLS, f"{name} should have been folded into `browser`"
    assert "browser" in TOOLS
    # `vision` is deliberately not built in: describing an image needs hardware
    # or an account this app cannot assume. It ships as a custom tool.
    assert "vision" not in TOOLS


def test_a_whole_flow_fits_in_one_call():
    """Four tool calls to fill a form is four model round trips and four
    context re-reads to learn what the first one did."""
    assert {"goto", "fill", "click", "expect", "shoot"} <= actions()


# ── Capabilities that must survive ──────────────────────────────────────────

def test_a_frame_can_be_saved_and_re_examined_later():
    """The old `browser-*` tools threw the bytes away, so nothing could be
    looked at twice without redoing the whole flow."""
    assert "shoot" in actions()


def test_sequences_are_still_possible():
    """Recording frames over time is how an animation or a loading state gets
    inspected at all."""
    assert "record" in actions()
    assert {"count", "interval_ms"} <= step_props()


def test_interactions_before_a_capture_are_still_possible():
    """`screenshot` took an `actions` list to reach a state first. Every one of
    those is a step in its own right now."""
    assert {"click", "fill", "press", "hover", "scroll", "wait"} <= actions()
    assert {"until", "state", "ms"} <= step_props()


def test_a_capture_can_be_cropped_or_full_page():
    assert "full_page" in step_props()
    assert "at" in step_props()


def test_the_viewport_is_still_configurable():
    assert {"width", "height"} <= props("browser")
    assert "resize" in actions()


# ── Capabilities the old surface did not have ───────────────────────────────

def test_the_page_can_be_inspected_structurally():
    """Without this a model has to guess CSS selectors, and a small one guesses
    wrong twice before giving up."""
    assert "snapshot" in actions()


def test_assertions_exist():
    """A pass/fail the model cannot narrate its way around is the only thing
    that makes "verify before you claim it works" enforceable."""
    assert "expect" in actions()
    assert {"visible", "hidden", "text", "url", "count", "console_clean"} <= step_props()


def test_there_is_an_escape_hatch():
    assert "eval" in actions()


def test_a_flow_can_be_started_from_a_clean_browser():
    assert "reset" in props("browser")


# ── Everything else ─────────────────────────────────────────────────────────

def test_capture_is_the_only_desktop_capturer():
    """One capturer per domain. Two tools that could both capture is how the
    old surface ended up with two browsers that did not share state."""
    assert "capture" in TOOLS
    assert "screenshot" not in TOOLS

def test_the_question_tool_stays_removed():
    """Deliberate: the model asks in prose and the user answers in the box."""
    assert "question" not in TOOLS


def test_the_browser_is_never_run_concurrently_with_itself():
    """It drives one stateful page; two steps at once would interleave."""
    assert not TOOLS["browser"].parallel_safe


def test_every_built_in_is_offered_unless_the_session_disabled_it():
    """`browser` and `capture` used to carry a `vision_only` flag, and the agent
    loop passed `include_vision=not provider.supports_vision()`. Every provider
    returned False, so the tools survived by accident; a provider that ever said
    True would have silently lost both. The flag is gone -- what a session is
    offered is now decided in one place, by its disabled-tools list."""
    from agent_server.tools.registry import BUILT_IN_NAMES, tool_schemas

    offered = {s["function"]["name"] for s in tool_schemas()}
    assert offered >= BUILT_IN_NAMES
    assert {"browser", "capture"} <= offered

    without = {s["function"]["name"] for s in tool_schemas(exclude={"browser"})}
    assert "browser" not in without
    assert "capture" in without


# ── What a stock install actually advertises ────────────────────────────────

def test_no_shipped_tool_mentions_vision():
    """There is no built-in `vision`: describing an image needs hardware or an
    account no install can be assumed to have. A stock install must therefore
    not name it anywhere in what it sends the model -- guidance for a tool that
    is not there costs tokens on every request and invites a call that can only
    come back "no `vision` tool is installed"."""
    from agent_server.tools.registry import tool_schemas

    for schema in tool_schemas():
        assert "vision" not in schema["function"]["description"].lower(), schema["function"]["name"]


def test_nothing_in_the_tool_surface_depends_on_a_tool_that_is_not_shipped():
    """`browser` and `capture` used to carry parameters -- `ask`, `compare`,
    capture's `prompt` -- that only worked if the user had added a custom tool
    named exactly `vision`. On any other install they were schema sent on every
    request for a call that could only come back "not installed", and the name
    was hardcoded, so a user who called theirs `eyes` got nothing either way.

    Saving a frame and looking at a frame are separate steps now. That costs one
    round trip and removes a coupling from the built-in tools to something the
    app does not ship."""
    from agent_server.tools.registry import tool_schemas

    for schema in tool_schemas():
        assert "vision" not in json.dumps(schema).lower(), schema["function"]["name"]

    assert "ask" not in step_props()
    assert "compare" not in step_props()
    assert "prompt" not in props("capture")
