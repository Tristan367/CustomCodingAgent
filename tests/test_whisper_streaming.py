"""Streaming dictation via whisper-server (whisper.cpp)."""

import io
import wave

import numpy as np

from agent_server import whisper_streaming


def test_whisper_session_encodes_a_valid_wav():
    session = whisper_streaming.WhisperSession(server=None)
    session.append(np.array([0.0, 0.5, -0.5, 1.0], dtype=np.float32))
    wav = session._to_wav()
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 4


def test_new_seconds_tracks_untrancribed_audio():
    session = whisper_streaming.WhisperSession(server=None)
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
    session = whisper_streaming.WhisperSession(server=None)
    session.append(np.full(16000, 0.5, dtype=np.float32))  # 1s of loud speech
    assert not session.should_finalize
    session.append(np.zeros(int((whisper_streaming.PAUSE_SECONDS + 1) * 16000), dtype=np.float32))
    assert session.should_finalize
