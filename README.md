# CodeAgent

A personal coding agent: FastAPI + HTMX backend, DeepSeek V4 as the model, running
against your local filesystem. Minimal by design — a small tool set, a short prompt,
and per-session settings that don't leak into each other.

## Running it

```bash
uv venv && uv pip install -r requirements.txt
cp .env.example .env        # optional; the key can also be saved in the UI
ln -s "$PWD/bin/codeagent" ~/.local/bin/codeagent
codeagent                   # starts the server and opens the browser
```

| | |
|---|---|
| `codeagent` | start, and open a browser at it |
| `codeagent stop` | stop it cleanly |
| `codeagent restart` | stop it, then start in this terminal |
| `codeagent status` | running or not, and on what pid |
| `codeagent open` | open a browser at one already running |

Ctrl-C stops a server started in this terminal; `codeagent stop` stops one
started elsewhere. Both run the shutdown hook, which is what unloads the vision
model from the rig. `kill -9` skips it, and the model then sits in memory until
Ollama expires it.

Add your DeepSeek API key on the home page (or set `DEEPSEEK_API_KEY`, which wins),
pick a project directory, and create a session.

**This is a single-user tool with no authentication.** It reads and writes anywhere
your user account can and runs arbitrary shell commands. Bind it to `127.0.0.1`
(the default) and do not expose it to a network.

## How it works

```
agent_server/
  agent.py          the conversation loop: stream, call tools, pause, resume
  conversation.py   DB rows <-> provider wire format
  compaction.py     summarising long conversations
  database.py       SQLite (one connection, WAL)
  system_prompt.py  prompt profiles + environment grounding
  stt.py            whisper.cpp transcription
  providers/        one adapter per model vendor
  permissions.py    what the agent may do without asking
  vision.py         image normalisation, browser capture, Ollama client
  tools/            read, edit, write, bash, grep, glob, webfetch,
                    question, task, vision, screenshot
  routes/           HTTP surface
web_ui/             Jinja templates, CSS, and ~4 files of vanilla JS
```

A turn runs like this: your message is persisted, the full transcript is serialised
to the wire format, the provider streams back reasoning/content/tool calls, tools
execute, and the loop repeats until the model answers. Every step is written to
SQLite as it happens, so the stored transcript always matches what was actually
sent to the API.

Independent read-only calls (`read`, `grep`, `glob`, `webfetch`, `task`, `vision`,
`screenshot`) run concurrently, so three subagents cost the slowest one rather than
the sum. Anything that mutates state runs sequentially, because parallel writes to
one file are not safe.

### Runs outlive the request

A run is a task owned by the server; clients subscribe to it over SSE. Closing the
tab, reloading, or switching sessions only unsubscribes — the turn keeps going and
its results are still recorded. Reopening a running session attaches to it and shows
the calls still outstanding. Only an explicit stop ends a run, and stopping cancels
the work immediately rather than waiting for it to notice a flag.

Every tool call in a cancelled batch is still given a result. An unanswered tool
call is treated as pending work and re-run on the next message, which is how a
stopped batch would otherwise restart itself.

### Messages sent while it is working

The composer stays live during a run. A message sent mid-turn is held in memory and
injected at the next turn boundary — never between an assistant `tool_calls` message
and its results, which would make the request invalid. Several queued messages are
delivered as one.

Because nothing is persisted until that moment, a pending message can be taken back:
undo removes it and returns the text to the composer, and the model never learns it
existed.

### The provider contract

DeepSeek's thinking mode has three rules that produce hard 400s if you get them
wrong. `conversation.py` exists to enforce them:

1. `tool_calls` must be `{"id", "type": "function", "function": {...}}`. The flat
   `{"id", "name", "arguments"}` shape fails with *missing field `type`*.
2. Every `tool_call_id` needs exactly one matching `role: "tool"` message.
3. `reasoning_content` must be echoed back on any assistant turn that called a tool.

`temperature` and `top_p` are accepted but ignored in thinking mode, so this app
does not send them. Effort is controlled by `reasoning_effort`
(`none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`).

### Permissions

Two independent gates, because they protect against different things.

**Shell.** Read-only commands (`ls`, `cat`, `git status`, ...) run silently.
Anything that can redirect, chain, or mutate asks first. You can approve once,
approve everything for the rest of the server process, or reject. A rejection is
fed back as a tool result so the model can adapt instead of the conversation
dead-ending.

**Filesystem writes outside the project directory.** These *always* ask, and
shell auto-approval deliberately does not cover them — letting an agent run
`npm test` in your repo is not the same as letting it rewrite `~/.ssh/config`.
You can allow once, or allow a directory for the rest of the session (it offers
the enclosing git repo when there is one). `/proc`, `/sys`, `/dev`, `/boot` and
the sudoers files can never be granted.

**Every grant is scoped to one session.** Nothing you allow in one session
applies to another, and deleting a session drops its grants. Shell auto-approval
and the writable-directory list both live in that session's ⋮ menu.

Pauses are derived from unanswered tool calls in the database rather than held in
memory, so reloading the page mid-prompt re-offers the same decision, and a
crash cannot strand a session with a half-finished turn.

### Images and vision

Attach images with the paperclip; they are saved and referenced by path in your
message. The agent then calls `vision` on those paths itself, choosing its own
question — you do not write a vision prompt. Uploads are decoded and re-encoded
as PNG rather than trusted by extension, because browsers routinely hand over a
WebP named `.jpg` and the Ollama backend rejects WebP outright.

`screenshot` captures a running page: a single frame, a cropped selector, a full
scrollable page, or a timed sequence (`count` and `interval_ms`) for animations
and loading states. `actions` can click, fill, hover, press or scroll first to
reach a particular state. It returns file paths.

`vision` accepts several paths at once and labels each by filename, which is how
comparison works — capture before and after, then ask what changed. Note that
the backend only reliably sees multiple images when each is sent on its own
message, which this client does for you.

If the vision machine is switched on, Ollama is started on it automatically --
at boot, and again on the first vision call if it went away since. That needs
key-based SSH and runs the binary as your user, with no sudo. Switch the machine
off and vision reports being unreachable instead of hanging; nothing else is
affected. Set `VISION_AUTOSTART=0` to manage it yourself.

Vision runs against Ollama on `VISION_OLLAMA_URL` (default `vision-host.local:11434`,
model `qwen3-vl:32b`). Browser capture uses Playwright's async API in-process.

### Notifications

Tabs carry a status dot: blue and pulsing while the agent works, amber when it
needs you, green when it finished, red on error. A short synthesised tone plays
on the same transitions unless you turn it off in Preferences. The dot clears
when you look at the session.

### Dictation

If `whisper-cli` and `ffmpeg` are on `PATH`, a mic button appears. Click it to
toggle recording, or hold **Ctrl+Space** to push-to-talk. Releasing transcribes
and inserts at the cursor; pressing Enter while recording stops, transcribes and
sends in one go. A level meter sits on the bottom edge of the composer while
recording. Everything stays local; no audio leaves the machine.

Point `WHISPER_MODEL` at a different `ggml-*.bin` to trade accuracy for speed.

### Context and compaction

The ring in the session bar shows how much of your compaction threshold is in
use; hover it for exact tokens, the model window, cache hit rate, and session
spend. All of it comes from real `usage` numbers returned by the API, priced at
the model's cache-hit/cache-miss/output rates.

When a session crosses its threshold the run pauses and asks. The dialog has a
box for one-off instructions ("keep the deployment steps"), and the alternative
action is a slider that raises the threshold instead, from 4K up to the model's
full window. The threshold is stored per session.

The split always lands on a turn boundary — never between an assistant's tool
call and its results, which would corrupt the session permanently. Recent turns
are kept verbatim.

### Cost

DeepSeek bills a cached prompt prefix at roughly **1/120th** of an uncached one
($0.003625 vs $0.435 per 1M on V4 Pro), so essentially all of the cost control is
in keeping the prefix byte-stable. Two rules follow, and both are load-bearing:

* The system prompt must be identical on every request in a session. The
  environment block is therefore snapshotted once per session — an earlier
  version recomputed a live directory listing, and every file the agent created
  invalidated the whole conversation. Measured hit rate went from 11-26% back to
  95-97% when that was fixed.
* `reasoning_content` is echoed back only on assistant messages that carry
  `tool_calls`, which is exactly where the API requires it. The rule keys on an
  immutable property of the row, so a message's serialisation never changes
  after the fact.

Running totals and the overall cache hit rate are on the home page.

## Prompts

`/prompts` edits the three built-in profiles, a preferences block appended to all of
them, and the compaction instructions. Clearing a field restores the default. The
page also lists every tool with its token cost, since schemas are sent on every call.

Each prompt ends with an auto-generated environment block (working directory,
platform, date, git status, top-level contents). This matters: without it the model
will confidently invent absolute paths from its training data.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q                    # unit tests, no network
.venv/bin/python -m pytest tests/test_live_agent.py -s  # hits the real API
```

`test_conversation.py` covers the serialization rules above and the compaction
split, including that compaction always leaves a real window of recent turns and
never just a summary. `test_permissions.py` covers both gates, including the two
cases that matter most: shell auto-approval must not imply filesystem access, and
grants must not leak between sessions. `test_run_manager.py` covers the run
lifecycle: a late subscriber still sees the whole turn, no event is duplicated or
lost across the subscribe boundary, a cancelled batch records a result for every
call, and an undone message is never delivered. `test_bash_tool.py` pins the
background-process behaviour. `test_live_agent.py` runs four real conversations —
a greeting, a multi-round tool loop, a shell approval, and a rejection — which are
the scenarios that used to be broken.

The browser side is worth stress-testing with Playwright's CDP performance
metrics when you touch streaming, timers, or the microphone. Three leaks got
through review because they only showed up under load or over time: markdown
re-rendered per token (O(n^2) once code blocks appear), a dictation race that
leaked an animation-frame loop, and a per-tool-call interval that kept firing
after its node was detached. The pattern is the same each time — something that
must stop has no owner once the DOM changes underneath it — so check that timers
end themselves when `isConnected` goes false. Headless Chromium has no
microphone, so run the mic path with
`--use-fake-device-for-media-stream` or it stays untested.

Two-session behaviour needs a browser: start a run in one session, switch to
another, come back, and confirm the run is still going and still visible.

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
