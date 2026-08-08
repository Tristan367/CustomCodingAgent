import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "agent.db"


def _get_api_key(env_var: str, db_key: str) -> str:
    """Get API key: env var first, then DB-stored setting."""
    val = os.getenv(env_var, "")
    if val:
        return val
    # Lazy import to avoid circular imports
    try:
        from agent_server.database import get_setting
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Can't run async in already-running loop — return empty
            return ""
        return loop.run_until_complete(get_setting(db_key, ""))
    except Exception:
        return ""


DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_PROVIDER = "deepseek"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 8192

VISION_RIG_URL = os.getenv("VISION_RIG_URL", "")
