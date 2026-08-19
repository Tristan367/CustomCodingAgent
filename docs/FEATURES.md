# MyriadCode — feature index

A single-user, self-hosted coding agent. FastAPI backend, HTMX + vanilla JS
frontend (no framework), SQLite storage, one Playwright-driven Chromium. This
document is a complete index of what the app does and, for each thing, a
one-sentence note on how. The essay-length explanation lives in `README.md`.

## Running and lifecycle

- **Launcher** — `bin/myriadcode` (symlinked on PATH) resolves the repo, frees the
  port if needed, waits for the server, opens a browser, and `exec`s uvicorn so
  Ctrl-C runs the shutdown hook. `run.sh` is the no-browser equivalent.
- **Server** — `uvicorn agent_server.main:app` on `127.0.0.1:8219` by default.
- **Startup** — `main.py` lifespan: configure logging, migrate the DB, prime the
  credentials cache, load custom tools/endpoints, discover DeepSeek models, seed
  theme + whisper model, then start two background tasks (browser reaper, whisper
  warm-up).
- **Shutdown** — stop in-flight turns first, then the whisper-server subprocess,
  then the browser, then the DB. `POST /_shutdown` signals SIGTERM (so the hook
  runs); `myriadcode stop` sends SIGTERM, escalating to SIGKILL.
- **Data** — `~/.local/share/codeagent/` (`CODEAGENT_DATA_DIR`), outside the
  checkout so `git clean` can't destroy it. `agent.db` (SQLite/WAL), `codeagent.log`
  (rotating), `browser_state/`, `tool-output/`.
- **Logging** — root logger writes to stderr and `codeagent.log`; level via
  `CODEAGENT_LOG_LEVEL`.

## Sessions and runs

- **Sessions** are named conversations pinned to a project directory. Each has its
  own settings, permissions grants, browser context, and model.
- **A run** is a server-owned task. Clients subscribe over SSE; closing the tab
  only unsubscribes — the turn keeps going and is recorded. Reopening attaches and
  replays outstanding calls.
- **Queued messages** — composer stays live mid-run; messages sent then are held
  and injected at the next turn boundary (never between a `tool_calls` message and
  its results). Queued messages can be undone before delivery.
- **Stop** cancels immediately; every tool call in a cancelled batch still gets a
  result; an unanswered call is treated as pending and re-run next message.
- **Doom-loop abort** — if a turn repeats the same tool call in a tight loop, the
  run aborts rather than bill forever.
- **Broadcast** (`/api/broadcast`) — a human sends one message to many sessions at
  once.
- **Tabs** — the top bar is real browser-tab behaviour: open/close/reorder/status
  dots, persisted per browser.

## Tools (12 built-in)

Registered in `agent_server/tools/registry.py`. Independent read-only tools run
concurrently; anything that mutates runs sequentially.

| Tool | What it does | How |
|---|---|---|
| `read` | read a file | bytes → decoded text, with a `Did you mean` hint on miss |
| `write` | create/overwrite a file | streams args (see progress below), returns a `diff` |
| `edit` | replace exact text | exact-match replace, errors on 0 or >1 matches |
| `bash` | run a shell command | subprocess with timeout, own process group, permission gate |
| `grep` | search file contents | ripgrep |
| `glob` | find files by pattern | glob |
| `webfetch` | fetch a URL to markdown | httpx |
| `websearch` | web search | provider-backed |
| `task` | spawn a subagent | recursive agent loop (see hierarchy) |
| `send_message` | message another session | mailbox table (see below) |
| `capture` | screenshot the desktop | per-platform screen capture, returns paths |
| `browser` | drive Chromium | Playwright steps + accessibility snapshots |

Tool results can carry a `diff`, which the UI renders inline with +/- highlighting.
`read`/`edit`/`write` operate inside the session's project directory; writes
outside it are gated by permissions.

### Anchored edits

`edit` matches on exact text. That is a choice about which way it fails: a
string that does not match produces a loud error and writes nothing, where a
wrong line number writes to the wrong place and reports success. The first costs
a retry; the second costs a corrupted file.

Three refusals, each with its own message because each has a different fix:

- **Not found** — nearly always whitespace. Nothing was written; look at the
  text again rather than guessing a variation.
- **Not found, and the file changed on disk** — someone else edited it. Re-read.
- **Lines that were never displayed** — matching text proves *where* an edit
  lands, not that anyone looked at it, so an edit whose match falls outside what
  `read` actually showed is refused. `write` is refused likewise when it would
  discard the unread tail of a partially-read file, which the drift check cannot
  catch because nothing else touched the file.

Every successful edit answers with the edited region of the file, numbered as it
now stands. The `diff` a tool returns is display-only and never reaches the
model, so without that echo an edit that landed in the wrong place stayed
invisible until the next read. The post-edit numbers also mean the model never
has to work out how far the lines below its own edit have shifted.

A call in the old shape -- `tag` plus a line range -- is answered by name
rather than with "oldString is required", which is true but reads as a malformed
call and invites the same one again with a guess bolted on. A conversation
started before the change is full of those calls, and a transcript is the
strongest few-shot prompt there is: a model reading its own history will keep
making them however clear the schema is. The message says what changed, what to
send instead, and that the calls above it were correct when they were made.

This replaced a scheme where `read` printed a `[path#tag]` fingerprint and
`edit` took that tag with a line range. The tag genuinely proved the file had
not moved, but it never proved the model was *aiming* at the right lines — and
it charged a running tax to use: carry the current tag, respect a window, do the
shift arithmetic, all while writing code. The seen-lines guarantee was the one
thing worth keeping, and it was kept.

## Subagents and hierarchy

- `task` spawns a subagent that can itself spawn more, down as many tiers as are
  configured (`sa_tool`, `sa_tool_2`, … on the Prompts page).
- Each tier has its own system prompt, model, thinking effort, disabled-tool set
  and concurrency cap (`agent_server/system_prompt.py` reads the
  `subagent_tiers` JSON column).
- **No timeouts and no round cap.** A subagent runs until it answers or the user
  stops it. Killing one at ten minutes throws away ten minutes of paid work and
  returns nothing.
- **Two gates, in `agent_server/tools/task.py`.** The per-tier cap is a ceiling
  across the whole session, not per call — `task` is parallel-safe, so the model
  can issue four calls in one round and they run concurrently; capping each call
  separately let a limit of 5 put twenty subagents in flight. The session-wide
  cap is the total across every tier, and only the shallowest spawns ever *wait*
  on it: a deeper one that waited would be queued behind the very agent holding
  the permit it needs, which is a deadlock.
- Both gates are `_Limiter`, not `asyncio.Semaphore`, because the limit can
  change while work is in flight. Changing a semaphore's limit means building a
  new one, and the agents already running hold permits on the old object — so
  the replacement starts empty and briefly allows `capacity + in-flight` to run.
- Fanned-out subagents sharing a prompt share one `(tool, arguments)` result
  cache, so five of them asking the same question cost one call. Any tool that
  mutates drops the cache, so a shared `read` cannot outlive a sibling's write.
- `send_message` is top-level only — subagents can't message other sessions.

## Inter-AI messaging

- `send_message` sends to another session **by name** (cross-session). Self-target
  is rejected. If the target is idle it's delivered and woken immediately; if
  busy, it lands in the `mailbox` table and is injected at the next turn boundary.
- The recipient sees a blue "mail" bubble (`mail_from`), and the prompt tells it to
  reply with `send_message`. Tests in `tests/test_send_message.py`.
- Because both sides can start a conversation, two sessions can go back and forth
  indefinitely. The stop button is the brake: `stop_all` aborts every run,
  cancels every subagent, and empties the mailbox and the queues, and a send that
  was already executing checks the abort flag before delivering — otherwise it
  would land after the clear-out and wake the target straight back up.

## Permissions and safety

Two independent gates (`agent_server/permissions.py`):

- **Shell** — read-only commands run silently; anything mutating/redirecting/chaining
  asks. Approve once, approve-all-for-the-process, or reject (fed back as a tool
  result).
- **Filesystem writes outside the project** — always ask, even with shell
  auto-approve on. Allow once, or allow a directory for the session. `/proc`, `/sys`,
  `/dev`, `/boot`, sudoers can never be granted.
- **Grants are per-session** and dropped when the session is deleted.
- **Sudo** — a `sudo` command always prompts for a password (never saved). The
  password is injected once into that call, which is rewritten to `sudo -S` + stdin.
- **`rm -rf` guard** — `danger_reason()` blocks `rm -rf /` (and protected paths,
  including the `/bin/rm` form), fork bombs, and raw block-device writes. Deliberately
  conservative: only the obvious catastrophes.

## Dictation (STT) — streaming

- Toggle via the mic button or **Ctrl+M**; talk, and pauses become sentences.
- Browser captures 16 kHz mono float32 via an AudioWorklet (`stt-worklet.js`) and
  streams it over a WebSocket (`/api/stt/stream`).
- **The engine** (`whisper_engine.py`) is faster-whisper, in-process. It replaced
  a `whisper-server` subprocess from whisper.cpp spoken to over HTTP on a local
  port, which meant nothing could be transcribed until you had installed
  whisper.cpp system-wide and downloaded a GGML file by hand. `pip install` and a
  model that fetches itself is a better deal for anyone not already set up.
- **The device is found, not configured.** CPU int8 out of the box; the CUDA
  libraries are a separate 2.3 GB opt-in (`requirements-gpu.txt`) that nothing
  requires. ctranslate2 links against CUDA 12, which is usually not what a
  current distribution ships, so they come from the `nvidia-*-cu12` wheels and
  are `dlopen`'d with `RTLD_GLOBAL` at startup. The documented alternative is exporting `LD_LIBRARY_PATH` before
  starting Python — which a program cannot do for itself, and is the most common
  reason a GPU install silently runs on the CPU. Without a usable GPU it falls
  back to CPU int8, which is fine for a small model.
- **Streaming is still ours** (`whisper_streaming.py`). Whisper has no streaming
  mode in any implementation, so live dictation is a sliding re-transcription:
  audio older than `COMMIT_DELAY_SEC` is committed using the segment timestamps
  and trimmed out of the buffer, so latency stays flat however long you talk.
  Swapping the engine changed none of that — only what runs the inference.
- Model is runtime-selectable from the home page and is a *name* (`base.en`,
  `small.en`, `large-v3-turbo`) or a local CTranslate2 directory, not a file you
  had to fetch. Downloaded once on first use.
- A manual edit of the composer while recording tears the session down cleanly.
- Noise cleanup: whisper's `[BLANK_AUDIO]`-style event tokens, lowercase sound
  descriptions, `--` em-dashes, space-before-punctuation, missing space after
  `.?!`. Whisper's own VAD filter is on, without which a pause reliably produces
  "Thank you." or a subtitle credit.
- One-shot transcription (`stt.py`) uses the same engine, and lets it decode the
  browser's WebM/Opus directly — the ffmpeg transcode and the second subprocess
  are gone.

## Attachments, capture, browser

- **Attachments** — attach files or directories with the paperclip; the agent is
  sent the absolute path and decides what to do with it. Images preview inline,
  other files and folders render as chips. Drag-drop works the same way.
- **`browser`** — drives a real Chromium from a list of steps (click/fill/hover/
  press/shoot/record) with accessibility-tree snapshots and `expect` assertions.
  One context per session, reaped when idle. It drives and asserts; `shoot` saves
  a frame and returns its path, and what anything makes of that image afterwards
  is not the tool's business.
- **`capture`** — desktop screenshots for anything that isn't a web page: a
  native app, a game, an emulator. Returns the paths of the frames it saved.
- Neither describes an image. Both used to carry parameters that dispatched to a
  custom tool named exactly `vision` — schema sent on every request for a call
  that, on any install without one, could only come back "not installed", and
  useless anyway to anyone who had named theirs something else. Saving a frame
  and looking at a frame are separate steps, and looking is something you bring.

## Editor and file manager

- **Editor** — sits between the session bar and the chat, full height or a
  half-height split with the transcript still visible above it. Per-session memory
  (open file, unsaved buffer, scroll, caret, split state) so it survives tab
  switches; back/forward history; reopen via button or Ctrl+E. Format via external
  formatters.
- **Click a path to open it** — in prose, and on a `read`/`edit`/`write` block in
  the transcript. A directory opens the file manager, an image opens a preview
  overlay (Esc or a click outside closes it), anything else opens the editor. The
  path comes from the tool itself rather than from the block's title, which is
  left-truncated for display and so is not a path at all for a deeply nested file.
- **File manager** — browse/mkdir/rename/move/delete/duplicate, wired so renames
  and moves keep the open editor in sync.
- **Formatters** — `agent_server/formatting.py` dispatches to clang-format
  (C/C++/C#/Java/ObjC/proto), black (Python, target pinned to the running
  interpreter), prettier, rustfmt, gofmt, shfmt, and JSON. Missing binary →
  "install X".

## UI chrome

- **Themes** — presets green/red/blue/gray + a custom colour picker. `data-theme`
  overrides CSS vars; accent is split into `--accent` (text) vs `--accent-btn`
  (buttons); custom hex is derived server- and client-side.
- **Notifications** — per-tab status dot (blue pulsing = working, amber = needs you,
  green = done, red = error) plus a short synthesised tone on transitions.
- **Sounds** — upload/play/delete custom sound files (`routes/sounds.py`).
- **Collapsible tool/reasoning blocks** — `<details>` with a disclosure arrow,
  collapsed by default except auto-expand tools (`write`, `edit`), with a per-tool
  "expand by default" panel and persistence (`POST /_settings/expand`).
- **Tool progress** — while a large `write` streams, a `tool_progress` event shows
  the tool name + argument byte count. Full live rendering of the arguments was
  deliberately skipped.

## Keyboard shortcuts

- One table in `web_ui/static/js/app.js` (`Keys`), one handler, one place to look
  them up. Groups: Sessions, Writing, Files, Running, Help.
- Everything is rebindable, because the useful combinations are exactly the ones
  a browser or window manager may already have claimed, and which ones those are
  depends on the machine. Click a shortcut and press the keys; Esc cancels,
  Backspace unbinds.
- Combos are normalised from `event.code` (`Alt+BracketRight`), so a binding
  follows the physical key and survives a non-US layout.
- Defaults are chosen against what browsers keep for themselves. Firefox on
  Linux takes Alt+1-8 for its own tabs, and Alt+F/E/V/S/B/T/H open its menus, so
  neither appears as a default; Alt+D and Ctrl+E/Ctrl+L are the address bar.
  Rebinding onto a known-reserved combo is flagged rather than blocked -- which
  of them actually bite depends on the browser.
- "Jump to session 1-9" binds a *prefix* (Alt+Shift by default) and answers to
  prefix+1 through prefix+9, so nine shortcuts cost one rebindable row.
- Two actions sharing a key is flagged in the panel, defaults included -- unless
  both carry a `when` guard that makes them mutually exclusive, which is how
  Escape leaves the composer *or* stops the run without ambiguity.
- Stopping every session is `Ctrl+Alt+Shift+Escape`: deliberately awkward,
  because it aborts every run and every subagent at once and the cost of
  fumbling it is all of that work.
- Defaults live in the JS, not the database: only deliberate overrides are
  stored (`GET`/`POST /_settings/keybinds`), so a better default in a later
  version still reaches everyone who never rebound it.
- A shortcut does not fire while typing unless it opts in (`whileTyping`), and
  may carry a `when` guard so two actions can share a key in different states —
  Escape leaves the composer, or stops the run, depending on what is happening.
- Listed at the bottom of the home page and, from any page, on `?`. Groups are
  collapsed by default.

## Prompts and profiles

- **Profiles** (`/prompts`) — three built-in prompt profiles, a shared preferences
  block, and compaction instructions. Clearing a field restores the default.
- **Environment grounding** — every prompt ends with an auto-generated snapshot
  (cwd, platform, date, git status, top-level contents), so the model doesn't invent
  paths.
- **Prompt edits are queued**, adopted at each session's next compaction (when the
  prefix is rewritten anyway), so a shared edit never disturbs a running session.
- **The whole tool array is frozen per session** the same way, in
  `sessions.tool_schemas`. Tools sit at the very front of a request, so anything
  about them that changes -- a description, a parameter, a custom tool being
  edited, a tool being enabled -- moves the first byte of the prefix and re-bills
  the entire conversation. Only the descriptions used to be frozen, which was
  worse than freezing nothing: the parameters went on changing underneath, so a
  session could send a tool whose frozen description told the model to pass
  arguments its own live schema no longer accepted, and every call it made was
  rejected. A session whose tools have moved on shows "Use the updated tools" in
  its menu; adopting is a confirmed action, because it costs a full-context pass,
  and compaction does it for free.
- The page lists every tool with its token cost (schemas are sent every request).

## Custom tools, scripts, secrets

- **Custom tools** — user-defined shell scripts with a JSON Schema, called by the
  model; arguments arrive as `$TOOL_ARG_NAME`. Callable per prompt profile. Loaded
  from the DB at startup.
- **Built-in tool descriptions** — the `/tools` page rewrites what the model is
  told a built-in tool does (edit + revert to default). Frozen per session like
  the system prompt, so an edit is adopted at the next compaction.
- **Scripts** — shell the *user* runs from the home page (never sent to the model,
  no schema). Start/stop daemons are the motivating example (`ollama-start`).
- **Secrets** — saved per-tool/script, exposed to their environment only.

## Providers and endpoints

- Adapters in `agent_server/providers/` for DeepSeek, Anthropic, OpenRouter, and any
  OpenAI-compatible endpoint, plus **custom endpoints** you define in the UI
  (`routes/endpoints.py`, `custom_openai.py`).
- `conversation.py` enforces the provider wire-format rules (tool-call shape,
  one `tool` message per `tool_call_id`, `reasoning_content` echo).
- DeepSeek models are discovered dynamically at startup so new releases need no
  code change.

## Context, compaction, cost

- **Compaction** (`compaction.py`) summarises long conversations, always cutting on
  a group boundary (a `tool_calls` message and its results are one atomic unit).
  Per-session threshold, adjustable with a slider or one-off instructions.
- The summary is produced by *continuing* the head of the conversation rather
  than rebuilding a transcript: the head is already a cached prefix, so the call
  is nearly free (24,284 uncached tokens against 58, measured on a 106,000-token
  session). The retained tail is simply not sent, so the summary cannot describe
  work that is also being kept verbatim — no instruction is needed telling the
  model to ignore messages it believes it wrote. Only a head too large to send
  falls back to a flattened transcript.
- **Cache guard** (`cache_guard.py`) predicts prefix-cache misses before they're
  paid for by diffing the about-to-send bytes against the last send.
- **Usage ring** — live token and cache stats from real API `usage` numbers.

## Internals

- **`dir_watcher.py`** — watchfiles/inotify to detect a project directory rename
  and re-point the session at its new location.
- **`capture.py`** — screen capture backends probed per platform.
- **`formatting.py`** — external formatter dispatch (see Editor).
- **`templating.py`** — the Jinja environment, filters, and theme derivation.

## Configuration

- Env (see `.env.example`): provider keys, `WHISPER_MODEL`, `WHISPER_SERVER_PORT`
  (8177), `CODEAGENT_DATA_DIR`, `CODEAGENT_DB`, `CODEAGENT_LOG_LEVEL`,
  `CODEAGENT_DICTATION_COMMIT_DELAY`, `CODEAGENT_DICTATION_PAUSE`, browser/vision
  paths.
- Runtime settings live in the `settings` DB table (theme, custom colour, expand
  tools, whisper model, sound, TTS tone, auto-approve, thresholds, …).

## Tests

```bash
.venv/bin/python -m pytest tests/ -q                    # unit tests, no network
.venv/bin/python -m pytest tests/test_live_agent.py -s  # hits the real API
```

`tests/route_inventory.json` pins the HTTP surface and is regenerated/checked by
`test_route_inventory.py` — update it whenever a route changes.
