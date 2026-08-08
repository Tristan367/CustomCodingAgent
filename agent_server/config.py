"""Static configuration. Runtime-mutable settings live in the `settings` DB table."""

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "agent.db"

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/codeagent_uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── Models ──────────────────────────────────────────────────────────────────
# Context/limits per https://api-docs.deepseek.com/quick_start/pricing
DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_THINKING_EFFORT = "high"

# reasoning_effort enum accepted by the DeepSeek API.
REASONING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]

MODELS = [
    {
        "id": "deepseek-v4-pro",
        "name": "DeepSeek V4 Pro",
        "provider": "deepseek",
        "context": 1_000_000,
        # USD per 1M tokens
        "price_in_hit": 0.003625,
        "price_in_miss": 0.435,
        "price_out": 0.87,
    },
    {
        "id": "deepseek-v4-flash",
        "name": "DeepSeek V4 Flash",
        "provider": "deepseek",
        "context": 1_000_000,
        "price_in_hit": 0.0028,
        "price_in_miss": 0.14,
        "price_out": 0.28,
    },
]

MODELS_BY_ID = {m["id"]: m for m in MODELS}

# Offer compaction once a session's live context passes this many tokens.
# Overridable per session; the ceiling is the model's context window.
COMPACT_THRESHOLD_TOKENS = int(os.getenv("COMPACT_THRESHOLD_TOKENS", "262144"))
MIN_COMPACT_THRESHOLD = 4096

# Slider stops offered in the UI: powers of two from 4K to 1M.
THRESHOLD_STEPS = [4096 * 2 ** i for i in range(8)] + [1_000_000]

# Safety rails on the agent loop.
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "40"))
MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "50000"))

# ── Vision ──────────────────────────────────────────────────────────────────
# Subagents
SUBAGENT_MAX_ROUNDS = int(os.getenv("SUBAGENT_MAX_ROUNDS", "20"))
SUBAGENT_TIMEOUT = int(os.getenv("SUBAGENT_TIMEOUT", "600"))
SUBAGENT_EFFORT = os.getenv("SUBAGENT_EFFORT", "low")

# webfetch
WEBFETCH_TIMEOUT = int(os.getenv("WEBFETCH_TIMEOUT", "30"))
WEBFETCH_MAX_BYTES = int(os.getenv("WEBFETCH_MAX_BYTES", "5000000"))
# Block requests to the local machine and private networks. The agent's own API
# lives on localhost, so an unfiltered fetch can drive this app through its own
# tool. Set to 0 only if you need the agent to reach an internal service.
WEBFETCH_ALLOW_PRIVATE = os.getenv("WEBFETCH_ALLOW_PRIVATE", "0") == "1"

VISION_OLLAMA_URL = os.getenv("VISION_OLLAMA_URL", "http://vision-host.local:11434")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen3-vl:32b")
VISION_TIMEOUT = int(os.getenv("VISION_TIMEOUT", "300"))
# Ollama unloads an idle model after five minutes by default. Reloading a 32B
# vision model costs about ten seconds, which dwarfs the two seconds the actual
# inference takes, so hold it in memory between calls.
VISION_KEEP_ALIVE = os.getenv("VISION_KEEP_ALIVE", "30m")
# Bring the rig up on demand over SSH, so the only thing to switch on by hand is
# the machine itself. Empty disables it and vision just reports being offline.
# Requires key-based SSH; the binary is started as your user, no sudo involved.
VISION_SSH_HOST = os.getenv("VISION_SSH_HOST", "you@vision-host.local")
VISION_REMOTE_BIN = os.getenv("VISION_REMOTE_BIN", "~/.local/bin/ollama")
VISION_AUTOSTART = os.getenv("VISION_AUTOSTART", "1") == "1"
# How long to wait for it to come up before giving up on a request.
VISION_START_TIMEOUT = int(os.getenv("VISION_START_TIMEOUT", "45"))
# Must be identical on every request: Ollama reloads the model whenever the
# options change, which would reintroduce the cold start it is meant to avoid.
VISION_NUM_CTX = int(os.getenv("VISION_NUM_CTX", "8192"))
# Downscale anything larger before sending; phone photos are needlessly huge.
VISION_MAX_PIXELS = int(os.getenv("VISION_MAX_PIXELS", str(1600 * 1600)))

WHISPER_BIN = os.getenv("WHISPER_BIN") or shutil.which("whisper-cli") or shutil.which("whisper")


def _find_whisper_model() -> str:
    if os.getenv("WHISPER_MODEL"):
        return os.getenv("WHISPER_MODEL", "")
    candidates = [
        Path.home() / "opt/whisper.cpp/models/ggml-base.en.bin",
        Path.home() / "models/stt/ggml-base.en.bin",
        Path.home() / "opt/whisper.cpp/models/ggml-tiny.en.bin",
        Path.home() / "models/stt/ggml-tiny.en-q8_0.bin",
        Path.home() / "models/stt/ggml-tiny.en-q4_1.bin",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return ""


WHISPER_MODEL = _find_whisper_model()
FFMPEG_BIN = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg")


def stt_available() -> bool:
    return bool(WHISPER_BIN and WHISPER_MODEL and FFMPEG_BIN)
