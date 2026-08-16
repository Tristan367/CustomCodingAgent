"""Streaming speech-to-text via sherpa-onnx zipformer.

The batch whisper path transcribes after the user stops talking; this one
transcribes as they speak. The browser captures 16 kHz mono float32 PCM and
streams it over a WebSocket; each chunk is fed to a streaming decoder and the
latest partial hypothesis is pushed back so the UI can render the trailing words
as provisional until they settle.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from agent_server.config import SHERPA_MODEL_DIR, streaming_stt_available

SAMPLE_RATE = 16000
# A short tail of silence flushed at the end lets the decoder emit the final
# partial frame (without it the very last syllable can be cut off).
_FINAL_PAD_SECONDS = 0.4


class StreamingSTTError(RuntimeError):
    pass


_recognizer = None
_load_lock = threading.Lock()


def _model_file(d: Path, kind: str) -> str:
    """Prefer the int8 build, fall back to fp32, so this works with any standard
    sherpa-onnx zipformer layout without hardcoding epoch numbers."""
    for pool in (sorted(d.glob(f"{kind}-*.int8.onnx")), sorted(d.glob(f"{kind}-*.onnx"))):
        if pool:
            return str(pool[0])
    raise StreamingSTTError(f"missing {kind} model in {d}")


def get_recognizer():
    """Load the model once, lazily, so a machine without it still runs the app."""
    global _recognizer
    if _recognizer is not None:
        return _recognizer
    if not streaming_stt_available():
        raise StreamingSTTError("streaming STT unavailable (set SHERPA_MODEL_DIR)")
    with _load_lock:
        if _recognizer is None:
            import sherpa_onnx

            d = Path(SHERPA_MODEL_DIR)
            _recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(d / "tokens.txt"),
                encoder=_model_file(d, "encoder"),
                decoder=_model_file(d, "decoder"),
                joiner=_model_file(d, "joiner"),
                num_threads=2,
                sample_rate=SAMPLE_RATE,
                feature_dim=80,
                decoding_method="greedy_search",
            )
    return _recognizer


class StreamingSession:
    """One utterance: feed float32 chunks, read back the running hypothesis."""

    def __init__(self, recognizer) -> None:
        self.recognizer = recognizer
        self.stream = recognizer.create_stream()

    def accept(self, samples: np.ndarray) -> str:
        """Feed 16 kHz float32 samples; return the partial text so far."""
        self.stream.accept_waveform(SAMPLE_RATE, samples)
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        return self.recognizer.get_result(self.stream)

    def finalize(self) -> str:
        """Flush a short tail of silence and return the completed text."""
        tail = np.zeros(int(_FINAL_PAD_SECONDS * SAMPLE_RATE), dtype=np.float32)
        self.stream.accept_waveform(SAMPLE_RATE, tail)
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        return self.recognizer.get_result(self.stream)

    def reset(self) -> None:
        self.recognizer.reset(self.stream)
