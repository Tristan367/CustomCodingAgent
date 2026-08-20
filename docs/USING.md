# What you can do with MyriadCode

Written for the person using it, not the person building it. If you want the
engineering detail, read [FEATURES.md](FEATURES.md); if you want to change the
code, read [DEVELOPING.md](DEVELOPING.md).

---

## "I want to do X" — the app probably already does it

The most expensive thing that can happen here is you building a workaround for
something already built. Start with this table.

| If you find yourself… | Use this instead |
|---|---|
| Pasting the same command to the agent every session | A **custom tool** — the agent calls it by name, forever |
| Running the same shell command yourself over and over | A **saved script**, bound to a **keyboard shortcut** |
| Asking the agent to remember a build step, a deploy, a hardware quirk | A **custom tool**, so it is part of the agent rather than part of the conversation |
| Wanting two agents to work together for hours | **Inter-session messaging** — they talk to each other directly |
| Wanting one strong model to plan and cheap models to execute | A **planner session** messaging two or three **worker sessions** |
| Wanting an agent to fan out over a big search | **Subagents**, configured per tier in a profile |
| Re-explaining your conventions in every new session | Put it in a **profile's system prompt** |
| Watching a long command finish with no idea how it is going | Bash **streams its output live** already |
| Losing the start of a long conversation | **Compaction** summarises it rather than dropping it |
| Copying a whole file into the chat to ask about it | The agent can `read` it — just give it the path |
| Wanting to hand your setup to a colleague | **Export the profile** — it takes its custom tools with it |
| Typing a long instruction | **Dictation** — Ctrl+M, runs locally on your GPU |

---

## Getting started

1. Add an API key on the home page. Any one will do: DeepSeek, Anthropic,
   OpenRouter, or Google Gemini. Only models whose provider has a key appear in
   the picker, so you cannot accidentally start a session that cannot run.
2. Give a session a name and a project directory. **Browse** opens the full file
   manager — you can create, rename and tidy folders there before you start.
3. Type. The agent works in that directory and nowhere else unless you say so.

The model dropdown groups by who serves the model, because OpenRouter resells
most of them: "Claude Opus 5" under **Anthropic** and under **OpenRouter** are
two different routes, two different keys, two different prices.

---

## The everyday loop

**Shell commands ask first.** You see the exact command and the directory it
will run in, and you can approve it once, approve everything for the session, or
reject it with a reason the agent will read and work around.

**Writes are fenced to your project directory.** Anything outside it asks, and a
few paths (`/etc`, your home directory itself, block devices) are never allowed
at all.

**The transcript never moves under you.** Blocks expand downward, thinking is
drawn over the space below the conversation rather than pushing it, and a long
session draws its last 60 messages with a **Show earlier messages** link.

**Stop** ends the turn immediately — every tool call still records a result, so
nothing is left half-answered, and you can carry straight on.

**Closing the tab does not stop the work.** Runs belong to the server. Close the
browser, reboot the browser, come back tomorrow: the turn kept going and the
transcript is there.

---

## Custom tools — the thing to understand first

A custom tool is **a shell script and a JSON schema**. Arguments arrive as
environment variables, stdout goes back to the model. That is the entire
contract. No plugin API, no SDK, no restart.

This matters more than it sounds. Every coding agent has `read`, `write` and
`bash`. What makes one *yours* is the twenty-line script that talks to your
build, your deploy, your hardware, your weird internal API. Once it is a tool,
the agent uses it by name and you stop explaining it.

- **Secrets live with the tool** that needs them, not in a global env file.
- **Tools can ask permission** or run silently, per tool.
- **Tools are opt-in per profile**, because every schema is sent on every
  request — a tool a profile will never use costs tokens on every turn.

If you are about to type the same command into the chat for the third time,
that is the moment to make it a tool.

---

## Saved scripts, and running them with a key

A **script** is a saved shell snippet you run yourself, from the home page — not
something the agent calls. Start your inference rig, tail a log, reset a
database.

Any saved script can be **bound to a keyboard shortcut**. Open the shortcuts
overlay (`?`), find it under **Scripts**, click, and press the keys you want.

Scripts ship with no key on purpose: a shortcut nobody chose that runs shell is
not a feature. Binding it *is* the confirmation, which is why running one from
its key does not ask again. You get a small toast when it finishes, or the
failure output if it did not.

---

## Sessions that talk to each other

This is the feature with no real equivalent elsewhere, and it is worth
understanding properly.

Any session can send a message to any other session **by name**, using the
`send_message` tool. The receiving session wakes up and handles it as an
ordinary message. That is the whole mechanism, and everything below falls out
of it.

**Ping-pong.** Two agents hand work back and forth. One writes, the other
reviews and sends it back with objections. They can keep this up for hours
without you.

**Persistent subagents.** Ordinary subagents live and die inside one turn. But a
*session* is permanent. So: run one session on a strong model whose only job is
to plan and delegate, and two or three sessions on a cheap fast model that do
the work and report back. You have a hierarchy that survives compaction,
restarts, and you going to bed — which a subagent tree does not.

**A specialist you keep around.** A session pinned to your test suite with a
profile that knows your conventions. Message it when you need it; it already has
the context.

Elsewhere this is a paid service with a visual node graph. Here it is a tool
call.

---

## Subagents and profiles

A **subagent** is a fresh agent launched by `task` for one piece of work, with
its own context, reporting back a single answer. Good for anything wide: search
a large tree, check twenty files, try three approaches at once.

A **profile** decides how that works. It holds:

- The **system prompt** for the main agent.
- A separate **subagent prompt**, per tier — what a subagent is told is not what
  the main agent is told.
- **Tiers**: subagents, sub-subagents, and so on, each with their own prompt,
  model, thinking effort, and tool list.
- **Spawn limits**: how many the main agent may launch, how many each tier may
  launch, and a cap on the total running at once.
- Which tools any of them may use.
- The **summarising prompt** used when the conversation is compacted.

A session picks one profile. Editing a profile reaches the sessions using it at
their next compaction, rather than mid-conversation — changing the prompt
mid-conversation would re-bill the whole context at the uncached rate.

**Designing a hierarchy is genuinely fiddly**, and a good use of the agent
itself: open a session, point it at this page, and describe the shape you want.

---

## Sharing a setup

**Export** on the Profiles page gives you one JSON file containing the profile,
its summarising prompt, its whole subagent hierarchy, and every custom tool a
session on it would actually have.

**Import** takes that file back. It shows you what it would do first: which
names already exist and would be replaced, and **the full text of every script**
in the bundle. Imported tools arrive switched off — an imported tool is shell
from someone else, and turning it on should be a decision you make, not
something that happens because you clicked import.

---

## Context, and what happens when it fills

Every session has a **context ring** in the header: how full the model's window
is, and the cache hit rate.

When it passes the threshold, the conversation **compacts**: the older part is
replaced by a summary written by the model, and the recent part is kept
word-for-word. The default threshold is worked out per model, from how much room
one more round needs — a model that can reply with 128,000 tokens needs more
headroom than one capped at 8,192.

Two things you can change per session, from the three-dot menu:

- **The threshold** — when it compacts. Set it above the model's window to only
  ever compact by hand.
- **How much is kept verbatim** — as a percentage. More keeps the last stretch
  of work exactly as it was; less leaves more room before the next compaction.

You can also compact by hand at any time, and edit the summarising prompt per
profile.

A **cache guard** warns before a request would throw away a large cached prefix,
because re-reading a cached context at the miss rate is roughly 120× the price.

---

## Files, editing, and dictation

- **File manager** (the folder icon, or `Alt+O`) — browse, open, rename, move,
  copy, delete, create. Multi-select and drag to move.
- **Editor** (`Alt+C`) — syntax highlighting, `Ctrl+S` to save, and a **Format**
  button that runs the right formatter for the file type (black, prettier,
  gofmt, rustfmt, clang-format, shfmt). It remembers your open file, unsaved
  text, and cursor per session.
- **Attachments** — drag a file into the composer, or `Alt+A`. Images are shown
  to models that can see them.
- **Dictation** (`Ctrl+M`) — local speech-to-text on your own GPU, nothing sent
  anywhere. Pick a model size on the home page; bigger is more accurate and
  slower, and you are told the download size before it starts.

---

## Keyboard shortcuts

Press `?` for the full list. Everything is rebindable — click a shortcut and
press the keys you want. Combinations your browser keeps for itself are flagged
rather than silently swallowed.

The ones worth learning first:

| | |
|---|---|
| `Alt+]` / `Alt+[` | Next / previous session |
| `Alt+Shift+1…9` | Jump straight to a session |
| `Alt+N` | Home / new session |
| `Alt+I` | Focus the message box |
| `Ctrl+M` | Dictation |
| `Alt+O` / `Alt+C` | File manager / editor |
| `Alt+P` / `Alt+Shift+T` | Profiles / Custom tools |
| `Esc` | Stop the run |
| `Ctrl+Alt+Shift+Esc` | Stop **every** session — deliberately awkward |

---

## Preferences

On the home page: a notification sound (thirteen of them, or upload your own),
volume, colour theme including a custom colour, which tool results open
automatically, and whether past thinking and tool calls stay visible in the
transcript.

---

## Where things live

- Your data: `~/.local/share/codeagent/` — the database, logs, browser state.
  Outside the checkout, so cleaning the repo cannot destroy it.
- The server: `127.0.0.1:8219`.
- Start it: `myriadcode`. Stop it: `myriadcode stop`, or the button on the home
  page.
