# MyriadCode

A personal coding agent you run on your own machine. FastAPI backend, HTMX +
vanilla-JS frontend (no framework), SQLite storage. It reads and edits your
filesystem, runs shell commands only with your permission, drives a real browser
to check its own work, and talks to DeepSeek, Anthropic, OpenRouter, or any
OpenAI-compatible endpoint.

---

## Features

### Sessions & runs

- **Sessions** are named conversations pinned to a project directory. Each has
  its own model, prompt profile, permissions, browser context, and settings —
  nothing leaks between them.
- **Runs outlive the browser tab.** A turn is a server-owned task; the UI
  subscribes over SSE. Closing the tab only unsubscribes — the turn keeps going
  and is recorded. Reopening a running session reattaches and replays the tool
  calls still outstanding.
- **Message mid-run.** The composer stays live; messages sent while the agent
  works are held and injected at the next turn boundary (never between a tool
  call and its results). Queued messages can be undone before delivery.
- **Broadcast** one message to many sessions at once.
- **Doom-loop abort** — a turn stuck repeating the same tool call in a tight loop
  aborts rather than billing forever.
- **Tabs** — the top bar is real browser-tab behaviour: open, close, reorder,
  and per-tab status dots, persisted between restarts.

### Tools (13 built-in)

| Tool | What it does |
|---|---|
| `read` | read a file (with a `Did you mean` hint on miss) |
| `write` | create/overwrite a file, returns a `diff` |
| `edit` | exact-match string replacement (errors on multiple matches) |
| `bash` | run a shell command (timeout, own process group, permission gate) |
| `grep` | search file contents (ripgrep) |
| `glob` | find files by pattern |
| `webfetch` | fetch a URL and convert it to markdown |
| `websearch` | search the web |
| `task` | spawn a subagent (which can spawn its own) |
| `explore` | spawn a read-only research subagent |
| `send_message` | message another session by name |
| `capture` | screenshot the desktop |
| `browser` | drive a real Chromium from a list of steps |

Independent read-only calls (`read`, `grep`, `glob`, `webfetch`, `websearch`,
`task`, `explore`, `capture`) run concurrently, so three subagents cost the
slowest one rather than the sum. Anything that mutates runs sequentially.
Tool results can carry a `diff`, which the UI renders inline.

### Subagents & inter-agent messaging

- `task` spawns a subagent that can itself spawn more, up to a user-configured
  tier hierarchy (`sa_tool`, `sa_tool_2`, …). Each tier has its own system
  prompt, model, disabled-tool set, and parallel cap.
- `explore` is the read-only, research-only subagent.
- `send_message` sends to another session **by name**. A self-target is rejected;
  an idle target is woken immediately, a busy one queues the message in a mailbox
  and receives it at its next turn boundary.

### Safety & permissions

Two independent gates (`agent_server/permissions.py`):

- **Shell** — read-only commands (`ls`, `cat`, `git status`, …) run silently.
  Anything that can redirect, chain, or mutate asks first. Approve once,
  approve-all-for-the-process, or reject (fed back as a tool result so the model
  can adapt).
- **Filesystem writes outside the project directory** — always ask, even with
  shell auto-approval on. Allow once, or allow a directory for the session.
  `/proc`, `/sys`, `/dev`, `/boot`, and the sudoers files can never be granted.

Every grant is scoped to one session and dropped when the session is deleted.
`sudo` always prompts for a password (never saved), injected once into a call
rewritten to `sudo -S`. A `danger_reason()` guard blocks `rm -rf /` (and
protected paths), fork bombs, and raw block-device writes.

### Editor, file manager, formatters

- **Editor** (side panel) — per-session memory (open file, unsaved buffer, scroll,
  caret, split state) so it survives tab switches; back/forward history; reopen
  via the session-bar button or **Ctrl+E**; half/full-height split.
- **File manager** — browse, mkdir, rename, move, delete, duplicate, wired so
  renames and moves keep the open editor in sync.
- **Formatters** — dispatch to clang-format (C/C++/C#/Java/ObjC/proto), black
  (Python), prettier, rustfmt, gofmt, shfmt, and JSON. A missing binary shows
  "install X" instead of failing.

### Attachments

Attach files or directories with the paperclip; the agent is sent the absolute
path and decides what to do with it (read it, glob it, call `vision` on it, …).
Images preview inline; other files and folders render as chips with their size.
Drag-and-drop works anywhere in the window — on Linux the real path is handed
over directly, and elsewhere the file is copied into the app's upload dir.
Attachments can be reordered by dragging and cleared all at once.

### Speech

- **Dictation** — click the mic button or press **Ctrl+M** to toggle recording.
  A level meter sits on the composer while recording. Streaming transcription
  via `whisper-server` commits audio older than a fixed delay using the model's
  own segment timestamps, so latency stays flat. Everything is local; no audio
  leaves the machine.
- **TTS** — Kokoro (via `onnxruntime`, deliberately on CPU) reads replies aloud,
  with tone settings and a per-session voice.

### Vision, browser, capture

- **`browser`** drives a real Chromium from a list of `steps` in one call
  (click/fill/hover/press/shoot/record/compare) with accessibility-tree snapshots
  and `expect` assertions, so a UI change can be proven rather than asserted in
  prose. One context per session, reaped when idle.
- **`capture`** screenshots the desktop for anything that isn't a web page.
- **No built-in `vision` tool** — looking at an image needs a GPU or a paid
  account, so `browser` and `capture` dispatch to whatever custom tool is named
  `vision`. `examples/vision-tool.sh` is a working one for Ollama.

### Prompts, custom tools, scripts, secrets

- **Profiles** (`/prompts`) — three built-in prompt profiles, a shared
  preferences block, and compaction instructions. Every prompt ends with an
  auto-generated environment snapshot (cwd, platform, date, git status,
  top-level contents), so the model doesn't invent absolute paths.
- **Custom tools** — shell scripts with a JSON Schema, called by the model;
  arguments arrive as `$TOOL_ARG_NAME`. Which sessions may call one is chosen per
  prompt profile.
- **Scripts** — shell the *user* runs from the home page (never shown to the
  model, no schema). Starting/stopping daemons is the motivating example.
- **Secrets** — saved per tool/script and exposed to their environment only.

### UI polish

- **Themes** — green (default), red, blue, gray, plus a custom colour picker.
- **Notifications** — per-tab status dot (blue pulsing = working, amber = needs
  you, green = done, red = error) and a short synthesised tone on transitions.
- **Collapsible tool/reasoning blocks** — `<details>` with a disclosure arrow,
  collapsed by default except auto-expand tools (`write`, `edit`), with a
  per-tool "expand by default" panel.

### Cost & prompt caching

DeepSeek bills a cached prompt prefix at roughly **1/120th** of an uncached one,
so essentially all cost control is keeping the prefix byte-stable. The system
prompt is snapshotted per session, shared-prompt edits are adopted only at a
session's next compaction, and `reasoning_content` is echoed back only where the
API requires it. A healthy session sits at 90%+ cached; a **usage ring** in the
session bar reports live tokens, cache hit rate, and spend. A **cache guard**
predicts prefix misses before they are paid for.

When a session crosses its compaction threshold the run pauses and asks — with a
box for one-off instructions and a slider to raise the threshold instead. The
split always lands on a turn boundary, never between a tool call and its results.

---

## Quick start

```bash
uv venv && uv pip install -r requirements.txt
cp .env.example .env        # optional; the key can also be saved in the UI
ln -s "$PWD/bin/myriadcode" ~/.local/bin/myriadcode
myriadcode                  # starts the server and opens the browser
```

| | |
|---|---|
| `myriadcode` | start, and open a browser at it |
| `myriadcode stop` | stop it cleanly |
| `myriadcode restart` | stop it, then start in this terminal |
| `myriadcode status` | running or not, and on what pid |
| `myriadcode open` | open a browser at one already running |

Ctrl-C stops a server started in this terminal; `myriadcode stop` stops one
started elsewhere.

Add your DeepSeek API key on the home page (or set `DEEPSEEK_API_KEY`, which
wins), pick a project directory, and create a session.

> **This is a single-user tool with no authentication.** It reads and writes
> anywhere your user account can and runs arbitrary shell commands. Bind it to
> `127.0.0.1` (the default) and do not expose it to a network.

## Configuration

Environment (see `.env.example`): provider keys, `HOST`/`PORT`, `WHISPER_BIN` /
`WHISPER_MODEL` / `FFMPEG_BIN`, the `VISION_*` block, `MAX_TOOL_ROUNDS`,
`MAX_TOOL_RESULT_CHARS`, `COMPACT_THRESHOLD_TOKENS`, and `CODEAGENT_DATA_DIR`.

Runtime settings (theme, custom colour, expand tools, whisper model, sound, TTS
tone, auto-approve, thresholds, …) live in the `settings` DB table and are edited
from the UI.

## Where your data lives

`~/.local/share/codeagent/agent.db` (`%APPDATA%` on Windows) — API keys and every
transcript. Outside the checkout on purpose, so `git clean -xdf` cannot take it.
Override with `CODEAGENT_DATA_DIR`, which is also how you point a test run at a
scratch database.

## How it works

```
agent_server/
  agent.py          the conversation loop: stream, call tools, pause, resume
  conversation.py   DB rows <-> provider wire format
  compaction.py     summarising long conversations
  database.py       SQLite (one connection, WAL)
  system_prompt.py  prompt profiles + environment grounding
  stt.py            whisper.cpp transcription
  tts.py            Kokoro speech synthesis
  providers/        one adapter per model vendor
  permissions.py    what the agent may do without asking
  browser.py        Playwright engine: one context per session
  capture.py        desktop screenshots, probed per platform
  templating.py     the Jinja environment and its filters
  tools/            bash, browser, capture, edit, explore, glob, grep,
                    read, send_message, task, webfetch, websearch, write
  routes/           HTTP surface, one module per page
web_ui/             Jinja templates, CSS, and ~4 files of vanilla JS
```

A turn runs like this: your message is persisted, the full transcript is
serialised to the wire format, the provider streams back reasoning/content/tool
calls, tools execute, and the loop repeats until the model answers. Every step
is written to SQLite as it happens, so the stored transcript always matches what
was actually sent to the API.

The provider contract is subtle (DeepSeek's thinking mode returns hard 400s for
a wrong `tool_calls` shape, a `tool_call_id` without a matching `tool` message,
or a missing `reasoning_content` echo). `conversation.py` exists to enforce it.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q                    # unit tests, no network
.venv/bin/python -m pytest tests/test_live_agent.py -s  # hits the real API
```

`tests/route_inventory.json` pins the HTTP surface and is regenerated/checked by
`test_route_inventory.py` — update it whenever a route changes.

## Adding things

**A tool** — write an `async def handler(ctx, *, ...) -> ToolResult` in
`agent_server/tools/`, then `register(Tool(...))` in `registry.py`. If it needs
to ask before running, add the rule to `permissions.check` rather than to the
tool, so the agent loop and the page-reload restore path cannot disagree.
Return a `diff` on the `ToolResult` and the UI renders it inline.

**A provider** — subclass `Provider` in `agent_server/providers/`, yield
`StreamEvent` dicts, and add it to the registry in `providers/__init__.py`.
Never raise out of `chat_completion`; yield an `error` event, because an exception
thrown after SSE headers are sent reaches the browser as an opaque stream error.
