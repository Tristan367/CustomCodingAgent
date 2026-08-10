# Task: saved user scripts, runnable from the UI

A place to keep small personal shell scripts — "stop the Ollama box", "restart
the dev server", "clear the cache" — and run one with a click instead of
opening a terminal and pasting.

**These are not custom tools.** Read `agent_server/tools/custom.py` and the
Tools page first so you can see the difference and reuse the runner:

| | custom tool | script |
|---|---|---|
| who calls it | the model | the user, by clicking |
| sent to the model | yes, costs tokens every request | never |
| needs | name, description, JSON parameter schema | name, body |
| arguments | `$TOOL_ARG_*` env vars from the model | none |

A script is deliberately the simpler thing. Do not give scripts parameters, a
description field, or a JSON schema. If you find yourself adding those, stop —
that is a custom tool and it already exists.

## Storage

New table, following the shape of `custom_tools` in `agent_server/database.py`
(look at how that one is declared and migrated; match it):

```
scripts(name TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at TEXT NOT NULL)
```

Add it to the migration list the same way the existing tables are added. Do not
write a bespoke migration path.

DB functions alongside the `custom_tools` ones: `list_scripts()`,
`get_script(name)`, `save_script(name, body)`, `delete_script(name)`.

## Routes

New file `agent_server/routes/scripts.py`, `router = APIRouter()`, included from
`main.py` like every other route module. **Add the new routes to
`tests/route_inventory.json` in the same commit** — that file pins the URL
surface and the test will fail until you do. It is the one file you are allowed
to update by hand here, and only by adding your new entries.

- `GET  /scripts` — the page
- `POST /_save_script` — upsert; name validated with the existing `_slug` helper
  in `agent_server/routes/context.py`
- `POST /_delete_script`
- `POST /_run_script` — run it, return the output

## Running

Reuse the subprocess handling in `agent_server/tools/custom.py` — the timeout,
the output capture, and the kill path are already written and already handle the
case where a script spawns children. Do not write a second copy.

Requirements:

- Run with `bash`, cwd = the app's base directory, inheriting the app's
  environment (so `.env` values are visible — that is the whole point for the
  Ollama case).
- **Timeout, default 120s**, killed with the existing kill helper.
- Capture stdout and stderr **separately** and show both.
- Return the exit code and show it. A script that fails must look different
  from one that succeeds — this is the main thing the UI is for.
- Stream nothing; a single response when it finishes is fine.
- Truncate output to something sane (match what the custom tool test panel
  does) and say when it was truncated.

## Confirmation

Running is a POST behind a confirmation dialog. Use the existing `ui.confirm`
in `web_ui/static/js/app.js` — do **not** use `window.confirm`, and do not write
a new dialog. The confirmation must show the script's name and its body, so the
user sees what they are about to run.

## Page

`web_ui/templates/scripts.html`, extending `base.html` like `custom_tools.html`
does. Add a "Scripts" link to the top bar next to Prompts and Tools.

Layout, following the existing pages so it does not look like a different app:

- A picker + Save / New / Delete row. `custom_tools.html` has exactly this in
  `.prompt-bar` — copy that structure.
- A `<textarea class="prompt-textarea">` for the body.
- A Run button, and an output area below it showing exit code, stdout, stderr.

**Style rules.** The CSS has a token system; use it and add no new colours:

- Spacing: `var(--s1)`…`var(--s6)`. Never a bare `8px`.
- Type: `var(--fs-micro|small|body|lead|title)`. Never a bare `13px`.
- Buttons: `.btn-primary` for Save, `.btn-danger` for Delete, plain `<button>`
  otherwise. Do not invent a button class. Do not put `style="..."` on anything.
- Success/failure: `var(--success)` and `var(--danger)`, which already exist.
- Monospace output: `var(--mono)`, on `var(--bg-inset)`.

Put page-specific rules at the end of `style.css` under a
`/* ── Scripts ── */` banner comment, matching the file's existing style.

## Tests

`tests/test_scripts.py`:

1. Save, list, get, delete round-trip through the DB functions.
2. A name that is not a valid slug is rejected.
3. Running a script returns its stdout.
4. **A failing script reports a non-zero exit code and its stderr.**
5. A script that runs longer than the timeout is killed and says so. Use a
   short timeout in the test; do not make the suite wait 120s.
6. Saving a script does not register a tool — assert the name is absent from
   `agent_server.tools.registry.TOOLS`. Scripts must never reach the model.

## Definition of done

```
.venv/bin/python -m pytest tests/ -q     # all green, including route_inventory
.venv/bin/python -m ruff check .         # All checks passed!
```

Then run the server and click through it: create a script that echoes something,
run it, see the output; create one that exits 1, run it, see the failure; delete
one.

```
mkdir -p /tmp/scripts-check
CODEAGENT_DATA_DIR=/tmp/scripts-check .venv/bin/python -m uvicorn agent_server.main:app --port 8298 &
sleep 4
curl -s -o /dev/null -w "/scripts -> %{http_code}\n" http://127.0.0.1:8298/scripts
```

Kill the server afterwards.

Report: the files you added, how you reused the custom-tool runner, and anything
you had to change outside the files listed above.
