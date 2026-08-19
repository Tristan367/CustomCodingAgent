"""The speech-to-text engine: faster-whisper, in-process.

This replaced a `whisper-server` subprocess from whisper.cpp, spoken to over
HTTP on a local port. That worked, but it meant the app could not transcribe
anything until you had built or installed whisper.cpp system-wide, downloaded a
GGML file by hand, and pointed `WHISPER_MODEL` at it -- and it carried a process
lifecycle, a port, readiness polling and a WAV encode on every request for the
privilege. `pip install` and a model that downloads itself is a better deal for
everyone who is not already set up.

What it is *not* is a change to how streaming works. Whisper is a fixed-context
model with no streaming mode in any implementation, so the sliding window and
timestamp-based commit in `whisper_streaming.py` are still what turns it into
live dictation. Only the thing doing the inference changed.

CUDA, when there is one, is found rather than configured. ctranslate2 links
against CUDA 12, which is usually not what a current distribution ships, so the
libraries come from the `nvidia-*-cu12` wheels and are loaded explicitly here.
The documented alternative is exporting `LD_LIBRARY_PATH` before starting
Python, which cannot be done from inside the program it applies to and is the
single most common reason a GPU install falls back to the CPU without saying so.
"""

from __future__ import annotations

import asyncio
import ctypes
import glob
import logging
import os
import sysconfig
import threading

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# Sizes worth offering. Bigger is more accurate and slower; `.en` variants are
# English-only and better than the multilingual model of the same size at it.
MODEL_SIZES = (
    "tiny.en", "tiny", "base.en", "base", "small.en", "small",
    "medium.en", "medium", "large-v3", "large-v3-turbo",
)
DEFAULT_MODEL = "base.en"

# Which CUDA libraries to pull in, in dependency order.
_CUDA_LIBS = (
    "*/lib/libcublas*.so.12",
    "*/lib/libcublasLt*.so.12",
    "*/lib/libcudnn*.so.9",
)

_cuda_ready: bool | None = None


def _load_cuda_libraries() -> bool:
    """Put the wheel-shipped CUDA libraries in this process's namespace.

    `RTLD_GLOBAL` is the point: ctranslate2 dlopen's them by bare soname, and
    without this it searches the system loader path, where a distribution that
    ships CUDA 13 has no `libcublas.so.12` to offer.
    """
    global _cuda_ready
    if _cuda_ready is not None:
        return _cuda_ready
    root = os.path.join(sysconfig.get_paths()["purelib"], "nvidia")
    if not os.path.isdir(root):
        _cuda_ready = False
        return False
    for pattern in _CUDA_LIBS:
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            try:
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                log.debug("could not preload %s", path, exc_info=True)
    _cuda_ready = True
    return True


def available() -> bool:
    """Whether transcription can run at all."""
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _pick_device() -> tuple[str, str]:
    """(device, compute_type). CUDA when it is really usable, else CPU int8."""
    if os.getenv("WHISPER_DEVICE"):
        device = os.getenv("WHISPER_DEVICE", "cpu")
        return device, os.getenv("WHISPER_COMPUTE") or (
            "float16" if device == "cuda" else "int8"
        )
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0 and _load_cuda_libraries():
            return "cuda", "float16"
    except Exception:
        log.debug("no usable CUDA device", exc_info=True)
    return "cpu", "int8"


class WhisperEngine:
    """One loaded model, shared by every dictation session.

    faster-whisper is synchronous and holds the GIL only around the Python
    parts, so calls run in a worker thread. One lock around inference: the model
    is not re-entrant, and two sessions transcribing at once would interleave
    into the same CUDA context.
    """

    def __init__(self, model_name: str = "") -> None:
        self.model_name = model_name or DEFAULT_MODEL
        self.device = ""
        self.compute_type = ""
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        """Blocking: downloads the model on first use, then loads it."""
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        device, compute = _pick_device()
        try:
            self._model = WhisperModel(self.model_name, device=device, compute_type=compute)
        except Exception:
            if device == "cpu":
                raise
            # A CUDA build that loads but cannot actually run is worse than no
            # CUDA at all: it fails on the first utterance instead of at startup.
            log.warning("CUDA model load failed; falling back to CPU", exc_info=True)
            device, compute = "cpu", "int8"
            self._model = WhisperModel(self.model_name, device=device, compute_type=compute)
        self.device, self.compute_type = device, compute
        log.info(
            "whisper ready: %s on %s (%s)", self.model_name, self.device, self.compute_type
        )

    def _transcribe(self, audio: np.ndarray | str) -> tuple[str, list[dict]]:
        assert self._model is not None
        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language="en",
                # Greedy. Dictation is re-transcribed every step, so a beam
                # search is paid for several times over for the same words.
                beam_size=1,
                temperature=0.0,
                condition_on_previous_text=False,
                # Whisper's own hallucination guard on silence: without it, a
                # pause reliably produces "Thank you." or a subtitle credit.
                vad_filter=True,
            )
            out = []
            for seg in segments:
                out.append(
                    {"start": float(seg.start), "end": float(seg.end), "text": seg.text}
                )
        return " ".join(s["text"] for s in out).strip(), out

    async def transcribe(self, audio: np.ndarray | str) -> tuple[str, list[dict]]:
        """``(text, segments)`` for float32 mono 16 kHz samples, or a file path.

        The path form is what the one-shot endpoint uses: faster-whisper decodes
        the container itself, so a browser's WebM/Opus needs no transcode.
        """
        if self._model is None:
            await asyncio.to_thread(self.load)
        return await asyncio.to_thread(self._transcribe, audio)


_engine: WhisperEngine | None = None
_engine_lock = asyncio.Lock()


async def get_engine(model_name: str = "") -> WhisperEngine:
    """The shared engine, loading the model on first use."""
    global _engine
    wanted = model_name or DEFAULT_MODEL
    async with _engine_lock:
        if _engine is None or _engine.model_name != wanted:
            if _engine is not None:
                _engine = None
            engine = WhisperEngine(wanted)
            await asyncio.to_thread(engine.load)
            _engine = engine
        return _engine


def loaded_engine() -> WhisperEngine | None:
    return _engine


async def shutdown() -> None:
    """Release the model. ctranslate2 frees its GPU memory when it is dropped."""
    global _engine
    _engine = None


async def restart() -> None:
    """Drop the model so the next session picks up a newly chosen one."""
    await shutdown()
