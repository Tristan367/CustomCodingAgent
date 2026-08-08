import aiosqlite
import json
from datetime import datetime, timezone
from agent_server.config import DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            project_dir TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'deepseek',
            model TEXT NOT NULL DEFAULT 'deepseek-v4-pro',
            temperature REAL DEFAULT 0.0,
            thinking_effort TEXT,
            system_prompt_override TEXT,
            prompt_profile TEXT DEFAULT 'default',
            bash_auto_approve INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_active_at TEXT NOT NULL,
            is_archived INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_calls TEXT,
            tool_call_id TEXT,
            reasoning_content TEXT,
            token_count INTEGER,
            created_at TEXT NOT NULL,
            is_compacted INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS compactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            summary_text TEXT NOT NULL,
            message_range_start INTEGER,
            message_range_end INTEGER,
            original_token_count INTEGER,
            compressed_token_count INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_compactions_session ON compactions(session_id);
    """)

    # Migration: add prompt_profile if missing
    cols = await db.execute("PRAGMA table_info(sessions)")
    col_names = [row[1] for row in await cols.fetchall()]
    if "prompt_profile" not in col_names:
        await db.execute("ALTER TABLE sessions ADD COLUMN prompt_profile TEXT DEFAULT 'default'")

    if "bash_auto_approve" not in col_names:
        await db.execute("ALTER TABLE sessions ADD COLUMN bash_auto_approve INTEGER DEFAULT 0")

    # Migration: add reasoning_content to messages
    msg_cols = await db.execute("PRAGMA table_info(messages)")
    msg_col_names = [row[1] for row in await msg_cols.fetchall()]
    if "reasoning_content" not in msg_col_names:
        await db.execute("ALTER TABLE messages ADD COLUMN reasoning_content TEXT")

    await db.commit()
    await db.close()


# ── Session CRUD ──

async def create_session(name: str, project_dir: str, provider: str = "deepseek",
                         model: str = "deepseek-v4-pro", prompt_profile: str = "default") -> dict:
    import uuid
    sid = str(uuid.uuid4())[:8]
    now = _now()
    db = await get_db()
    await db.execute(
        "INSERT INTO sessions (id, name, project_dir, provider, model, prompt_profile, created_at, last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, name, project_dir, provider, model, prompt_profile, now, now),
    )
    await db.commit()
    await db.close()
    return await get_session(sid)


async def get_session(session_id: str) -> dict | None:
    db = await get_db()
    row = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    r = await row.fetchone()
    await db.close()
    return dict(r) if r else None


async def list_sessions(archived: bool = False) -> list[dict]:
    db = await get_db()
    rows = await db.execute(
        "SELECT * FROM sessions WHERE is_archived = ? ORDER BY last_active_at DESC",
        (1 if archived else 0,),
    )
    results = [dict(r) for r in await rows.fetchall()]
    await db.close()
    return results


async def update_session(session_id: str, **kwargs) -> dict | None:
    if not kwargs:
        return await get_session(session_id)
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [session_id]
    db = await get_db()
    await db.execute(f"UPDATE sessions SET {set_clause} WHERE id = ?", values)
    await db.commit()
    await db.close()
    return await get_session(session_id)


async def touch_session(session_id: str):
    db = await get_db()
    await db.execute("UPDATE sessions SET last_active_at = ? WHERE id = ?", (_now(), session_id))
    await db.commit()
    await db.close()


async def delete_session(session_id: str):
    db = await get_db()
    await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    await db.commit()
    await db.close()


# ── Message CRUD ──

async def add_message(session_id: str, role: str, content: str, tool_calls: str | None = None,
                      tool_call_id: str | None = None, reasoning_content: str | None = None,
                      token_count: int | None = None) -> dict:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, reasoning_content, token_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, role, content, tool_calls, tool_call_id, reasoning_content, token_count, _now()),
    )
    msg_id = cursor.lastrowid
    await db.commit()
    await db.close()
    await touch_session(session_id)
    row = await _get_message_by_id(msg_id)
    return row


async def _get_message_by_id(msg_id: int) -> dict:
    db = await get_db()
    row = await db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,))
    r = await row.fetchone()
    await db.close()
    return dict(r)


async def get_messages(session_id: str, include_compacted: bool = False) -> list[dict]:
    db = await get_db()
    if include_compacted:
        rows = await db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )
    else:
        rows = await db.execute(
            "SELECT * FROM messages WHERE session_id = ? AND is_compacted = 0 ORDER BY created_at ASC",
            (session_id,),
        )
    results = [dict(r) for r in await rows.fetchall()]
    await db.close()
    return results


async def mark_messages_compacted(session_id: str, message_ids: list[int]):
    db = await get_db()
    placeholders = ",".join("?" * len(message_ids))
    await db.execute(
        f"UPDATE messages SET is_compacted = 1 WHERE session_id = ? AND id IN ({placeholders})",
        [session_id] + message_ids,
    )
    await db.commit()
    await db.close()


async def get_message_count(session_id: str) -> int:
    db = await get_db()
    row = await db.execute("SELECT COUNT(*) as c FROM messages WHERE session_id = ?", (session_id,))
    r = await row.fetchone()
    await db.close()
    return r["c"]


# ── Compaction CRUD ──

async def add_compaction(session_id: str, summary_text: str, range_start: int,
                         range_end: int, original_tokens: int, compressed_tokens: int) -> dict:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO compactions (session_id, summary_text, message_range_start, message_range_end, original_token_count, compressed_token_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, summary_text, range_start, range_end, original_tokens, compressed_tokens, _now()),
    )
    await db.commit()
    await db.close()
    return {"id": cursor.lastrowid}


async def get_compactions(session_id: str) -> list[dict]:
    db = await get_db()
    rows = await db.execute(
        "SELECT * FROM compactions WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
    )
    results = [dict(r) for r in await rows.fetchall()]
    await db.close()
    return results


async def get_active_token_count(session_id: str) -> int:
    db = await get_db()
    row = await db.execute(
        "SELECT COALESCE(SUM(token_count), 0) as total FROM messages WHERE session_id = ? AND is_compacted = 0",
        (session_id,),
    )
    r = await row.fetchone()
    await db.close()
    return r["total"]


# ── Settings CRUD ──

async def get_setting(key: str, default: str = "") -> str:
    db = await get_db()
    row = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    r = await row.fetchone()
    await db.close()
    return r["value"] if r else default


async def set_setting(key: str, value: str):
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, _now()),
    )
    await db.commit()
    await db.close()


async def get_all_settings() -> dict[str, str]:
    db = await get_db()
    rows = await db.execute("SELECT key, value FROM settings")
    results = {r["key"]: r["value"] for r in await rows.fetchall()}
    await db.close()
    return results
