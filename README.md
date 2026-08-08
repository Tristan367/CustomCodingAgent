# CodeAgent

A personal coding agent: FastAPI + HTMX backend, DeepSeek V4 as the model, running
against your local filesystem. Minimal by design — a small tool set, a short prompt,
and per-session settings that don't leak into each other.

## Running it

```bash
uv venv && uv pip install -r requirements.txt
cp .env.example .env        # optional; the key can also be saved in the UI
./run.sh                    # http://127.0.0.1:8080
```

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
  tools/            read, edit, write, bash, grep, glob, webfetch,
                    question, todowrite, task, vision
  routes/           HTTP surface
web_ui/             Jinja templates, CSS, and ~4 files of vanilla JS
```

A turn runs like this: your message is persisted, the full transcript is serialised
to the wire format, the provider streams back reasoning/content/tool calls, tools
execute in order, and the loop repeats until the model answers. Every step is
written to SQLite as it happens, so the stored transcript always matches what was
actually sent to the API.

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

### Tool approval

Shell commands pause for approval by default. Read-only commands (`ls`, `cat`,
`git status`, ...) run without asking; anything that can redirect, chain, or
mutate prompts you. From the prompt you can approve once, approve everything for
the rest of the server process, or reject — a rejection is fed back to the model
as a tool result, so the conversation stays usable.

The per-session options menu (⋮) toggles this permanently, renames the session,
compacts the conversation, and changes model/effort/profile.

Pauses are derived from unanswered tool calls in the database rather than held in
memory, so reloading the page mid-prompt re-offers the same decision.

### Dictation

If `whisper-cli` and `ffmpeg` are on `PATH`, a mic button appears. Toggle it on to
record (with a live level meter), toggle it off to transcribe and insert at the
cursor, or just press Enter — that stops the recording, transcribes, and sends.
Everything stays local; no audio leaves the machine.

Point `WHISPER_MODEL` at a different `ggml-*.bin` to trade accuracy for speed.

### Compaction

Long conversations can be summarised from the ⋮ menu. The split always lands on a
turn boundary — never between an assistant's tool call and its results, which
would corrupt the session permanently. The last few turns are kept verbatim.

The context/cost readout in the session bar uses real `usage` numbers from the API,
priced per the model's cache-hit/cache-miss/output rates.

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

`test_conversation.py` covers the serialization rules above. `test_live_agent.py`
runs four real conversations: a greeting, a multi-round tool loop, a shell approval,
and a rejection — the scenarios that used to be broken.

## Adding things

**A tool** — write an `async def handler(ctx, *, ...) -> ToolResult` in
`agent_server/tools/`, then `register(Tool(...))` in `registry.py`. Set
`pause="permission"` if it should ask first.

**A provider** — subclass `Provider` in `agent_server/providers/`, yield
`StreamEvent` dicts, and add it to the registry in `providers/__init__.py`.
Never raise out of `chat_completion`; yield an `error` event, because an exception
thrown after SSE headers are sent reaches the browser as an opaque stream error.
