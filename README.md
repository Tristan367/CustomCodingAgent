# MyriadCode

A personal coding agent you run on your own machine. FastAPI + HTMX, no
framework, no account. It does the usual things — the agent reads and edits
files, runs shell commands, drives a browser — but what it's built around is
that you can configure every part of it, and the agents talk to each other.

## What makes it different

### Agents that talk to agents

`send_message` lets any session message another **by name** — an idle session is
woken up, a busy one queues the message in a mailbox for its next turn.
Broadcast sends one message to every session at once. Split a job across
sessions and let them hand off to each other instead of doing it all in one
thread.

### A sub-agent hierarchy you define

`task` spawns a sub-agent that can spawn its own, up to a tier hierarchy you
configure (`sa_tool`, `sa_tool_2`, …). Each tier gets its own system prompt,
model, disabled-tool list, and parallel cap. `explore` is the read-only
research agent.

### Everything is custom

- **System prompts** — three profiles plus a shared preferences block, fully
  editable; clear a field to restore the default.
- **Tools** — custom tools are shell scripts with a JSON Schema, arguments
  arriving as environment variables (`examples/echo-tool.sh` is a minimal one).
  You can also rewrite what the model is told a *built-in* tool does.
- **Endpoints** — DeepSeek, Anthropic, OpenRouter, or any OpenAI-compatible
  endpoint you define in the UI.
- **Secrets** — stored per tool/script and injected into only that tool's
  environment.

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

Environment knobs are in `.env.example` (provider keys, whisper model, tool and
compaction limits). Runtime settings live in the UI. Your data lives in
`~/.local/share/codeagent/` — override with `CODEAGENT_DATA_DIR`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q                    # unit tests, no network
.venv/bin/python -m pytest tests/test_live_agent.py -s  # hits the real API
```
