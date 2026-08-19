"""Streaming dictation on top of a non-streaming model.

Whisper has no streaming mode in any implementation, so this is a sliding
re-transcription:
the accumulated audio is re-transcribed every couple of seconds and the result
is pushed out as a partial. whisper runs far faster than realtime (especially on
the GPU), so the re-transcription keeps up.

To stop latency growing with a long utterance, the buffer is bounded the way
whisper.cpp's own stream example bounds it: audio older than a fixed window is
finalised and trimmed, so each re-transcription only covers the recent tail.
The cut uses whisper's segment timestamps, and enough trailing audio is kept
under review that whisper can still revise the last sentence or two.
"""

from __future__ import annotations

import os
import re

import numpy as np

from agent_server.whisper_engine import SAMPLE_RATE

# Re-transcribe only once this much NEW audio has accumulated.
STEP_SECONDS = 1.5
# A pause this long (with speech before it) commits the sentence and adds a
# period, so dictation can stay on and each burst of talking becomes a sentence.
PAUSE_SECONDS = float(os.getenv("CODEAGENT_DICTATION_PAUSE", "10"))
# Audio older than this is committed and trimmed, so the re-transcription only
# ever covers the recent tail. Sized to keep the last couple of sentences under
# review, since whisper sometimes revises them when more context arrives. Ten
# seconds matches whisper.cpp's own stream example window (--length 10000).
COMMIT_DELAY_SEC = float(os.getenv("CODEAGENT_DICTATION_COMMIT_DELAY", "10"))

# whisper emits these for silence/background noise; they read as garbage in the
# transcript. The first arm catches the special-event tokens ([BLANK_AUDIO],
# [MUSIC], [INAUDIBLE], and the lowercase [silence]); the second catches sound
# descriptions such as "(wind blowing)" and "(music)".
_NOISE = re.compile(
    r"\[[A-Za-z_ ]+\]"         # whisper's event tokens, upper- or lowercase
    r"|\([a-z]+(?: [a-z]+)*\)"  # lowercase sound descriptions
)
# A standalone "--" is whisper's way of writing an em-dash; dictation doesn't
# want it. Word-boundary anchored so a flag like --help survives.
_DASH = re.compile(r"(?<!\S)--(?!\S)")
# whisper sometimes runs a sentence straight into the next ("done.Next").
_MISSING_SPACE = re.compile(r"([.!?])([A-Z])")
# whisper sometimes leaves a space before punctuation ("even ?").
_SPACE_PUNCT = re.compile(r"\s+([,.;:!?])")


def _clean(text: str) -> str:
    text = _NOISE.sub(" ", text)
    text = _DASH.sub(" ", text)
    text = _MISSING_SPACE.sub(r"\1 \2", text)
    text = _SPACE_PUNCT.sub(r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


class WhisperStreamingError(RuntimeError):
    pass


class WhisperSession:
    """One continuous dictation session.

    Re-transcribes the in-progress audio every couple of seconds for live
    feedback, and -- so the user can leave dictation on and just talk -- detects
    a long pause and commits the speech before it as a finished sentence (adding
    a period if whisper did not). Committed sentences are dropped from the audio
    buffer, so the re-transcription never grows without bound.
    """

    def __init__(self, engine) -> None:
        self.engine = engine
        self._buf = bytearray()          # audio since the last committed sentence
        self._finalized: list[str] = []  # committed sentences
        self._silence = 0.0              # seconds of trailing silence
        self._speech = False             # whether the current buffer has speech
        self._peak = 0.0                 # slowly-decaying recent RMS
        self._last_transcribed = 0
        self.busy = False

    def append(self, samples: np.ndarray) -> None:
        self._buf.extend(samples.astype(np.float32).tobytes())
        if not samples.size:
            return
        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        # Adaptive silence: anything well under the recent speech level counts as
        # a pause, with a floor so a genuinely quiet mic still has a threshold.
        self._peak = max(self._peak * 0.999, rms)
        if rms < max(0.008, self._peak * 0.2):
            self._silence += samples.size / SAMPLE_RATE
        else:
            self._silence = 0.0
            self._speech = True

    @property
    def new_seconds(self) -> float:
        return (len(self._buf) // 4 - self._last_transcribed) / SAMPLE_RATE

    @property
    def should_finalize(self) -> bool:
        return self._speech and self._silence >= PAUSE_SECONDS

    def finalized_text(self) -> str:
        return " ".join(self._finalized)

    def _samples(self) -> np.ndarray:
        """The buffer as float32 mono, which is what the engine takes.

        The whisper-server backend needed a WAV encode here on every step --
        header, int16 conversion, a fresh BytesIO -- purely to hand the audio
        over an HTTP boundary that no longer exists.
        """
        return np.frombuffer(bytes(self._buf), dtype=np.float32)

    async def current_partial(self) -> str:
        """Transcribe the in-progress buffer; empty if nothing has been said.

        Finalises and trims any audio old enough to be stable, so the buffer
        never grows past the review window and each transcription stays fast.
        Returns only the text still under review.
        """
        if not self._speech:
            return ""
        text, segments = await self.engine.transcribe(self._samples())
        segments = [
            {**s, "text": _clean(s["text"])} for s in segments
        ]
        text = _clean(text)
        buf_sec = len(self._buf) // 4 / SAMPLE_RATE
        cutoff = buf_sec - COMMIT_DELAY_SEC
        commit_idx = 0
        for i, seg in enumerate(segments):
            if seg["end"] <= cutoff:
                commit_idx = i + 1
            else:
                break
        if commit_idx:
            committed = [s["text"] for s in segments[:commit_idx] if s["text"]]
            if committed:
                self._finalized.append(" ".join(committed))
            trim = min(int(segments[commit_idx - 1]["end"] * SAMPLE_RATE), len(self._buf) // 4)
            if trim > 0:
                del self._buf[: trim * 4]
            remaining = " ".join(s["text"] for s in segments[commit_idx:] if s["text"])
        else:
            remaining = text
        self._last_transcribed = len(self._buf) // 4
        return remaining

    @staticmethod
    def _ensure_period(text: str) -> str:
        text = text.strip()
        if text and text[-1] not in ".!?":
            text += "."
        return text

    async def commit_pause(self) -> bool:
        """Finalize the buffer as a finished sentence and reset it."""
        text = self._ensure_period(await self.current_partial())
        if text:
            self._finalized.append(text)
        self._reset()
        return bool(text)

    async def finalize(self) -> str:
        """Commit whatever remains and return the whole utterance."""
        text = self._ensure_period(await self.current_partial())
        if text:
            self._finalized.append(text)
        self._reset()
        return self.finalized_text()

    def _reset(self) -> None:
        self._buf = bytearray()
        self._silence = 0.0
        self._speech = False
        self._peak = 0.0
        self._last_transcribed = 0
