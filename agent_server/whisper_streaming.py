"""Streaming dictation backed by whisper-server (whisper.cpp).

Whisper's architecture is non-streaming, so this is a sliding re-transcription:
the accumulated audio is re-transcribed every couple of seconds and the result
is pushed out as a partial. whisper runs far faster than realtime (especially on
the GPU), so the re-transcription keeps up. This gives whisper's accuracy with
live feedback.
"""

from __future__ import annotations

import asyncio
import io
import re
import wave

import httpx
import numpy as np

from agent_server.config import (
    WHISPER_MODEL,
    WHISPER_SERVER_BIN,
    WHISPER_SERVER_PORT,
    whisper_streaming_available,
)

SAMPLE_RATE = 16000
# Re-transcribe only once this much NEW audio has accumulated.
STEP_SECONDS = 1.5

# whisper emits these for silence/music/etc; they read as noise in the chat.
_NOISE = re.compile(
    r"\[(?:BLANK[ _]AUDIO|MUSIC|LAUGHTER|APPLAUSE|NOISE)\]\s*|\(\s*(?:silence|noise|music|speech)\s*\)",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _NOISE.sub("", text)).strip()


class WhisperStreamingError(RuntimeError):
    pass


class WhisperServer:
    """A persistent whisper-server process with the model already loaded."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.client: httpx.AsyncClient | None = None
        self.url = f"http://127.0.0.1:{WHISPER_SERVER_PORT}/inference"

    async def start(self) -> None:
        if self.proc is not None:
            return
        if not whisper_streaming_available():
            raise WhisperStreamingError("whisper-server is not installed")
        self.proc = await asyncio.create_subprocess_exec(
            WHISPER_SERVER_BIN,
            "-m", WHISPER_MODEL,
            "--host", "127.0.0.1",
            "--port", str(WHISPER_SERVER_PORT),
            "-l", "en",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Wait for the HTTP server to answer (the model loads in a few seconds).
        async with httpx.AsyncClient() as probe:
            for _ in range(150):
                if self.proc.returncode is not None:
                    raise WhisperStreamingError("whisper-server exited during startup")
                try:
                    # Any HTTP status means the socket is up; GET is 404 by design.
                    await probe.get(self.url, timeout=0.5)
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.2)
            else:
                raise WhisperStreamingError("whisper-server did not become ready")
        self.client = httpx.AsyncClient(timeout=60)

    async def transcribe(self, wav_bytes: bytes) -> str:
        if self.client is None:
            await self.start()
        assert self.client is not None
        resp = await self.client.post(
            self.url,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            data={"response_format": "json", "temperature": "0.0"},
        )
        resp.raise_for_status()
        text = resp.json().get("text", "").strip()
        return _clean(" ".join(text.split()))  # collapse whisper's line-wrapped output

    async def shutdown(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.kill()
        self.proc = None


_server: WhisperServer | None = None
_lock = asyncio.Lock()


async def get_server() -> WhisperServer:
    global _server
    if _server is None:
        async with _lock:
            if _server is None:
                _server = WhisperServer()
                await _server.start()
    return _server


async def shutdown() -> None:
    global _server
    if _server is not None:
        await _server.shutdown()
        _server = None


class WhisperSession:
    """One utterance: accumulates float32 samples, re-transcribes on demand."""

    def __init__(self, server: WhisperServer) -> None:
        self.server = server
        self._buf = bytearray()
        self._last_transcribed = 0
        self.busy = False

    def append(self, samples: np.ndarray) -> None:
        self._buf.extend(samples.astype(np.float32).tobytes())

    @property
    def new_seconds(self) -> float:
        n = len(self._buf) // 4
        return (n - self._last_transcribed) / SAMPLE_RATE

    def _to_wav(self) -> bytes:
        samples = np.frombuffer(self._buf, dtype=np.float32)
        pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm16.tobytes())
        return out.getvalue()

    async def transcribe(self) -> str:
        wav = self._to_wav()
        self._last_transcribed = len(self._buf) // 4
        return await self.server.transcribe(wav)
