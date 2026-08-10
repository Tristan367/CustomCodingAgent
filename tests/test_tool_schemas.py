"""The tool surface the model is offered.

These exist because parameters were once removed for being unused in a log of
two short sessions. Usage is not evidence of uselessness when almost nothing
has been used yet, so the capabilities are pinned here instead.

Eight overlapping vision and browser tools became two. That is a consolidation,
not a reduction: everything the old surface could do has a home below, and
anything that does not is a capability that was quietly dropped.
"""

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

def test_a_page_can_be_checked_against_a_local_image_in_one_call():
    """A mockup and a live page compared in a single call. Capture moved into
    `browser`, so `shoot` takes the reference images itself -- otherwise this
    costs a round trip."""
    assert "compare" in step_props()


def test_frames_can_be_captured_without_paying_to_describe_them():
    """Analysis is a vision model call over the network, seconds each. It is
    opt-in per step now rather than a flag to turn off."""
    assert "ask" in step_props()


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
