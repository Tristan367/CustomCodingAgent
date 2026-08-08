"""SQLite persistence layer.

Uses one long-lived aiosqlite connection guarded by a write lock. Rows are
ordered by the autoincrement `id`, never by `created_at` -- two messages written
in the same microsecond must not be allowed to swap places, because the
OpenAI/DeepSeek wire format requires tool results to directly follow the
assistant message that requested them.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import aiosqlite

from agent_server.config import DB_PATH

_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_dir TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'deepseek',
    model TEXT NOT NULL DEFAULT 'deepseek-v4-pro',
    thinking_effort TEXT,
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
    content TEXT NOT NULL DEFAULT '',
    reasoning_content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    tool_name TEXT,
    is_error INTEGER DEFAULT 0,
    token_count INTEGER,
    usage TEXT,
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

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_compactions_session ON compactions(session_id, id);
"""

# Columns added after the original schema shipped. Applied idempotently.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("sessions", "prompt_profile", "TEXT DEFAULT 'default'"),
    ("sessions", "bash_auto_approve", "INTEGER DEFAULT 0"),
    ("messages", "reasoning_content", "TEXT"),
    ("messages", "tool_name", "TEXT"),
    ("messages", "is_error", "INTEGER DEFAULT 0"),
    ("messages", "usage", "TEXT"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def connect() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        _conn = await aiosqlite.connect(str(DB_PATH))
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA foreign_keys=ON")
        await _conn.execute("PRAGMA busy_timeout=5000")
        await _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


async def close():
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def init_db():
    db = await connect()
    await db.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        cur = await db.execute(f"PRAGMA table_info({table})")
        existing = {r[1] for r in await cur.fetchall()}
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    await db.commit()


async def _fetchone(sql: str, params: tuple = ()) -> dict | None:
    db = await connect()
    cur = await db.execute(sql, params)
    row = await cur.fetchone()
    return dict(row) if row else None


async def _fetchall(sql: str, params: tuple = ()) -> list[dict]:
    db = await connect()
    cur = await db.execute(sql, params)
    return [dict(r) for r in await cur.fetchall()]


async def _execute(sql: str, params: tuple = ()) -> int:
    db = await connect()
    async with _write_lock:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid or 0


# ── Sessions ────────────────────────────────────────────────────────────────

SESSION_FIELDS = {
    "name", "project_dir", "provider", "model", "thinking_effort",
    "prompt_profile", "bash_auto_approve", "is_archived",
}


async def create_session(
    name: str,
    project_dir: str,
    provider: str = "deepseek",
    model: str = "deepseek-v4-pro",
    prompt_profile: str = "default",
    thinking_effort: str | None = None,
) -> dict:
    sid = uuid.uuid4().hex[:8]
    now = _now()
    await _execute(
        "INSERT INTO sessions (id, name, project_dir, provider, model, prompt_profile,"
        " thinking_effort, created_at, last_active_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, name, project_dir, provider, model, prompt_profile, thinking_effort, now, now),
    )
    session = await get_session(sid)
    assert session is not None
    return session


async def get_session(session_id: str) -> dict | None:
    return await _fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))


async def list_sessions(archived: bool = False) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM sessions WHERE is_archived = ? ORDER BY last_active_at DESC",
        (1 if archived else 0,),
    )


async def update_session(session_id: str, **kwargs) -> dict | None:
    updates = {k: v for k, v in kwargs.items() if k in SESSION_FIELDS}
    if not updates:
        return await get_session(session_id)
    clause = ", ".join(f"{k} = ?" for k in updates)
    await _execute(
        f"UPDATE sessions SET {clause} WHERE id = ?",
        (*updates.values(), session_id),
    )
    return await get_session(session_id)


async def touch_session(session_id: str):
    await _execute("UPDATE sessions SET last_active_at = ? WHERE id = ?", (_now(), session_id))


async def delete_session(session_id: str):
    await _execute("DELETE FROM sessions WHERE id = ?", (session_id,))


# ── Messages ────────────────────────────────────────────────────────────────

async def add_message(
    session_id: str,
    role: str,
    content: str = "",
    *,
    reasoning_content: str | None = None,
    tool_calls: list[dict] | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    is_error: bool = False,
    token_count: int | None = None,
    usage: dict | None = None,
) -> dict:
    """Insert a message. `tool_calls` is stored as canonical OpenAI wire JSON."""
    msg_id = await _execute(
        "INSERT INTO messages (session_id, role, content, reasoning_content, tool_calls,"
        " tool_call_id, tool_name, is_error, token_count, usage, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            role,
            content or "",
            reasoning_content,
            json.dumps(tool_calls) if tool_calls else None,
            tool_call_id,
            tool_name,
            1 if is_error else 0,
            token_count,
            json.dumps(usage) if usage else None,
            _now(),
        ),
    )
    await touch_session(session_id)
    row = await _fetchone("SELECT * FROM messages WHERE id = ?", (msg_id,))
    assert row is not None
    return row


async def get_messages(session_id: str, include_compacted: bool = False) -> list[dict]:
    if include_compacted:
        return await _fetchall(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,)
        )
    return await _fetchall(
        "SELECT * FROM messages WHERE session_id = ? AND is_compacted = 0 ORDER BY id ASC",
        (session_id,),
    )


async def delete_message(message_id: int):
    await _execute("DELETE FROM messages WHERE id = ?", (message_id,))


async def mark_messages_compacted(session_id: str, message_ids: list[int]):
    if not message_ids:
        return
    placeholders = ",".join("?" * len(message_ids))
    await _execute(
        f"UPDATE messages SET is_compacted = 1 WHERE session_id = ? AND id IN ({placeholders})",
        (session_id, *message_ids),
    )


async def get_session_usage(session_id: str) -> dict:
    """Aggregate token usage and estimated cost for a session."""
    from agent_server.config import MODELS_BY_ID

    session = await get_session(session_id)
    rows = await _fetchall(
        "SELECT usage FROM messages WHERE session_id = ? AND usage IS NOT NULL", (session_id,)
    )
    totals = {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "cost": 0.0}
    pricing = MODELS_BY_ID.get((session or {}).get("model", ""), {})
    for row in rows:
        try:
            u = json.loads(row["usage"])
        except (json.JSONDecodeError, TypeError):
            continue
        cached = u.get("cached_tokens", 0) or 0
        prompt = u.get("prompt_tokens", 0) or 0
        completion = u.get("completion_tokens", 0) or 0
        miss = max(prompt - cached, 0)
        totals["input"] += prompt
        totals["cached"] += cached
        totals["output"] += completion
        totals["reasoning"] += u.get("reasoning_tokens", 0) or 0
        if pricing:
            totals["cost"] += (
                cached * pricing["price_in_hit"]
                + miss * pricing["price_in_miss"]
                + completion * pricing["price_out"]
            ) / 1_000_000
    # Live context size = tokens the next request will actually send.
    row = await _fetchone(
        "SELECT COALESCE(SUM(token_count), 0) AS total FROM messages"
        " WHERE session_id = ? AND is_compacted = 0",
        (session_id,),
    )
    totals["context"] = (row or {}).get("total", 0)
    return totals


# ── Compactions ─────────────────────────────────────────────────────────────

async def add_compaction(
    session_id: str,
    summary_text: str,
    range_start: int,
    range_end: int,
    original_tokens: int,
    compressed_tokens: int,
) -> int:
    return await _execute(
        "INSERT INTO compactions (session_id, summary_text, message_range_start,"
        " message_range_end, original_token_count, compressed_token_count, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (session_id, summary_text, range_start, range_end, original_tokens,
         compressed_tokens, _now()),
    )


async def get_compactions(session_id: str) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM compactions WHERE session_id = ? ORDER BY id ASC", (session_id,)
    )


# ── Settings ────────────────────────────────────────────────────────────────

async def get_setting(key: str, default: str = "") -> str:
    row = await _fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


async def set_setting(key: str, value: str):
    await _execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, _now()),
    )


async def get_all_settings() -> dict[str, str]:
    rows = await _fetchall("SELECT key, value FROM settings")
    return {r["key"]: r["value"] for r in rows}
