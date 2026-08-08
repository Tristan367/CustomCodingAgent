# Custom Coding Agent - Plan

## Overview
Build a simple, personal coding agent optimized for my workflow. Python backend + web UI.
OpenCode is open source (TypeScript/Go) and we can reference it.

## Architecture Decisions (CONFIRMED)
| Decision | Choice |
|----------|--------|
| Backend | FastAPI (async, good streaming) |
| Frontend | HTMX + minimal JS (server-rendered) |
| Session Storage | SQLite (single file, queryable) |
| Session UI | Tabs bar with horizontal scroll (portrait monitor) |
| Compaction | Manual for v1, design for auto at ~262K token threshold later |
| Priority v1 Tool | File editing (read + edit + write) |
| First Provider | DeepSeek |

## API Keys I Use
- DeepSeek API key (starting point)
- Anthropic API key
- OpenRouter API key
- Future: Unsloth API key (for local models)

## Pain Points with OpenCode to Fix
1. **Tabs bleed off screen** when >5 sessions — no scroll/overflow handling
   -> Fix: Horizontally scrollable tab bar, or wrap to multiple rows
2. **Changing thinking effort in one session changes another** (shared API key bug)
   -> Fix: Each session stores its OWN settings (model, temp, thinking, system prompt) in DB
3. **Conversation compaction** — I don't like how OpenCode does it
   -> Fix: Manual compaction trigger, but architected for auto at 262K token threshold
4. **Overly complex system prompt** — want minimal and token-efficient
   -> Fix: Start with only essential tools, short prompt, add as needed

## Feature Requirements

### Session Management
- Each session is fully isolated: own messages, own settings, own working dir
- Settings per session: model, API provider, temperature, thinking effort, system prompt override
- Session list sorted by last activity timestamp
- Tab bar with horizontal scroll (L/R arrow buttons, or mousewheel, or wrap)
- Create / delete / rename sessions
- Sessions tied to a project directory

### Tool System (v1 - Minimal Token Set)
1. **read** — read file contents with line range support
2. **edit** — exact string replacement in files (primary editing tool)
3. **write** — create or overwrite files
4. **bash** — execute shell commands (persistent shell sessions)
5. **grep** — regex search across codebase
6. **glob** — file pattern matching
7. **webfetch** — fetch web content
8. **question** — ask user questions during execution

### Tools to Add Later
- **vision** — proxy to local Ollama vision rig for non-vision models
- todowrite, websearch, lsp, skill, apply_patch

### Compaction Strategy
- v1: Manual trigger (button) — summarize conversation up to current point
- v2: Auto-trigger when context reaches ~262K tokens
- Summarize oldest messages into a compressed summary, keep recent messages intact
- The summary is injected as a system message at the top
- User can always view full history in the UI

### Providers
Each provider has its own adapter implementing:
- `chat_completion(messages, tools, stream=True) -> AsyncIterator[chunks]`
- `supports_vision() -> bool`
- `count_tokens(messages) -> int`

v1: DeepSeek only (OpenAI-compatible API at api.deepseek.com)
v2: Anthropic, OpenRouter

### Vision Rig Integration (Future)
- Ollama on local rig
- Expose as a tool: `vision(image_path, prompt) -> description`
- When a non-vision model needs to "see" an image, it calls this tool
- The tool sends the image to the local Ollama vision model, returns text description
- This description gets injected into the conversation

---

## Database Schema (SQLite)

```sql
-- Core session table
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_dir TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'deepseek',
    model TEXT NOT NULL DEFAULT 'deepseek-chat',
    temperature REAL DEFAULT 0.0,
    thinking_effort TEXT,  -- null = default, 'low', 'medium', 'high' (deepseek)
    system_prompt_override TEXT,  -- null = use default
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    is_archived INTEGER DEFAULT 0
);

-- Individual messages in a session
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,  -- 'user', 'assistant', 'system', 'tool'
    content TEXT NOT NULL,
    tool_calls TEXT,     -- JSON array of tool calls (assistant messages)
    tool_call_id TEXT,   -- for tool result messages
    token_count INTEGER,
    created_at TEXT NOT NULL,
    is_compacted INTEGER DEFAULT 0  -- marked if summarized into a compaction
);

-- Compaction summaries (stored separately from regular messages)
CREATE TABLE compactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    summary_text TEXT NOT NULL,
    message_range_start INTEGER,  -- first message ID covered
    message_range_end INTEGER,    -- last message ID covered
    original_token_count INTEGER,
    compressed_token_count INTEGER,
    created_at TEXT NOT NULL
);
```

## Project Structure

```
CustomCodingAgent/
├── PLAN.md                    # This file
├── requirements.txt
├── agent_server/
│   ├── __init__.py
│   ├── main.py                # FastAPI app entry point
│   ├── config.py              # Settings, env vars
│   ├── database.py            # SQLite setup + queries
│   ├── models.py              # Pydantic models
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── sessions.py        # Session CRUD
│   │   ├── chat.py            # Chat endpoint (SSE/WS streaming)
│   │   └── files.py           # File browsing endpoints
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py            # Abstract provider interface
│   │   ├── deepseek.py        # DeepSeek API adapter
│   │   ├── anthropic.py       # Anthropic API adapter (future)
│   │   └── openrouter.py      # OpenRouter adapter (future)
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── registry.py        # Tool registration + execution
│   │   ├── bash.py            # Shell command execution
│   │   ├── file_ops.py        # read, edit, write
│   │   ├── search.py          # grep, glob
│   │   ├── web.py             # webfetch
│   │   ├── question.py        # user question tool
│   │   └── vision.py          # Vision rig proxy (future)
│   ├── compaction.py          # Summary/compaction logic
│   └── system_prompt.py       # Default system prompt builder
├── web_ui/
│   ├── static/
│   │   ├── css/
│   │   │   └── style.css
│   │   └── js/
│   │       ├── htmx.min.js
│   │       └── app.js         # Minimal JS for streaming + tab scroll + file tree
│   └── templates/
│       ├── base.html          # Base layout (tab bar, sidebar skeleton)
│       ├── index.html         # Session list / home
│       ├── session.html       # Active session view
│       ├── chat_messages.html # HTMX partial for message list
│       └── components/
│           ├── tab_bar.html
│           └── file_tree.html
└── data/                      # SQLite DB lives here (gitignored)
    └── agent.db
```

## Implementation Order

### Phase 1: Skeleton (DeepSeek-only, basic chat)
1. FastAPI app scaffold, config, database setup
2. DeepSeek provider adapter (OpenAI-compatible API)
3. Basic chat endpoint with SSE streaming
4. HTMX frontend: session creation, message list, input box
5. Tab bar with horizontal scroll

### Phase 2: Tools (file editing focus)
6. read tool
7. edit tool (exact string replacement)
8. write tool
9. bash tool (persistent shell session)
10. grep + glob tools
11. webfetch tool
12. question tool

### Phase 3: Polish
13. Session settings panel (per-session model/temp/thinking)
14. Manual compaction
15. Session rename/delete/archive
16. Token counting + usage display

### Phase 4: Multi-provider
17. Anthropic adapter
18. OpenRouter adapter
19. Provider switching in session settings

### Phase 5: Advanced
20. Vision tool (Ollama rig proxy)
21. Auto-compaction at token threshold
22. Unsloth provider
23. Subagents (task tool)

## Default System Prompt (Token-Efficient)

```
You are a coding agent that helps with software engineering tasks.
You have tools to read, edit, write files, run shell commands, and search code.
Be concise. Read files before editing them.
Follow existing code conventions in any file you modify.
```

Kept deliberately short — the model's own training handles the rest.
Session-level overrides can extend this per-project.
