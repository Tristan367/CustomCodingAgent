"""Turning a written reply into something worth listening to."""

import pytest
from fastapi.testclient import TestClient

from agent_server import tts

# ── Markdown to prose ───────────────────────────────────────────────────────

def test_fenced_code_is_dropped_silently():
    md = "Here is the fix.\n\n```python\ndef f():\n    return 1\n```\n\nIt works."
    prose = tts.to_prose(md)
    assert "def f" not in prose
    assert "return" not in prose
    assert "Here is the fix." in prose and "It works." in prose


def test_tables_and_rules_are_dropped():
    md = "Before.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n---\n\nAfter."
    prose = tts.to_prose(md)
    assert "|" not in prose
    assert "Before." in prose and "After." in prose


def test_links_keep_their_text_and_lose_their_url():
    prose = tts.to_prose("See [the docs](https://example.com/a/b) for details.")
    assert prose == "See the docs for details."


def test_a_bare_url_is_not_spelled_out():
    prose = tts.to_prose("Fetch https://example.com/very/long/path now.")
    assert "example.com" not in prose
    assert "a link" in prose


def test_inline_code_keeps_its_text_without_backticks():
    prose = tts.to_prose("The fix is in `agent.py` at line 12.")
    assert prose == "The fix is in agent.py at line 12."


def test_emphasis_and_headings_are_stripped():
    prose = tts.to_prose("## Title\n\nThis is **bold** and *italic*.")
    assert "#" not in prose and "*" not in prose
    assert "Title" in prose and "bold" in prose


def test_underscores_in_identifiers_survive():
    """`_` in paths and dunder names is literal, not an emphasis marker."""
    prose = tts.to_prose("Run `web_ui/static/js/app.js` and `__init__.py`.")
    assert prose == "Run web_ui/static/js/app.js and __init__.py."


def test_emojis_are_stripped():
    """Emoji are pictures, not words, so they never reach the synthesiser."""
    prose = tts.to_prose("Done \U0001F44D and ready \u2728.")
    assert "\U0001F44D" not in prose and "\u2728" not in prose
    assert "Done" in prose and "ready" in prose


# ── Sentence splitting ──────────────────────────────────────────────────────

def test_a_soft_wrapped_sentence_is_not_split_in_half():
    """Markdown wraps at the author's column, not at pauses. Splitting on every
    newline chopped clauses in half and the voice stopped mid-thought."""
    md = "It works, and a fast grep now\nreports first. Then the slow one."
    assert tts.plan(md) == [
        "It works, and a fast grep now reports first.",
        "Then the slow one.",
    ]


def test_list_items_stay_separate():
    md = "- First item\n- Second item\n- Third item"
    assert tts.plan(md) == ["First item.", "Second item.", "Third item."]


@pytest.mark.parametrize("text,head", [
    ("It works, e.g. like this. Next sentence.", "It works, for example like this."),
    ("Version 1.0 shipped. Next sentence.", "Version 1 point 0 shipped."),
    ("Dr. Smith agreed. Next sentence.", "Dr. Smith agreed."),
    ("Ask J. Smith about it. Next sentence.", "Ask J. Smith about it."),
])
def test_abbreviations_and_numbers_do_not_end_a_sentence(text, head):
    assert tts.plan(text) == [head, "Next sentence."]


# ── Saying it the way a person would ────────────────────────────────────────

@pytest.mark.parametrize("raw,spoken", [
    # Every one of these was confirmed wrong at the phoneme level first:
    # "3.3" came out as "three. three", with a full stop in the middle.
    ("It was 3.3x too strong", "It was 3 point 3 times too strong"),
    ("80 DPR vs 24", "80 DPR versus 24"),
    ("~25% stronger", "about 25% stronger"),
    ("1/day reroll", "1 per day reroll"),
    ("5-10 items", "5 to 10 items"),
    ("$0.11 total", "0 point 11 dollars total"),
    ("#3 and 50/50", "number 3 and 50 50"),
    ("e.g. this", "for example this"),
    ("a — b", "a. b"),
    ("AI & ML", "AI and ML"),
])
def test_symbols_are_spoken_not_spelled(raw, spoken):
    assert tts.normalise(raw) == spoken


@pytest.mark.parametrize("raw,spoken", [
    # A dot glued to a digit is "point", to a letter is "dot".
    ("Version 3.2.1 shipped", "Version 3 point 2 point 1 shipped"),
    ("Edit file.py and run app.js", "Edit file dot py and run app dot js"),
    ("See U.S.A. for details", "See U dot S dot A. for details"),
    # File paths lose their slashes.
    ("Run /tmp/file.py now", "Run tmp file dot py now"),
    # Repeated full stops collapse to one pause.
    ("Wait... then go", "Wait. then go"),
])
def test_dots_slashes_and_repeated_stops(raw, spoken):
    assert tts.normalise(raw) == spoken


def test_normalising_keeps_the_blank_lines_between_list_items():
    r"""Collapsing runs of whitespace welded every item into one block, because
    \s also matches the newlines that separate them."""
    assert tts.plan("- First item\n- Second item") == ["First item.", "Second item."]


def test_a_clip_gets_silence_at_the_front():
    """Kokoro starts on the first phoneme with no run-up, so the browser's play
    ramp landed on the opening consonant and ate it."""
    import numpy as np

    rate = 24000
    speech = np.ones(rate // 2, dtype="float32") * 0.5
    out = tts.pad_edges(speech, rate)
    lead = int(np.nonzero(np.abs(out) > 0.01)[0][0])
    assert lead >= rate * 40 // 1000, f"only {lead / rate * 1000:.0f}ms of lead-in"


def test_an_overlong_tail_is_trimmed():
    import numpy as np

    rate = 24000
    speech = np.concatenate([
        np.ones(rate // 2, dtype="float32") * 0.5,
        np.zeros(rate, dtype="float32"),          # a second of trailing silence
    ])
    out = tts.pad_edges(speech, rate)
    assert len(out) < len(speech), "the trailing silence should be cut back"


def test_question_and_exclamation_end_sentences():
    assert tts.plan("Did it work? Yes! It did.") == ["Did it work?", "Yes!", "It did."]


def test_a_reply_that_is_only_code_produces_nothing_to_say():
    assert tts.plan("```\nprint(1)\n```") == []


# ── Routes ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from agent_server.main import app
    return TestClient(app)


def test_plan_route_returns_sentences(client):
    r = client.post("/api/tts/plan", json={"text": "One. Two."})
    assert r.status_code == 200
    assert r.json()["sentences"] == ["One.", "Two."]


def test_status_route_reports_voices_and_settings(client):
    body = client.get("/api/tts/status").json()
    assert set(body) >= {"available", "voices", "voice", "speed", "volume"}
    assert all(v.startswith(("af_", "am_", "bf_", "bm_")) for v in body["voices"])


def test_speaking_nothing_is_a_bad_request_not_a_crash(client):
    assert client.post("/api/tts/speak", json={"text": "   "}).status_code == 400


def test_a_missing_model_explains_itself(monkeypatch):
    """The whisper side degrades with a message rather than a stack trace, and
    so must this one."""
    import asyncio

    monkeypatch.setattr(tts, "_kokoro", None)
    monkeypatch.setattr(tts, "TTS_MODEL", "")
    with pytest.raises(tts.TTSError, match="unavailable"):
        asyncio.run(tts.synth("hello"))
