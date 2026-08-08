DEFAULT_SYSTEM_PROMPT = "You are a coding agent. Respond ONLY to what the user asks. If the user greets you, greet back. Do NOT explore code, run commands, or make plans unless explicitly asked. Use tools to read, edit, write files, run shell commands, search code, and visually verify web UIs. Be concise."

VISUAL_VERIFY_PROMPT = "You are a coding agent. Respond ONLY to what the user asks. If the user greets you, greet back. Do NOT explore code or make plans unless asked. After making any UI changes, use the vision tool to screenshot and verify. Be concise. Read files before editing. Follow existing conventions."

MINIMAL_PROMPT = "You are a coding agent. Respond ONLY to what the user asks. Do not initiate actions. Be concise."

PROFILES = {
    "default": DEFAULT_SYSTEM_PROMPT,
    "visual-verify": VISUAL_VERIFY_PROMPT,
    "minimal": MINIMAL_PROMPT,
}

PROFILE_NAMES = list(PROFILES.keys())


def build_system_prompt(profile: str = "default") -> str:
    prompt = _get_profile_prompt(profile)
    user_prefs = _get_user_prefs()
    if user_prefs:
        prompt += "\n\nUser preferences:\n" + user_prefs
    return prompt


def _get_profile_prompt(profile: str) -> str:
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).resolve().parent.parent / "data" / "agent.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (f"profile_{profile}",)).fetchone()
            conn.close()
            if row and row[0].strip():
                return row[0]
    except Exception:
        pass
    return PROFILES.get(profile, DEFAULT_SYSTEM_PROMPT)


def _get_user_prefs() -> str:
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).resolve().parent.parent / "data" / "agent.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            row = conn.execute("SELECT value FROM settings WHERE key = 'user_prefs'").fetchone()
            conn.close()
            if row and row[0].strip():
                return row[0]
    except Exception:
        pass
    return ""
