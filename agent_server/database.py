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
    compact_threshold INTEGER,
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

CREATE TABLE IF NOT EXISTS session_write_dirs (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, path)
);

CREATE TABLE IF NOT EXISTS prompts (
    kind TEXT NOT NULL DEFAULT 'system',
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, name)
);

CREATE TABLE IF NOT EXISTS usage_monthly (
    month TEXT PRIMARY KEY,
    cost REAL NOT NULL DEFAULT 0,
    input INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    reasoning INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0
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
    ("sessions", "compact_threshold", "INTEGER"),
    ("sessions", "auto_compact", "INTEGER DEFAULT 0"),
    # The rendered system prompt, frozen per session. Kept here rather than
    # rebuilt per request so that editing a shared prompt, restarting the
    # server, or the date rolling over cannot change a live conversation's
    # prefix and re-bill it at the cache-miss rate.
    ("sessions", "system_prompt", "TEXT"),
    # Set when the prompt was edited for this session specifically, so a later
    # "apply to existing" can leave it alone. Comparing text against what the
    # shared prompt renders to cannot tell a customised session from one that
    # simply predates an earlier edit.
    ("sessions", "prompt_custom", "INTEGER DEFAULT 0"),
    # Which summarising prompt this session compacts with. Independent of the
    # system prompt: the instructions for writing a summary have nothing to do
    # with the instructions for doing the work.
    ("sessions", "compact_profile", "TEXT DEFAULT 'default'"),
    # Fingerprint of the last request sent, so the next one can be compared
    # against it and a cache miss predicted before it is paid for.
    ("sessions", "cache_fp", "TEXT"),
    ("sessions", "cache_fp_tokens", "TEXT"),
    ("sessions", "cache_checked_at", "TEXT"),
    ("sessions", "cache_prompt_tokens", "INTEGER"),
    # A new shared prompt waiting to be adopted. Applied at the next compaction
    # rather than immediately: compaction rewrites the conversation anyway, so
    # the prefix is already a miss at that point and the swap is close to free.
    # Applying it mid-conversation would re-bill the entire context.
    ("sessions", "pending_system_prompt", "TEXT"),
    ("messages", "reasoning_content", "TEXT"),
    ("messages", "tool_name", "TEXT"),
    ("messages", "is_error", "INTEGER DEFAULT 0"),
    ("messages", "usage", "TEXT"),
    # Diffs used to be streamed over SSE only, so they vanished on reload.
    ("messages", "diff", "TEXT"),
    ("messages", "tool_title", "TEXT"),
    ("messages", "duration_ms", "INTEGER"),
    ("messages", "file_path", "TEXT"),
    # Whether this row's reasoning is still echoed back to the API. Thinking
    # mode requires it while a tool turn is open; once a later user message
    # closes the turn it is dead weight. The decision is stored rather than
    # derived, because deriving it would silently rewrite the prefix the moment
    # the user typed again and re-bill the whole conversation at the miss rate.
    ("messages", "send_reasoning", "INTEGER DEFAULT 1"),
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
    await _rekey_prompts(db)
    await db.commit()


async def _rekey_prompts(db):
    """Give prompts a (kind, name) key so both kinds can have a 'default'.

    The table shipped keyed on name alone, when system prompts were the only
    kind. Summarising prompts need their own 'default', and SQLite cannot alter
    a primary key, so the table is rebuilt once.
    """
    cur = await db.execute("PRAGMA table_info(prompts)")
    cols = {r[1] for r in await cur.fetchall()}
    if "kind" in cols:
        return
    await db.execute("ALTER TABLE prompts RENAME TO prompts_old")
    await db.executescript(SCHEMA)
    await db.execute(
        "INSERT INTO prompts (kind, name, body, updated_at)"
        " SELECT 'system', name, body, updated_at FROM prompts_old"
    )
    await db.execute("DROP TABLE prompts_old")



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
    "prompt_profile", "compact_profile", "bash_auto_approve", "is_archived", "compact_threshold",
    "cache_fp", "cache_fp_tokens", "cache_checked_at", "cache_prompt_tokens",
    "auto_compact", "system_prompt", "prompt_custom", "pending_system_prompt",
}


async def create_session(
    name: str,
    project_dir: str,
    provider: str = "deepseek",
    model: str = "deepseek-v4-pro",
    prompt_profile: str = "default",
    compact_profile: str = "default",
    thinking_effort: str | None = None,
) -> dict:
    sid = uuid.uuid4().hex[:8]
    now = _now()
    await _execute(
        "INSERT INTO sessions (id, name, project_dir, provider, model, prompt_profile,"
        " compact_profile, thinking_effort, created_at, last_active_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, name, project_dir, provider, model, prompt_profile, compact_profile,
         thinking_effort, now, now),
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
    """Remove a session and everything hanging off it.

    There is no cascade: SQLite only enforces foreign keys when asked, and the
    schema does not declare them, so deleting just the session row left its
    messages behind forever.
    """
    db = await connect()
    async with _write_lock:
        for table in ("messages", "compactions", "session_write_dirs"):
            await db.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
        await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await db.commit()


async def purge_orphans() -> int:
    """Drop rows left behind by earlier deletes that did not cascade."""
    db = await connect()
    total = 0
    async with _write_lock:
        for table in ("messages", "compactions", "session_write_dirs"):
            cur = await db.execute(
                f"DELETE FROM {table} WHERE session_id NOT IN (SELECT id FROM sessions)"
            )
            total += cur.rowcount or 0
        await db.commit()
    return total


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
    diff: str = "",
    tool_title: str = "",
    duration_ms: int = 0,
    file_path: str = "",
) -> dict:
    """Insert a message. `tool_calls` is stored as canonical OpenAI wire JSON."""
    msg_id = await _execute(
        "INSERT INTO messages (session_id, role, content, reasoning_content, tool_calls,"
        " tool_call_id, tool_name, is_error, token_count, usage, diff, tool_title,"
        " duration_ms, file_path, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            diff or None,
            tool_title or None,
            duration_ms or None,
            file_path or None,
            _now(),
        ),
    )
    await touch_session(session_id)
    if usage:
        await _record_month(session_id, usage)
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


async def delete_messages_after(session_id: str, message_id: int) -> int:
    """Drop everything after a message. Used by retry and edit-and-resend."""
    db = await connect()
    async with _write_lock:
        cur = await db.execute(
            "DELETE FROM messages WHERE session_id = ? AND id > ?", (session_id, message_id)
        )
        await db.commit()
        return cur.rowcount or 0


MESSAGE_FIELDS = {"content", "send_reasoning"}


async def update_message(message_id: int, **fields):
    fields = {k: v for k, v in fields.items() if k in MESSAGE_FIELDS}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    await _execute(
        f"UPDATE messages SET {sets} WHERE id = ?", (*fields.values(), message_id)
    )


async def get_turn_changes(session_id: str) -> dict:
    """Aggregate the file changes made since the last user message.

    Used for the summary shown when a turn finishes, so the user can see
    everything that was touched without scrolling back through the transcript.
    """
    rows = await _fetchall(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    since = rows[0]["id"] if rows else 0
    edits = await _fetchall(
        "SELECT file_path, diff, tool_name FROM messages"
        " WHERE session_id = ? AND id > ? AND diff IS NOT NULL AND file_path IS NOT NULL"
        " ORDER BY id ASC",
        (session_id, since),
    )
    by_file: dict[str, dict] = {}
    for row in edits:
        entry = by_file.setdefault(
            row["file_path"], {"path": row["file_path"], "added": 0, "removed": 0, "diffs": []}
        )
        diff = row["diff"] or ""
        entry["added"] += sum(
            1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")
        )
        entry["removed"] += sum(
            1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")
        )
        entry["diffs"].append(diff)
    files = list(by_file.values())
    return {
        "files": files,
        "added": sum(f["added"] for f in files),
        "removed": sum(f["removed"] for f in files),
    }


async def mark_messages_compacted(session_id: str, message_ids: list[int]):
    if not message_ids:
        return
    placeholders = ",".join("?" * len(message_ids))
    await _execute(
        f"UPDATE messages SET is_compacted = 1 WHERE session_id = ? AND id IN ({placeholders})",
        (session_id, *message_ids),
    )


def _price(usage_json: str, pricing: dict) -> tuple[dict, float]:
    """Split one usage record into token counts and its dollar cost."""
    try:
        u = json.loads(usage_json)
    except (json.JSONDecodeError, TypeError):
        return {}, 0.0
    if not pricing:
        return u, 0.0
    cached = u.get("cached_tokens", 0) or 0
    prompt = u.get("prompt_tokens", 0) or 0
    completion = u.get("completion_tokens", 0) or 0
    cost = (
        cached * pricing["price_in_hit"]
        + max(prompt - cached, 0) * pricing["price_in_miss"]
        + completion * pricing["price_out"]
    ) / 1_000_000
    return u, cost


async def get_session_usage(session_id: str) -> dict:
    """Token totals, spend, and live context size for one session."""
    from agent_server.config import COMPACT_THRESHOLD_TOKENS, MODELS_BY_ID

    session = await get_session(session_id)
    pricing = MODELS_BY_ID.get((session or {}).get("model", ""), {})
    # Spend is counted from the last reset, while the context measurement below
    # still reads every row. Deleting the usage records to clear the figure
    # would take the measured context with them and drop the ring back to an
    # estimate, so the reset is a cutoff rather than a delete.
    since = await get_setting("usage_reset_at", "")
    rows = await _fetchall(
        "SELECT usage FROM messages WHERE session_id = ? AND usage IS NOT NULL"
        " AND created_at > ?",
        (session_id, since),
    )

    totals = {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "cost": 0.0, "requests": 0}
    for row in rows:
        u, cost = _price(row["usage"], pricing)
        if not u:
            continue
        totals["requests"] += 1
        totals["input"] += u.get("prompt_tokens", 0) or 0
        totals["cached"] += u.get("cached_tokens", 0) or 0
        totals["output"] += u.get("completion_tokens", 0) or 0
        totals["reasoning"] += u.get("reasoning_tokens", 0) or 0
        totals["cost"] += cost

    # The most recent request's prompt size is the truest measure of live context.
    # Assistant rows only. Subagent usage is recorded on tool rows so that its
    # cost counts, but a subagent's prompt is its own conversation and says
    # nothing about how full this session's context is.
    last = await _fetchone(
        "SELECT usage FROM messages WHERE session_id = ? AND usage IS NOT NULL"
        " AND role = 'assistant' AND is_compacted = 0 ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    # A measured prompt size is only meaningful if nothing has been removed
    # since it was measured. Right after a compaction the newest surviving
    # assistant row still carries the prompt size from before the summary
    # replaced everything, so the ring would keep showing the old figure until
    # the next request happened to correct it.
    stale = False
    if last:
        newest_compaction = await _fetchone(
            "SELECT created_at FROM compactions WHERE session_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        last_row = await _fetchone(
            "SELECT created_at FROM messages WHERE session_id = ? AND usage IS NOT NULL"
            " AND role = 'assistant' AND is_compacted = 0 ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if newest_compaction and last_row:
            stale = newest_compaction["created_at"] > last_row["created_at"]

    context = 0
    if last and not stale:
        u, _ = _price(last["usage"], {})
        context = u.get("prompt_tokens", 0) or 0
    if not context:
        row = await _fetchone(
            "SELECT COALESCE(SUM(token_count), 0) AS total FROM messages"
            " WHERE session_id = ? AND is_compacted = 0",
            (session_id,),
        )
        summaries = await _fetchone(
            "SELECT COALESCE(SUM(compressed_token_count), 0) AS total FROM compactions"
            " WHERE session_id = ?",
            (session_id,),
        )
        context = (row or {}).get("total", 0) + (summaries or {}).get("total", 0)

    totals["context"] = context
    totals["threshold"] = (session or {}).get("compact_threshold") or COMPACT_THRESHOLD_TOKENS
    totals["max_context"] = pricing.get("context", 1_000_000)
    totals["percent"] = round(100 * context / totals["threshold"], 1) if totals["threshold"] else 0
    totals["cache_hit_rate"] = (
        round(100 * totals["cached"] / totals["input"], 1) if totals["input"] else 0
    )
    return totals


def current_month() -> str:
    return datetime.now().strftime("%Y-%m")


async def _record_month(session_id: str, usage: dict):
    """Add one request to this month's running total.

    Spend used to be recomputed by scanning every usage record ever written,
    which meant it could not be reset without deleting the per-message figures
    the context ring is measured from, and it only ever grew. A rollup is a
    handful of numbers, so the history it replaces does not need keeping.
    """
    from agent_server.config import MODELS_BY_ID

    session = await get_session(session_id)
    _, cost = _price(json.dumps(usage), MODELS_BY_ID.get((session or {}).get("model", ""), {}))
    month = current_month()
    async with _write_lock:
        db = await connect()
        # Only the month in progress is kept; last month is not something we
        # can act on, and it would accumulate forever.
        await db.execute("DELETE FROM usage_monthly WHERE month != ?", (month,))
        await db.execute(
            "INSERT INTO usage_monthly (month, cost, input, cached, output, reasoning, requests)"
            " VALUES (?,?,?,?,?,?,1)"
            " ON CONFLICT(month) DO UPDATE SET"
            "  cost = cost + excluded.cost,"
            "  input = input + excluded.input,"
            "  cached = cached + excluded.cached,"
            "  output = output + excluded.output,"
            "  reasoning = reasoning + excluded.reasoning,"
            "  requests = requests + 1",
            (
                month,
                cost,
                usage.get("prompt_tokens", 0) or 0,
                usage.get("cached_tokens", 0) or 0,
                usage.get("completion_tokens", 0) or 0,
                usage.get("reasoning_tokens", 0) or 0,
            ),
        )
        await db.commit()


async def get_month_usage() -> dict:
    """Spend for the month in progress."""
    month = current_month()
    row = await _fetchone("SELECT * FROM usage_monthly WHERE month = ?", (month,))
    total = dict(row) if row else {
        "month": month, "cost": 0.0, "input": 0, "cached": 0,
        "output": 0, "reasoning": 0, "requests": 0,
    }
    total["cache_hit_rate"] = (
        round(100 * total["cached"] / total["input"], 1) if total["input"] else 0
    )
    total["label"] = datetime.strptime(total["month"], "%Y-%m").strftime("%B %Y")
    return total


async def reset_usage():
    """Zero the counters without touching what they were counted from."""
    await _execute("DELETE FROM usage_monthly")
    await set_setting("usage_reset_at", _now())


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


# ── Per-session write grants ────────────────────────────────────────────────

async def list_write_dirs(session_id: str) -> list[str]:
    rows = await _fetchall(
        "SELECT path FROM session_write_dirs WHERE session_id = ? ORDER BY path", (session_id,)
    )
    return [r["path"] for r in rows]


async def add_write_dir(session_id: str, path: str):
    await _execute(
        "INSERT OR IGNORE INTO session_write_dirs (session_id, path, created_at) VALUES (?,?,?)",
        (session_id, path, _now()),
    )


async def remove_write_dir(session_id: str, path: str):
    await _execute(
        "DELETE FROM session_write_dirs WHERE session_id = ? AND path = ?", (session_id, path)
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


async def delete_setting(key: str):
    await _execute("DELETE FROM settings WHERE key = ?", (key,))


# ── Prompts ─────────────────────────────────────────────────────────────────


async def list_prompts(kind: str = "") -> list[dict]:
    sql = "SELECT * FROM prompts"
    params: tuple = ()
    if kind:
        sql += " WHERE kind = ?"
        params = (kind,)
    return await _fetchall(sql + " ORDER BY kind, name = 'default' DESC, name", params)


async def get_prompt(name: str, kind: str = "system") -> dict | None:
    return await _fetchone(
        "SELECT * FROM prompts WHERE kind = ? AND name = ?", (kind, name)
    )


async def save_prompt(name: str, body: str, kind: str = "system"):
    # Textareas submit CRLF per the HTML spec; storing that would make an
    # untouched round-trip through the editor look like an edit.
    body = body.replace("\r\n", "\n").strip()
    await _execute(
        "INSERT INTO prompts (kind, name, body, updated_at) VALUES (?,?,?,?)"
        " ON CONFLICT(kind, name) DO UPDATE SET body = excluded.body,"
        " updated_at = excluded.updated_at",
        (kind, name, body, _now()),
    )


async def delete_prompt(name: str, kind: str = "system"):
    await _execute("DELETE FROM prompts WHERE kind = ? AND name = ?", (kind, name))

