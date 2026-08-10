# Task: collapsible tool blocks and thinking blocks in the transcript

A long session is mostly `read` and `grep` output the user has already scrolled
past. Everything should be collapsed by default, except the thing currently
happening.

All of this is `web_ui/static/js/app.js`, `web_ui/static/css/style.css`, and the
transcript templates. No new routes except one settings save. Read how a message
is rendered and how SSE appends to it before starting.

Run throughout — both must hold at the end:

```
.venv/bin/python -m pytest tests/ -q     # currently 317 passed, 4 deselected
.venv/bin/python -m ruff check .         # currently All checks passed!
```

## 1. Collapsed by default

Every tool block and every thinking block renders collapsed: a one-line header
showing the tool name and its existing title (e.g. `read src/app.py (412 lines)`),
plus a disclosure arrow. Clicking toggles it. Use `<details>`/`<summary>` — the
keyboard and accessibility behaviour is free, and `.schema-details` in
style.css is a worked example of the styling.

Do not animate the toggle. Do not lazy-render the body; it is already in the DOM.

## 2. The last assistant response is always expanded

Non-negotiable and the point of the feature: the user watches thinking and tool
output stream live.

- While a turn is running, its blocks are expanded.
- When the **next** assistant response begins, the previous one collapses —
  unless its tool type is set to expand by default (section 3).
- A block the user expanded or collapsed by hand keeps that state for the rest
  of the session. Never fight a manual choice; track it per block.

The streaming path appends to blocks as text arrives. Make sure an expanded
streaming block stays scrolled to its newest content without yanking the page
if the user has scrolled up.

## 3. Per-tool defaults

A settings panel on the home page, in the Preferences section: one checkbox per
tool, "expanded by default". Persist as a single settings row — a JSON object of
`{tool_name: true}` — via one new `POST /_settings/collapse` route, following
`/_settings/sound` exactly. **Add the route to `tests/route_inventory.json` in
the same commit.**

Build the tool list from `agent_server.tools.registry.TOOLS` so custom tools
appear automatically. Do not hardcode names.

Sensible shipped defaults: `edit` and `write` expanded (the user wants to see
diffs), everything else collapsed. Thinking blocks collapsed.

## 4. "Collapse all" in the three-dot menu

An item in the existing session menu (`web_ui/templates/components/session_meta.html`)
that collapses every block in the current transcript, including the last
response and including blocks the user expanded by hand. It is an explicit
instruction, so it overrides the manual-state tracking in section 2.

## 5. Expanded blocks are a fixed-height scroll window

They already are — `max-height` with `overflow: auto`. Confirm it applies to
tool output and thinking blocks and that the height is a sensible reading size
(roughly 300–400px), not the whole viewport. If a block can currently grow
unbounded, cap it. A 17,000-line file must not fill the chat.

Use `var(--s*)` spacing tokens and `var(--fs-*)` type tokens. No bare pixel
values except the max-height. No new colours.

## Tests

`tests/test_collapse.py`, for the parts that are testable in Python:

1. The settings route round-trips the JSON and rejects a body that is not an
   object.
2. Unknown tool names in stored settings are ignored rather than raising.
3. The default map has `edit` and `write` expanded and `read` collapsed.

The behavioural half is browser work. Verify it by driving the app and say in
your final message what you checked:

```
mkdir -p /tmp/collapse-check
CODEAGENT_DATA_DIR=/tmp/collapse-check .venv/bin/python -m uvicorn agent_server.main:app --port 8296 &
```

Check by hand, with a session that has several tool calls: blocks start
collapsed; the newest response is expanded while streaming; it collapses when
the next one starts; a manually expanded block stays expanded; "Collapse all"
closes everything; a huge file does not fill the chat.

## Definition of done

- 317 tests + yours, 0 failed
- `ruff check .` clean
- `route_inventory.json` updated with the one new route and nothing else
- No `window.alert/confirm/prompt` (use `ui.*` in app.js)
- Templates balance their tags — `test_every_page_template_closes_its_tags`
  will catch it, but check before you get there

Report: what you changed in each file, the shipped default map, and anything
that looked wrong but was out of scope.
