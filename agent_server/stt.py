"""One-shot speech-to-text: a recording in, text out.

The streaming path in `whisper_streaming.py` is what dictation actually uses.
This is the simpler one, for a recording that arrives whole.

It used to shell out twice -- ffmpeg to transcode the browser's WebM/Opus into
16 kHz mono WAV, then whisper-cli to read it -- which meant two binaries, two
subprocesses and a temporary directory per utterance. faster-whisper decodes the
container itself through the PyAV it already depends on, so both are gone.
"""

import asyncio
import re
import tempfile
from pathlib import Path

from agent_server import whisper_engine
from agent_server.config import whisper_model

MAX_AUDIO_BYTES = 100 * 1024 * 1024

# Whisper emits these for non-speech audio; they are noise in a text box.
_NOISE = re.compile(
    r"^\s*[\(\[\*][^)\]\*]{0,40}[\)\]\*]\s*$|^\s*(you|thanks for watching[.!]?|thank you[.!]?)\s*$",
    re.IGNORECASE,
)
# Whisper sometimes inserts bracket-delimited placeholders ([BLANK AUDIO],
# [inaudible], [music]). Nobody says brackets aloud, so strip what is inside.
_BRACKET = re.compile(r"\[[^\]]*\]")


class STTError(RuntimeError):
    pass


def availability() -> dict:
    engine = whisper_engine.loaded_engine()
    return {
        "available": whisper_engine.available(),
        "model": whisper_model(),
        "model_path": whisper_model(),
        "device": engine.device if engine else "",
        "compute_type": engine.compute_type if engine else "",
    }


async def transcribe(audio: bytes, suffix: str = ".webm") -> str:
    if not whisper_engine.available():
        raise STTError(
            "speech-to-text unavailable: faster-whisper is not installed "
            "(pip install faster-whisper)"
        )
    if not audio:
        raise STTError("empty audio")
    if len(audio) > MAX_AUDIO_BYTES:
        raise STTError(f"audio too large ({len(audio):,} bytes)")

    engine = await whisper_engine.get_engine(whisper_model())
    # A path rather than the bytes: faster-whisper accepts a file-like object,
    # but PyAV needs to seek to probe the container, and a browser recording is
    # a stream the decoder would otherwise have to buffer itself.
    with tempfile.TemporaryDirectory(prefix="codeagent-stt-") as tmp:
        path = Path(tmp) / f"input{suffix or '.webm'}"
        path.write_bytes(audio)
        try:
            text, _segments = await asyncio.wait_for(
                engine.transcribe(str(path)), timeout=300
            )
        except TimeoutError:
            raise STTError("transcription timed out") from None
        except Exception as e:
            raise STTError(f"transcription failed: {type(e).__name__}: {e}") from e

    return _clean(text)


def _clean(raw: str) -> str:
    raw = _BRACKET.sub("", raw)
    lines = [ln.strip() for ln in raw.splitlines()]
    kept = [ln for ln in lines if ln and not _NOISE.match(ln)]
    text = " ".join(kept)
    text = re.sub(r"\s+", " ", text).strip()
    return "" if _NOISE.match(text) else text
