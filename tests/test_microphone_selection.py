"""A saved microphone that has gone stale must not break dictation.

The reported failure was the whole of it: pressing the dictation button
produced "Microphone unavailable: Constraints could not be satisfied." and
nothing else, on a machine with three working inputs. The cause was a
`deviceId: { exact: ... }` read from localStorage. That id stops being valid
without anything the user did:

  * a Bluetooth headset reconnects and comes back under a new id;
  * Firefox rotates deviceIds between browser restarts unless the origin holds
    a persistent permission.

`exact` turns a stale preference into a hard failure, and OverconstrainedError's
entire message -- "Constraints could not be satisfied." -- names neither the
constraint nor the device, so there is nothing on screen pointing at the mic
picker. Dictation stayed dead until the user thought to re-pick a microphone
they had never un-picked.

The rule these tests hold: the chosen device is a preference, never a
requirement. Every one of them drives the real functions from app.js against a
stubbed `navigator.mediaDevices`, so they measure the shipped code rather than
a transcription of it.
"""

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def home(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("mic-data")
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
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


# A stub standing in for the browser's media stack. `devices` is what
# enumerateDevices reports; `openable` is the subset getUserMedia will actually
# hand a stream for, which is not the same thing -- a device can be listed and
# still be held by another application.
STUB = """
([devices, openable]) => {
  window.__gum = [];
  const err = (name, extra) => Object.assign(new Error(
    name === 'OverconstrainedError' ? 'Constraints could not be satisfied.' : name),
    { name, ...extra });
  navigator.__stub = {
    enumerateDevices: async () => devices.map(
      (d) => ({ kind: 'audioinput', deviceId: d.id, label: d.label })),
    getUserMedia: async (c) => {
      window.__gum.push(JSON.parse(JSON.stringify(c)));
      const want = c.audio && c.audio.deviceId && c.audio.deviceId.exact;
      if (want && !openable.includes(want)) {
        throw err('OverconstrainedError', { constraint: 'deviceId' });
      }
      if (!want && !openable.length) throw err('NotFoundError');
      return { id: want || openable[0], getTracks: () => [] };
    },
  };
  Object.defineProperty(navigator, 'mediaDevices',
    { configurable: true, get: () => navigator.__stub });
}
"""


async def _open(page, devices, openable, saved_id="", saved_label=""):
    """Run the shipped openMicStream against a stubbed media stack."""
    await page.evaluate(STUB, [devices, openable])
    await page.evaluate(
        "([id, label]) => { localStorage.clear(); if (id) saveMicDevice(id, label); }",
        [saved_id, saved_label],
    )
    return await page.evaluate(
        """async () => {
            try {
              const r = await openMicStream({ channelCount: 1 });
              return {
                ok: true, opened: r.stream.id, notes: r.notes,
                requests: window.__gum,
                savedId: localStorage.getItem('micDeviceId') || '',
                savedLabel: localStorage.getItem('micDeviceLabel') || '',
              };
            } catch (e) {
              return { ok: false, error: e.name, text: micErrorText(e) };
            }
        }"""
    )


@pytest.fixture
async def page(home):
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:                       # pragma: no cover
            pytest.skip(f"Chromium is not available: {exc}")
        pg = await browser.new_page()
        await pg.goto(home)
        await pg.wait_for_function("typeof openMicStream === 'function'")
        yield pg
        await browser.close()


TWO = [{"id": "aaa", "label": "Cloud Nest"}, {"id": "bbb", "label": "Analog Stereo"}]


# ── The reported failure ─────────────────────────────────────────────────────

async def test_a_saved_device_that_no_longer_exists_still_records(page):
    """The bug, exactly: an id from a previous session, and no device with it.

    Against the old code this returned ok=False with "Constraints could not be
    satisfied." Recording must happen anyway.
    """
    r = await _open(page, TWO, ["aaa", "bbb"], saved_id="gone-with-the-old-session")
    assert r["ok"], f"dictation refused to start: {r.get('text')}"
    assert r["opened"] == "aaa", "it should have fallen through to the default device"


async def test_the_last_request_pins_nothing_once_the_saved_device_is_gone(page):
    r = await _open(page, TWO, ["aaa", "bbb"], saved_id="gone")
    assert not any("deviceId" in req["audio"] for req in r["requests"]), (
        "a device that does not exist must not be pinned with `exact`")
    assert all(req["audio"]["channelCount"] == 1 for req in r["requests"]), (
        "the caller's own constraints must survive the fallback")


async def test_a_disconnected_device_keeps_its_place_for_when_it_comes_back(page):
    """Falling back is not the same as forgetting. A headset that is off should
    still be the chosen microphone when it is switched on again, so the
    preference survives the fallback and is picked up on the next attempt."""
    r = await _open(page, TWO, ["aaa", "bbb"], saved_id="beats", saved_label="Beats Studio")
    assert r["ok"] and r["opened"] == "aaa", "it recorded from the default meanwhile"
    assert r["savedId"] == "beats", "the choice must not be silently discarded"

    back = TWO + [{"id": "beats", "label": "Beats Studio"}]
    again = await page.evaluate(STUB, [back, ["aaa", "bbb", "beats"]]) or await page.evaluate(
        """async () => {
            const r = await openMicStream({ channelCount: 1 });
            return { opened: r.stream.id, notes: r.notes };
        }"""
    )
    assert again["opened"] == "beats", "reconnecting should just work"


async def test_a_missing_device_is_reported_once_not_on_every_press(page):
    """The note is worth saying; saying it on every single dictation press for
    as long as a headset is switched off is not."""
    await page.evaluate(STUB, [TWO, ["aaa", "bbb"]])
    await page.evaluate(
        "() => { localStorage.clear(); saveMicDevice('beats', 'Beats Studio'); }")
    counts = await page.evaluate(
        """async () => {
            const out = [];
            for (let i = 0; i < 3; i++) {
              out.push((await openMicStream({ channelCount: 1 })).notes.length);
            }
            return out;
        }"""
    )
    assert counts == [1, 0, 0], f"said once, then quiet: {counts}"


# ── Re-finding the device the user actually chose ────────────────────────────

async def test_a_rotated_id_is_recovered_by_label(page):
    """Firefox hands out a fresh deviceId for the same hardware after a
    restart. The label is what the user recognised, so it is what we match."""
    r = await _open(page, TWO, ["aaa", "bbb"],
                    saved_id="yesterdays-id", saved_label="Analog Stereo")
    assert r["opened"] == "bbb", "the chosen microphone should have been re-found"
    assert r["savedId"] == "bbb", "and the new id remembered for next time"
    assert r["notes"] == [], "silently correct: nothing went wrong from the user's view"


async def test_a_present_device_is_used_directly(page):
    r = await _open(page, TWO, ["aaa", "bbb"], saved_id="bbb", saved_label="Analog Stereo")
    assert r["opened"] == "bbb"
    assert r["requests"][0]["audio"]["deviceId"] == {"exact": "bbb"}
    assert r["notes"] == []


async def test_no_saved_device_asks_for_no_device(page):
    r = await _open(page, TWO, ["aaa", "bbb"])
    assert r["ok"] and r["opened"] == "aaa"
    assert "deviceId" not in r["requests"][0]["audio"]


# ── Listed but not openable ──────────────────────────────────────────────────

async def test_a_device_held_by_another_application_falls_back(page):
    """enumerateDevices lists it, getUserMedia will not open it. Recording from
    the default beats not recording."""
    r = await _open(page, TWO, ["aaa"], saved_id="bbb", saved_label="Analog Stereo")
    assert r["ok"] and r["opened"] == "aaa"
    assert len(r["requests"]) == 2, "it should have tried the pinned device first"
    assert r["savedId"] == "bbb", "a busy device is still the one that was chosen"


async def test_the_user_is_told_which_microphone_was_lost(page):
    r = await _open(page, TWO, ["aaa"], saved_id="bbb", saved_label="Analog Stereo")
    assert len(r["notes"]) == 1
    assert "Analog Stereo" in r["notes"][0], "name the device, not the constraint"
    assert "default" in r["notes"][0].lower(), "and say what is being used instead"


async def test_a_lost_device_with_no_remembered_name_says_nothing_confusing(page):
    """Preferences saved before labels were stored have an id and nothing else.
    There is no name to report, so the fallback is silent rather than blank."""
    r = await _open(page, TWO, ["aaa", "bbb"], saved_id="ancient")
    assert r["ok"]
    assert r["notes"] == [], f"nothing useful to say, so say nothing: {r['notes']}"


# ── Genuine failures still surface ───────────────────────────────────────────

async def test_no_microphone_at_all_is_still_an_error(page):
    r = await _open(page, [], [])
    assert not r["ok"], "the fallback must not swallow a real failure"
    assert r["error"] == "NotFoundError"


async def test_the_error_text_names_something_actionable(page):
    cases = await page.evaluate(
        """() => {
            const mk = (name, extra) => Object.assign(new Error('x'), { name, ...extra });
            return {
              denied: micErrorText(mk('NotAllowedError')),
              missing: micErrorText(mk('NotFoundError')),
              busy: micErrorText(mk('NotReadableError')),
              constrained: micErrorText(mk('OverconstrainedError', { constraint: 'channelCount' })),
            };
        }"""
    )
    assert "permission" in cases["denied"].lower()
    assert "no audio input" in cases["missing"].lower()
    assert "another application" in cases["busy"].lower()
    assert "channelCount" in cases["constrained"], (
        "OverconstrainedError carries the field that failed; the message does not")
    assert "could not be satisfied" not in json.dumps(cases), (
        "the browser's own wording is what made this unreportable")


# ── The device vanishing in the middle of a recording ────────────────────────

async def test_a_track_that_ends_is_reported_by_name(page):
    """The USB microphone fell off the bus mid-sentence. Nothing listened for
    `ended`, so the recorder kept running against a dead track: flat meter,
    button still lit, words simply stopping. What the user could say afterwards
    was "it just stopped, or it was already stopped, I can't tell."
    """
    seen = await page.evaluate(
        """async () => {
            const track = new EventTarget();
            track.label = 'CMTECK';
            const stream = { getAudioTracks: () => [track] };
            const out = [];
            watchMicTrack(stream, (label) => out.push(label));
            track.dispatchEvent(new Event('ended'));
            return out;
        }"""
    )
    assert seen == ["CMTECK"], "the device's own name is what the user recognises"


async def test_a_stream_with_no_audio_track_is_not_an_error(page):
    ok = await page.evaluate(
        """() => {
            try { watchMicTrack({ getAudioTracks: () => [] }, () => {}); return true; }
            catch (e) { return false; }
        }"""
    )
    assert ok


async def test_loss_is_announced_once_however_many_times_it_fires(page):
    count = await page.evaluate(
        """async () => {
            const track = new EventTarget();
            track.label = 'CMTECK';
            let n = 0;
            watchMicTrack({ getAudioTracks: () => [track] }, () => { n += 1; });
            track.dispatchEvent(new Event('ended'));
            track.dispatchEvent(new Event('ended'));
            return n;
        }"""
    )
    assert count == 1, "one disconnection, one notice"


async def test_an_unnamed_track_still_produces_a_sentence(page):
    """A device the browser will not name must not render as "undefined
    disconnected"."""
    label = await page.evaluate(
        """async () => {
            const track = new EventTarget();
            const out = [];
            watchMicTrack({ getAudioTracks: () => [track] }, (l) => out.push(l));
            track.dispatchEvent(new Event('ended'));
            return out[0];
        }"""
    )
    assert label and "undefined" not in label


async def test_dictation_stops_and_says_so_when_the_microphone_goes(page):
    """End to end through Dictation's own handler: it must leave the recorder
    stopped and put a notice in the transcript, not sit there looking live."""
    result = await page.evaluate(
        """async () => {
            const notices = [];
            const realNotice = window.appendNotice;
            window.appendNotice = (kind, text) => notices.push([kind, text]);
            const realStop = Dictation.stop;
            let stopped = 0;
            Dictation.stop = async () => { stopped += 1; Dictation.recording = false; return ''; };

            const track = new EventTarget();
            track.label = 'CMTECK';
            Dictation.recording = true;
            Dictation.watchForLoss({ getAudioTracks: () => [track] });
            track.dispatchEvent(new Event('ended'));
            await new Promise((r) => setTimeout(r, 0));

            Dictation.stop = realStop;
            window.appendNotice = realNotice;
            return { stopped, notices, recording: Dictation.recording };
        }"""
    )
    assert result["stopped"] == 1, "the recorder must actually be stopped"
    assert not result["recording"]
    assert result["notices"], "silence here is the whole bug"
    kind, text = result["notices"][0]
    assert kind == "error"
    assert "CMTECK" in text and "disconnected" in text


async def test_nothing_is_announced_if_dictation_was_not_running(page):
    """Stopping normally also ends the track. That is not a disconnection and
    must not be reported as one."""
    notices = await page.evaluate(
        """async () => {
            const out = [];
            const real = window.appendNotice;
            window.appendNotice = (k, t) => out.push(t);
            const track = new EventTarget();
            track.label = 'CMTECK';
            Dictation.recording = false;
            Dictation.starting = false;
            Dictation.watchForLoss({ getAudioTracks: () => [track] });
            track.dispatchEvent(new Event('ended'));
            await new Promise((r) => setTimeout(r, 0));
            window.appendNotice = real;
            return out;
        }"""
    )
    assert notices == [], f"a normal stop is not a disconnection: {notices}"


# ── The picker agrees with what will actually be opened ──────────────────────

async def test_the_picker_still_shows_the_chosen_microphone_after_an_id_rotates(page):
    """Otherwise the choice appears to have been forgotten, and the user re-picks
    a device that was never actually unset."""
    await page.evaluate(STUB, [TWO, ["aaa", "bbb"]])
    await page.evaluate(
        "() => { localStorage.clear(); saveMicDevice('yesterdays-id', 'Analog Stereo'); }")
    selected = await page.evaluate(
        """async () => {
            const sel = document.createElement('select');
            MicTest.els = MicTest.els || {};
            MicTest.els.device = sel;
            await MicTest.refreshDevices();
            return sel.value;
        }"""
    )
    assert selected == "bbb", "the picker should have re-pointed at the same hardware"
