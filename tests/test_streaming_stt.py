"""Streaming STT (sherpa-onnx). These run only when the model is installed."""

import wave
from pathlib import Path

import numpy as np
import pytest

from agent_server import streaming_stt

pytestmark = pytest.mark.skipif(
    not streaming_stt.streaming_stt_available(),
    reason="streaming STT model not installed (set SHERPA_MODEL_DIR)",
)


@pytest.fixture(scope="module")
def recognizer():
    return streaming_stt.get_recognizer()


def test_model_files_resolve(recognizer):
    d = Path(streaming_stt.SHERPA_MODEL_DIR)
    assert (d / "tokens.txt").exists()
    assert list(d.glob("encoder-*.onnx")) or list(d.glob("encoder-*.int8.onnx"))
    assert list(d.glob("decoder-*.onnx")) or list(d.glob("decoder-*.int8.onnx"))
    assert list(d.glob("joiner-*.onnx")) or list(d.glob("joiner-*.int8.onnx"))


def test_streaming_session_transcribes(recognizer):
    wav = Path(streaming_stt.SHERPA_MODEL_DIR) / "test_wavs" / "0.wav"
    with wave.open(str(wav), "rb") as w:
        samples = (
            np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
            / 32768.0
        )

    session = streaming_stt.StreamingSession(recognizer)
    for i in range(0, len(samples), 8000):
        session.accept(samples[i : i + 8000])
    text = session.finalize()
    session.reset()

    assert "nightfall" in text.lower()
    assert "brothels" in text.lower()
    assert text != text.upper(), "dictation must not come back in ALL CAPS"


def test_normalize_case():
    assert streaming_stt.normalize_case("AFTER EARLY NIGHTFALL, I SAW IT.") == "After early nightfall, I saw it."
    assert streaming_stt.normalize_case("HELLO WORLD") == "Hello world"
