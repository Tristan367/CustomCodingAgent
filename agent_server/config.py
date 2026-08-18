"""Static configuration. Runtime-mutable settings live in the `settings` DB table."""

import os
import shutil
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    """User data lives outside the checkout.

    The database holds API keys and every conversation. Keeping it in the
    working tree means one `git clean -xdf` destroys it, and it only stays out
    of a public repository for as long as nobody edits .gitignore. XDG puts it
    where the rest of the user's application data already is, and where a
    backup tool will find it.
    """
    if os.name == "nt":
        base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "codeagent"


# Overridable so the app can be run against a scratch database -- smoke-testing
# a change otherwise means pointing it at the real conversation history.
DATA_DIR = Path(os.getenv("CODEAGENT_DATA_DIR") or _default_data_dir())
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("CODEAGENT_DB") or DATA_DIR / "agent.db")

# One-time move from the old in-repo location. Only runs when the destination
# is empty, so it cannot overwrite a database the user is already using, and it
# copies rather than moves the sqlite sidecars so an interrupted run is
# recoverable from the original.
_LEGACY_DB = BASE_DIR / "data" / "agent.db"
if not DB_PATH.exists() and _LEGACY_DB.exists() and not os.getenv("CODEAGENT_DATA_DIR"):
    for suffix in ("", "-wal", "-shm"):
        source = _LEGACY_DB.with_name(_LEGACY_DB.name + suffix)
        if source.exists():
            shutil.copy2(source, DB_PATH.with_name(DB_PATH.name + suffix))
    print(f"[config] moved database to {DB_PATH} (old copy left at {_LEGACY_DB})")  # runs at import time, before logging is configured

# tempfile.gettempdir() rather than "/tmp": the screen-capture backends are
# chosen per platform, so the app claims to run on Windows, where /tmp is not
# a path.
_TMP = Path(tempfile.gettempdir())
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR") or _TMP / "codeagent_uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Frames written by `browser` and `capture`, read back by `vision`.
CAPTURE_DIR = Path(os.getenv("CODEAGENT_CAPTURE_DIR") or _TMP / "codeagent_captures")
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

# Cookies/localStorage saved by `browser`, so a login survives the context being
# reaped and the app restarting. Lives under DATA_DIR because it must persist,
# unlike CAPTURE_DIR which is fine to lose on reboot.
BROWSER_STATE_DIR = Path(os.getenv("CODEAGENT_BROWSER_STATE_DIR") or DATA_DIR / "browser_state")
BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)

# ── Models ──────────────────────────────────────────────────────────────────
# Context/limits per https://api-docs.deepseek.com/quick_start/pricing
# (no price tracking: provider rates move and DeepSeek even varies by time of day)
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
    },
    {
        "id": "deepseek-v4-flash",
        "name": "DeepSeek V4 Flash",
        "provider": "deepseek",
        "context": 1_000_000,
    },
    {
        "id": "anthropic/claude-sonnet-4-20250514",
        "name": "Claude Sonnet 4",
        "provider": "openrouter",
        "context": 200_000,
    },
    {
        "id": "openai/gpt-4.1",
        "name": "GPT-4.1",
        "provider": "openrouter",
        "context": 1_000_000,
    },
    {
        "id": "google/gemini-2.5-pro",
        "name": "Gemini 2.5 Pro",
        "provider": "openrouter",
        "context": 1_000_000,
    },
    {
        "id": "meta-llama/llama-4-maverick",
        "name": "Llama 4 Maverick",
        "provider": "openrouter",
        "context": 1_000_000,
    },
    # Anthropic, per platform.claude.com/docs/en/about-claude/models/overview,
    # checked 2026-08-10. Opus's context and output ceiling had both been wrong
    # by a factor of five, which fed straight into the compaction threshold.
    {
        "id": "claude-fable-5",
        "name": "Claude Fable 5",
        "provider": "anthropic",
        "context": 1_000_000,
        "max_output": 128_000,
    },
    {
        "id": "claude-opus-5",
        "name": "Claude Opus 5",
        "provider": "anthropic",
        "context": 1_000_000,
        "max_output": 128_000,
    },
    {
        "id": "claude-sonnet-5",
        "name": "Claude Sonnet 5",
        "provider": "anthropic",
        "context": 1_000_000,
        "max_output": 128_000,
    },
    {
        "id": "claude-haiku-4-5",
        "name": "Claude Haiku 4.5",
        "provider": "anthropic",
        "context": 200_000,
        "max_output": 64_000,
    },
]

MODELS_BY_ID = {m["id"]: m for m in MODELS}

# DeepSeek model ids discovered from the /models endpoint at startup. They carry
# no context metadata (the endpoint returns ids only), so they fall through to
# UNKNOWN_MODEL for sizing. Refreshed on every start so a newly-released model
# appears without a code change, and never touched for local/custom endpoints
# (which are queried only for what the operator serves).
DYNAMIC_DEEPSEEK_MODELS: list[str] = []


def register_dynamic_deepseek_models(ids: list[str]) -> None:
    """Record ids the DeepSeek /models endpoint returned, minus ones already
    listed by hand in MODELS."""
    for mid in ids:
        if mid and mid not in MODELS_BY_ID and mid not in DYNAMIC_DEEPSEEK_MODELS:
            DYNAMIC_DEEPSEEK_MODELS.append(mid)


def is_known_model(model_id: str) -> bool:
    """Whether a session can be created for this id (built-in or discovered)."""
    return model_id in MODELS_BY_ID or model_id in DYNAMIC_DEEPSEEK_MODELS


def dynamic_deepseek_models() -> list[dict]:
    """Discovered DeepSeek models as offerable entries, humanised for the UI."""
    return [
        {"id": mid, "name": _humanize_model_id(mid), "provider": "deepseek"}
        for mid in DYNAMIC_DEEPSEEK_MODELS
    ]


def _humanize_model_id(mid: str) -> str:
    return " ".join("DeepSeek" if t == "deepseek" else t.capitalize() for t in mid.split("-"))

# What a model whose context we do not know is assumed to hold. A custom
# endpoint can serve anything, so the honest answer is "unknown"; these keep the
# context ring from reading as an authoritative zero.
UNKNOWN_MODEL = {
    "context": 131_072,
    "max_output": 8_192,
}

DEFAULT_MAX_OUTPUT = 8_192


def model_info(model_id: str) -> dict:
    """Context window and output ceiling, or the unknown defaults."""
    entry = MODELS_BY_ID.get(model_id)
    if not entry:
        return {**UNKNOWN_MODEL, "id": model_id}
    return {"max_output": DEFAULT_MAX_OUTPUT, **entry}


def provider_for_model(model_id: str) -> str:
    """Which provider serves this model.

    The provider is a property of the model, not a separate choice. Recording
    them independently is how a session came to hold `claude-opus-5` alongside
    `provider="deepseek"`: the creation form had a Model dropdown, no provider
    field at all, and the database default filled in the rest.
    """
    entry = MODELS_BY_ID.get(model_id)
    return entry["provider"] if entry else DEFAULT_PROVIDER


def resolve_model_choice(choice: str, custom_model: str = "") -> tuple[str, str]:
    """Turn the Model dropdown's value into a (provider, model) pair.

    Built-in models post their own id. A custom endpoint posts `custom:NAME`
    and carries the model id in a free-text field beside it, because only the
    endpoint's operator knows what it serves.
    """
    choice = (choice or "").strip()
    if choice.startswith("custom:"):
        model = custom_model.strip()
        if not model:
            raise ValueError("Type the model id the custom endpoint expects.")
        return choice, model
    if not is_known_model(choice):
        raise ValueError(f"Unknown model: {choice}")
    return provider_for_model(choice), choice

# Offer compaction once a session's live context passes this many tokens.
# Overridable per session; the ceiling is the model's context window.
COMPACT_THRESHOLD_TOKENS = int(os.getenv("COMPACT_THRESHOLD_TOKENS", "262144"))
MIN_COMPACT_THRESHOLD = 4096

# Slider stops offered in the UI: powers of two from 4K to 1M.
THRESHOLD_STEPS = [4096 * 2 ** i for i in range(8)] + [1_000_000]

# Warn before a request throws away this many previously cached tokens. At the
# miss rate a cached prefix is ~120x more expensive to re-read, so a large
# accidental invalidation is worth a confirmation rather than a surprise.
CACHE_WARN_TOKENS = int(os.getenv("CACHE_WARN_TOKENS", "25000"))

# Safety rails on the agent loop.
MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "50000"))

# ── Subagents ────────────────────────────────────────────────────────────────
# A subagent runs until it answers or is cancelled by the user stopping the run.
# There is no round cap or timeout: cutting a slow-but-productive agent off
# mid-task throws away the work it has already done. Thinking effort is not
# forced here either -- a subagent inherits the parent's effort, or the model's
# default when it runs on a different model.

# webfetch
WEBFETCH_TIMEOUT = int(os.getenv("WEBFETCH_TIMEOUT", "30"))
WEBFETCH_MAX_BYTES = int(os.getenv("WEBFETCH_MAX_BYTES", "5000000"))
# Block requests to the local machine and private networks. The agent's own API
# lives on localhost, so an unfiltered fetch can drive this app through its own
# tool. Set to 0 only if you need the agent to reach an internal service.
WEBFETCH_ALLOW_PRIVATE = os.getenv("WEBFETCH_ALLOW_PRIVATE", "0") == "1"



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


_whisper_model = _find_whisper_model()


def whisper_model() -> str:
    """The active whisper model path: env/settings, seeded at startup."""
    return _whisper_model


def set_whisper_model(value: str) -> None:
    """Switch the active model; empty falls back to env/auto-detection."""
    global _whisper_model
    _whisper_model = (value or "").strip() or _find_whisper_model()


def list_whisper_models() -> list[str]:
    """GGML models in the usual locations, for the settings dropdown."""
    dirs = [
        Path.home() / "opt/whisper.cpp/models",
        Path.home() / "models/stt",
    ]
    found: list[str] = []
    seen: set[str] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.bin")):
            s = str(p)
            if s not in seen:
                seen.add(s)
                found.append(s)
    current = whisper_model()
    if current and current not in seen:
        found.insert(0, current)
    return found


FFMPEG_BIN = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg")
WHISPER_SERVER_BIN = os.getenv("WHISPER_SERVER_BIN") or shutil.which("whisper-server")
WHISPER_SERVER_PORT = int(os.getenv("WHISPER_SERVER_PORT", "8177"))


def stt_available() -> bool:
    return bool(WHISPER_BIN and whisper_model() and FFMPEG_BIN)


def whisper_streaming_available() -> bool:
    """whisper-server (whisper.cpp) for accurate streaming dictation."""
    return bool(WHISPER_SERVER_BIN and whisper_model())

