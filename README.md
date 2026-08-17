# MyriadCode

A personal coding agent you run on your own machine. FastAPI + HTMX, no
framework, no account. The point isn't one chat window that edits files — it's a
fleet of agents that you configure completely and that talk to each other.

## What makes it different

### Agents that talk to agents

`send_message` lets any session message another **by name** — an idle session is
woken up, a busy one queues it in a mailbox for its next turn. Broadcast sends
one message to every session at once. Split a job across sessions and let them
hand off to each other instead of doing it all in one thread.

### A sub-agent hierarchy you define

`task` spawns a sub-agent that can spawn its own, up to a tier hierarchy you
configure (`sa_tool`, `sa_tool_2`, …). Each tier gets its own system prompt,
model, disabled-tool list, and parallel cap. `explore` is the read-only
research agent for when a sub-agent shouldn't touch anything.

### Everything is custom

- **System prompts** — three profiles plus a shared preferences block, fully
  editable; clear a field to restore the default.
- **Tools** — custom tools are shell scripts with a JSON Schema, arguments
  arriving as environment variables. You can also rewrite what the model is told
  a *built-in* tool does, and revert to the default.
- **Endpoints** — DeepSeek, Anthropic, OpenRouter, or any OpenAI-compatible
  endpoint you define in the UI.
- **Vision** — there is no hardcoded vision model. `browser` and `capture`
  dispatch to whatever tool you name `vision`, so it runs on your GPU, your
  cloud account, or nothing at all. An Ollama example ships in `examples/`.
- **Secrets** — stored per tool/script and injected into only that tool's
  environment.

### Attachments are just paths

Attach files *or directories* with the paperclip; the agent is handed the
absolute path and decides what to do with it. Drag-and-drop anywhere, image
previews, sizes, reorderable, one-button clear.

### The boring stuff is handled

Standard tools are all here (read, write, edit, bash, grep, glob, webfetch,
websearch, a browser it can drive) behind per-session permissions and a
`rm -rf /` guard — but that's table stakes, not the point.

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

Add your API key on the home page (or set `DEEPSEEK_API_KEY`, which wins), pick
a project directory, and create a session.

> **This is a single-user tool with no authentication.** It reads and writes
> anywhere your user account can and runs arbitrary shell commands. Bind it to
> `127.0.0.1` (the default) and do not expose it to a network.

## Configuration & data

Environment knobs are in `.env.example` (provider keys, whisper model, the
`VISION_*` block, tool/compaction limits). Runtime settings live in the UI.
Your data lives in `~/.local/share/codeagent/` — override with
`CODEAGENT_DATA_DIR`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q                    # unit tests, no network
.venv/bin/python -m pytest tests/test_live_agent.py -s  # hits the real API
```
