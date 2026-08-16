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
