# Working on MyriadCode

For whoever is changing the code — very often an agent. [FEATURES.md](FEATURES.md)
is the index of *what* exists; this is *how to work on it*, and the list of
things that have already gone wrong so they do not go wrong again.

---

## Orientation

```
agent_server/
  agent.py          the turn loop: provider -> tools -> provider, SSE events out
  conversation.py   stored rows <-> wire format. The tool-call invariants live here
  compaction.py     summarising the older part of a session
  permissions.py    what may be written where
  providers/        one adapter per vendor; openai_compat is the base for most
  tools/            the built-in tools; registry.py assembles the schemas
  routes/           FastAPI; context.py builds what the templates render
web_ui/
  templates/        Jinja. base.html wraps everything; chat_messages.html is the transcript
  static/js/app.js  the whole front end, ~6k lines, no framework, no build step
tests/              pytest; a few drive a real browser
```

Run it: `.venv/bin/python -m pytest tests/ -q` and `.venv/bin/python -m ruff check .`
Both must be clean. There is no build step — edit a file and reload.

---

## The rules that are not obvious

### Templates reload; Python does not

Jinja re-reads templates from disk on every render, but the running process
keeps the Python it started with. **Edit a template and a context builder in the
same change, and the live server renders the new template against the old
context.** This has produced a 500 on the home page and a silently empty
dropdown. Restart the server after any Python change.

### The transcript must never move under the reader

This is a hard constraint, not a preference, and there is a browser test suite
enforcing it (`tests/test_transcript_anchoring.py`).

- A block that expands grows **downward**. The header stays where it was.
- Anything that streams (thinking, live command output) is drawn **out of the
  layout**, over the tail padding, so it costs one line however long it gets.
- `#messages` keeps 25vh of padding below the last message for that to grow into.
- **Do not reach for `content-visibility: auto` or hand-rolled virtualisation.**
  Both were tried. Both make the browser estimate off-screen heights, which
  reintroduces exactly the jumping this is all about and breaks the restored
  scroll position. Windowing the *number of rows drawn* is the approach that
  works.

### The foot of the transcript is two rows, and they are the same height

While a turn runs, everything below the last thing the agent said is: the most
recent tool call, and the live line. Both are `--live-row` tall, and so is every
state either can take.

- **Any new transient row must use that height.** The live line, the progress
  counter, a running call and a one-line thinking block once measured 28, 31, 30
  and 40px. They replace each other several times a second, so each swap moved
  the whole transcript by the difference. `tests/test_live_region.py` measures
  all four and fails on a spread over 1px.
- **The live line is blanked, never removed, mid-turn.** It used to be cleared
  by every event and restored by only some, so between a call finishing and the
  next round starting the foot lost a row and got it back. `syncLiveLine` runs
  once after each event and decides who owns the slot, so the handover happens
  inside a single handler and there is never a frame with neither.
- **A finished call is hidden only when the next one starts** -- in the same
  handler that appends the replacement, so the row going out and the row coming
  in cancel. Do not hide it when it finishes.
- **Only one block may hold the `.live` overlay.** Overlays are positioned from
  their own row's top and grow to the same height, so two of them 30px apart
  cover each other almost entirely -- a streaming thinking block once painted
  over 86% of an open diff. `takeLive` is the only way in, and it clears every
  other holder regardless of kind.
- **Markers hang in the gutter.** A spinner dot in the flow indents its row past
  the assistant text beside it, and removing it when the call ends moves the
  label sideways. Position it, and hide it rather than removing it.
- **Streaming content is anchored to the top of its box**, and follows only if
  the reader scrolled that box to the bottom themselves.

There is deliberately *no* compensation for an auto-expanded result arriving at
full height. `edit` and `write` ship collapsed; asking them to open is asking to
watch the page move.

### `enabled` on a custom tool is a fact about this machine

Not about the profile. `apply_bundle` switches every imported tool off on
purpose, so filtering an export by `enabled` made bundles lossy in one step: a
bundle that had been imported once exported without its scripts, and whoever it
was passed to next got a profile referring to tools that did not exist.

### Windowing the view must never narrow what the model sees

The transcript draws the last N messages. The model's request is assembled from
`db.get_messages()` and is completely separate. If those two ever meet, the
agent silently forgets the start of every long session with nothing on screen to
say so. There is a test whose only job is to assert this.

### Tool calls and their results are one unit

An assistant message carrying `tool_calls` and the `tool` rows answering it can
never be separated — not by compaction, not by windowing, not by anything. A
kept window must never begin with an orphaned tool result, and no tool call may
be left unanswered; an unanswered call is treated as pending work and re-run on
the next message, forever. `conversation.pending_tool_calls` is the check.

### `token_count` is not set for user messages

It comes from the usage a provider reports, and nothing reports a cost for what
the user typed. Every reader that does `row["token_count"] or 0` is pricing user
turns at zero. This broke compaction twice. Use `compaction.message_tokens`,
which estimates from content when nothing measured it.

### `disabled_tools = NULL` does not mean "nothing disabled"

It means the profile has never been configured, and such a profile offers **no
custom tools at all**. Reading the column directly has now caused two separate
bugs. Use the same rule `_effective_disabled` uses.

### Editing a prompt mid-conversation re-bills the whole context

The system prompt sits at the front of the cached prefix. Changing it
invalidates the cache, and re-reading a large context at the miss rate is
roughly 120× the price. That is why prompt edits are queued onto
`pending_system_prompt` and adopted at the next compaction, where the prefix is
being rewritten anyway.

---

## Providers

Most adapters subclass `OpenAICompatibleProvider`; Anthropic has its own.
Everything here was found by sending a real request, and none of it was caught
by unit tests against fabricated responses:

- **Do not pass `stream=True` into `messages.stream()`.** It is already a
  streaming context manager. That single line meant Anthropic had *never worked*
  — every turn died with a `TypeError` before a request left the machine.
- **Vendor fields on a tool call must round-trip.** Gemini attaches a
  `thought_signature` to every function call and rejects the *next* request if
  it does not come back. Rebuilding a tool call from the canonical shape and
  dropping it makes the first call succeed and the round after it fail with a
  400 that names nothing recognisable. See `VENDOR_CALL_KEYS`.
- **Not every provider indexes its streamed tool-call fragments.** Gemini sends
  no `index`; defaulting it to 0 puts every call of a turn in one slot, so a
  turn asking for two tools runs only the second.
- **Finish reasons differ.** Anthropic's `max_tokens` / `tool_use` are
  translated to the OpenAI vocabulary in `providers/base.py`, because consumers
  match on that. Missing one meant the output-limit guard never fired.

If you add a provider, send it real work with a real tool call before believing
it works. A `/models` listing proves the key, nothing more.

---

## Compaction

The part most likely to break three hours into a session, so it gets its own
list.

- The headroom **above** the threshold must hold one more round: the model's
  output ceiling plus that round's tool results. `max_output` varies 16× between
  models, so the reserve is computed per model rather than assumed.
- The kept tail is a share of the threshold, never a flat constant. A flat 24K
  tail with a 16K threshold swallows the whole conversation: it summarises the
  single oldest round, frees nothing, and fires again next round while
  destroying history a message at a time.
- **The threshold and the tail budget are in different units.** The threshold is
  compared against `context`, which is the last request's `prompt_tokens` — the
  system prompt, every tool schema, *and* the messages. The tail budget can only
  be spent on the messages. The difference is fixed overhead the budget cannot
  reach, and it is not small: a long profile with a dozen custom tools is tens of
  thousands of tokens before the conversation starts. Derive the tail from the
  threshold alone and, on a small threshold, the budget exceeds every message
  there is — the walk keeps all of them and nothing is freed. No clamp written in
  threshold tokens closes this; clamp against the conversation you actually have.
- A compaction that summarised nothing will summarise nothing next time. Stop,
  and say so.
- **A failed compaction must not end the turn.** Returning there leaves the
  user's message with nothing answering it, and the next thing they type piles
  on behind it.
- An empty summary is not a transport error, so nothing retries it. Smaller
  models handed a short head answer with nothing surprisingly often. Ask again.
- **The summariser is sent the full tool schemas** so the cached prefix matches
  a normal turn — which means it can and will try to *use* them unless the
  prompt says not to. It will also narrate the task, and decline if the prompt
  frames everything around code.

The only way to test any of this is to force it: set a low threshold on a
scratch session and drive real turns through it. Every fault above was found
that way and none by reading.

---

## Testing

Unit tests are cheap and most of the suite. Two other kinds matter:

**Browser tests** (`tests/test_transcript_anchoring.py`) drive real Chromium
against a real server. They assert **numbers** — a row's y coordinate before an
action and after it — never wording or colour, so restyling cannot break them.
Playwright is already a dependency for the `browser` tool. If its Chromium is
missing the module skips rather than fails.

**Live provider work** is opt-in (`pytest -m live`) because it costs money.
Everything else runs on fabricated responses, which is exactly why the wire
faults above survived so long.

When you fix a bug, write the test that fails against the old code, and *check
that it does*. Several tests in this repo were verified that way; the comments
say which failure each one is standing guard over.

---

## Things that look like bugs and are not

- Rows *above* the viewport shifting when a block below them expands: that is
  the browser's scroll anchoring keeping the visible content still. Compare only
  rows the reader can see.
- The Save button vanishing in the editor: it is hidden when there is nothing to
  save; that *is* the dirty indicator.
- Creating a file from the file manager closing the manager: it opens the file
  for editing, which is the point of creating it.
- `Escape` in the editor raising a modal: unsaved changes, correctly.

---

## House style

Comments explain **why**, especially when the code looks odd — most of the
strange-looking things here are load-bearing and the comment says what broke.
Match the density of whatever file you are in. Keep `ruff` clean. Prefer the
loud, harmless failure over the clever silent one, particularly around files,
billing, and anything that deletes.
