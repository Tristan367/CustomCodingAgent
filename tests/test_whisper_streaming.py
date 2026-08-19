"""Streaming dictation: the sliding window that makes a non-streaming model live.

Whisper has no streaming mode in any implementation, so what turns it into live
dictation is here rather than in the engine -- re-transcribe the recent tail,
commit what is old enough to be stable, and trim it out of the buffer so the
next pass stays the same size.
"""

import numpy as np

from agent_server import whisper_streaming


def test_the_buffer_reads_back_as_the_samples_that_went_in():
    """It is handed to the engine as-is now. The whisper-server backend needed a
    WAV encode here on every step -- header, int16 conversion, a fresh BytesIO --
    purely to cross an HTTP boundary that no longer exists."""
    session = whisper_streaming.WhisperSession(engine=None)
    samples = np.array([0.0, 0.5, -0.5, 1.0], dtype=np.float32)
    session.append(samples)
    assert np.array_equal(session._samples(), samples)


def test_new_seconds_tracks_untrancribed_audio():
    session = whisper_streaming.WhisperSession(engine=None)
    session.append(np.zeros(16000, dtype=np.float32))  # one second
    assert session.new_seconds == 1.0
    session._last_transcribed = 16000
    assert session.new_seconds == 0.0


def test_clean_strips_noise_markers():
    assert whisper_streaming._clean("hello [BLANK_AUDIO] world") == "hello world"
    assert whisper_streaming._clean("[MUSIC] [INAUDIBLE] hi") == "hi"
    assert whisper_streaming._clean("then (wind blowing) it stopped") == "then it stopped"
    assert whisper_streaming._clean("normal speech") == "normal speech"


def test_clean_removes_em_dash_but_keeps_flags():
    assert whisper_streaming._clean("one -- two") == "one two"
    assert whisper_streaming._clean("run ls --help") == "run ls --help"


def test_clean_inserts_space_after_sentence_punctuation():
    assert whisper_streaming._clean("done.Next") == "done. Next"
    assert whisper_streaming._clean("keep 3.14 and foo.py") == "keep 3.14 and foo.py"


def test_clean_strips_space_before_punctuation():
    assert whisper_streaming._clean("worth it even ?") == "worth it even?"
    assert whisper_streaming._clean("hello , world") == "hello, world"


def test_clean_strips_silence_token():
    assert whisper_streaming._clean("[silence]") == ""
    assert whisper_streaming._clean("turn it on [silence] now") == "turn it on now"


def test_ensure_period():
    ensure = whisper_streaming.WhisperSession._ensure_period
    assert ensure("hello world") == "hello world."
    assert ensure("hello world!") == "hello world!"
    assert ensure("  ") == ""


def test_pause_finalization_requires_long_silence():
    session = whisper_streaming.WhisperSession(engine=None)
    session.append(np.full(16000, 0.5, dtype=np.float32))  # 1s of loud speech
    assert not session.should_finalize
    session.append(np.zeros(int((whisper_streaming.PAUSE_SECONDS + 1) * 16000), dtype=np.float32))
    assert session.should_finalize


class _FakeEngine:
    """Returns canned segments instead of running a model."""

    def __init__(self, segments):
        self._segments = segments

    async def transcribe(self, samples):
        text = " ".join(s["text"] for s in self._segments)
        return text, [dict(s) for s in self._segments]


def _session_with(segments, seconds):
    session = whisper_streaming.WhisperSession(engine=None)
    session.engine = _FakeEngine(segments)
    session.append(np.full(int(16000 * seconds), 0.5, dtype=np.float32))
    return session


async def test_partial_commits_old_segments_and_trims(monkeypatch):
    segs = [
        {"start": 0.0, "end": 2.0, "text": "first sentence."},
        {"start": 2.1, "end": 5.0, "text": "second sentence."},
        {"start": 5.1, "end": 9.0, "text": "still under review."},
    ]
    monkeypatch.setattr(whisper_streaming, "COMMIT_DELAY_SEC", 6.0)
    # 12s of audio, 6s commit delay: anything ending before 6.0s commits.
    session = _session_with(segs, seconds=12)
    partial = await session.current_partial()
    assert session.finalized_text() == "first sentence. second sentence."
    assert partial == "still under review."
    # The last committed segment ended at 5.0s, so the buffer is trimmed to it.
    assert len(session._buf) // 4 == 7 * 16000


async def test_partial_keeps_recent_audio_under_review():
    segs = [{"start": 0.0, "end": 2.0, "text": "hello world."}]
    session = _session_with(segs, seconds=3)
    partial = await session.current_partial()
    assert session.finalized_text() == ""
    assert partial == "hello world."
    assert len(session._buf) // 4 == 3 * 16000  # nothing trimmed
