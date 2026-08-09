"""The tool surface the model is offered.

These exist because parameters were once removed for being unused in a log of
two short sessions. Usage is not evidence of uselessness when almost nothing
has been used yet, so the capabilities are pinned here instead.
"""

from agent_server.tools.registry import TOOLS


def props(name: str) -> set[str]:
    return set(TOOLS[name].schema()["function"]["parameters"]["properties"])


def test_vision_can_capture_a_page_alongside_local_files():
    """Its own capture params are what let one call compare a mockup to a live
    page; `screenshot` cannot take local paths, so this is not a duplicate."""
    assert {"url", "selector", "full_page", "width", "height"} <= props("vision")
    assert "paths" in props("vision")


def test_screenshot_can_capture_without_describing():
    """Analysis costs seconds of model time; capturing frames to look at later
    must not require paying for it."""
    assert "analyze" in props("screenshot")


def test_screenshot_keeps_its_sequence_and_interaction_controls():
    assert {"count", "interval_ms", "actions", "wait_for", "delay_ms"} <= props("screenshot")


def test_the_question_tool_stays_removed():
    """Deliberate: the model asks in prose and the user answers in the box."""
    assert "question" not in TOOLS
