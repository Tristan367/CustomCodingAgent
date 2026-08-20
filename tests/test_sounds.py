"""The notification sounds, and the seam between their two halves.

The names live in `config.SOUND_CHOICES` because the picker is rendered server
side; the synthesis lives in `SOUNDS` in app.js because it runs in the browser.
Nothing in the language stops those two drifting, and the failure is quiet --
an option that plays nothing, or a voice nobody can select. These tests are the
thing that stops it.

They were originally three calls to one function at different pitches, which is
why "click", "chime" and "knock" all sounded like the same ping: timbre comes
from the envelope and the spectrum, not the frequency.
"""

import re
from pathlib import Path

import pytest

from agent_server.config import DEFAULT_SOUND, FIXED_SOUNDS, SOUND_CHOICES

APP_JS = Path(__file__).resolve().parent.parent / "web_ui" / "static" / "js" / "app.js"


def _synth_source() -> str:
    text = APP_JS.read_text(errors="replace")
    start = text.index("const SOUNDS = {")
    depth, i = 0, start + len("const SOUNDS = ")
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    raise AssertionError("could not find the end of the SOUNDS table in app.js")


def _synth_keys() -> list[str]:
    """Voice names defined in app.js, in source order."""
    body = _synth_source()
    # Top-level keys only: `name: (s) => ...`
    return re.findall(r"^  ([a-z][a-z0-9]*):\s*\(s\)", body, re.M)


# ── The two halves agree ─────────────────────────────────────────────────────

def test_every_offered_sound_has_a_voice():
    """An option with no voice falls through to the uploaded-file branch and
    tries to fetch itself as an audio file, which 404s silently."""
    missing = [name for name, _ in SOUND_CHOICES if name not in _synth_keys()]
    assert not missing, f"offered in the picker with no synthesis in app.js: {missing}"


def test_every_voice_is_either_offered_or_fixed():
    """A voice nobody can reach is dead code; the exceptions are the two
    attention tones, which are played directly rather than chosen."""
    offered = {name for name, _ in SOUND_CHOICES}
    extra = [k for k in _synth_keys() if k not in offered and k not in FIXED_SOUNDS]
    assert not extra, f"synthesised but unreachable: {extra}"


def test_the_fixed_attention_tones_exist():
    """"Needs you" and "failed" must not be the user's chosen sound: you should
    be able to tell them apart without remembering what you picked."""
    for name in FIXED_SOUNDS:
        assert name in _synth_keys(), f"{name} has no voice"
    offered = {name for name, _ in SOUND_CHOICES}
    assert not offered & set(FIXED_SOUNDS), "an attention tone is also offered as a choice"


def test_the_default_is_one_of_the_choices():
    assert DEFAULT_SOUND in {name for name, _ in SOUND_CHOICES}


# ── The catalogue itself ─────────────────────────────────────────────────────

def test_the_names_are_unique_and_url_safe():
    names = [name for name, _ in SOUND_CHOICES]
    assert len(names) == len(set(names)), "a sound id is listed twice"
    for name in names:
        assert re.fullmatch(r"[a-z][a-z0-9]*", name), f"{name} is not a safe id"


def test_every_choice_has_a_label():
    for name, label in SOUND_CHOICES:
        assert label and label[0].isupper(), f"{name} has no readable label"


def test_there_are_enough_to_choose_between():
    assert len(SOUND_CHOICES) >= 8, "the point of this was more than three"


# ── They are actually different from each other ──────────────────────────────

@pytest.mark.parametrize("name,_label", SOUND_CHOICES)
def test_each_voice_is_defined_distinctly(name, _label):
    """The original three shared one call, so they could only differ by pitch.
    Each voice now has its own body; identical bodies would mean the same
    complaint again."""
    body = _synth_source()
    match = re.search(rf"^  {name}:\s*\(s\) =>(.*?)(?=^  [a-z][a-z0-9]*:|\Z)",
                      body, re.M | re.S)
    assert match, f"no voice body for {name}"
    assert match.group(1).strip(), f"{name} has an empty voice"


def test_no_two_voices_are_the_same_sound():
    body = _synth_source()
    bodies = {}
    for name in _synth_keys():
        match = re.search(rf"^  {name}:\s*\(s\) =>(.*?)(?=^  [a-z][a-z0-9]*:|\Z)",
                          body, re.M | re.S)
        # Normalise whitespace and comments so formatting differences do not
        # hide a genuine duplicate.
        text = re.sub(r"//.*", "", match.group(1))
        bodies.setdefault(re.sub(r"\s+", " ", text).strip(), []).append(name)
    duplicates = {k: v for k, v in bodies.items() if len(v) > 1}
    assert not duplicates, f"these sounds are synthesised identically: {list(duplicates.values())}"


def test_the_voices_use_more_than_one_kind_of_synthesis():
    """A catalogue of nothing but `tone` calls is a catalogue of beeps at
    different pitches, which is what this replaced."""
    body = _synth_source()
    assert body.count("s.noise(") >= 4, "no percussive voices: everything is a tone"
    assert body.count("type: 'triangle'") + body.count("type: 'square'") >= 2, (
        "every pitched voice is a sine, so they will all sound like bells"
    )
    assert "to:" in body, "no pitch bends, which is what makes a pop or a blip"


def test_the_default_sound_is_the_gentle_one():
    """It fires when a long run finishes, often while the user is reading
    something else, so the least startling voice is the right default."""
    assert DEFAULT_SOUND == "swell"


def test_nothing_hardcodes_a_different_default():
    """The default used to be spelled `'click'` in five places -- two templates,
    a route, and two spots in app.js -- so changing it changed it partially."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    stale = []
    for path in (root / "web_ui/templates/base.html",
                 root / "web_ui/templates/index_content.html",
                 root / "agent_server/routes/settings.py"):
        if re.search(r"sound_choice['\"],\s*['\"]click['\"]", path.read_text()):
            stale.append(path.name)
        if "soundKind || 'click'" in path.read_text():
            stale.append(path.name)
    assert not stale, f"a literal sound default is still hardcoded in: {stale}"
