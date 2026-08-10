# Changes for Claude to review

Last updated: 2026-08-10

## System prompt rewrite (`system_prompt.py`)
Replaced DEFAULT_PROMPT with omp-inspired anti-lazy rules:
- "Rules of engagement" — never guess, match conventions, hope is not a strategy
- "Execution pipeline" — Scope → Research → Decompose → Implement → Verify → Cleanup
- "Delivery contract" — NEVER yield, NEVER fabricate, NEVER re-audit, NEVER punt
- "Tool discipline" — MUST use read/grep/glob over bash equivalents
- "Editing" section explaining hashline format (`N|hhhh|` prefixes)
- Prompt injection hardening — XML tags declared authoritative
- New tools mentioned: websearch, explore, skill, browser-steps

## Hashline edit format (`file_ops.py`)
- `read_file` outputs `N|hhhh| content` where hhhh is 4-char MD5 hex of line content
- `edit_file` accepts hashStart, hashEnd, newText (hashline mode)
- oldString/newString kept as fallback (backwards compatible)
- Hash mismatch rejects edits with "file changed since read" error
- Collision rate ~0.36% for real code (4 different-content collisions in 1098 lines)

## Doom loop detection (`agent.py`)
- `_doom_history` per-session deque of last 10 (name, args_json) pairs
- 3 consecutive identical calls → error and abort
- Checked before every tool execution in both `_drain_pending` and `_run_batch`

## Pattern-based bash permissions (`permissions.py`, `index_content.html`, `app.js`)
- `fnmatch` glob patterns: `"git push* → ask"`, `"npm test* → allow"`, `"rm -rf * → deny"`
- Stored as JSON in `bash_rules` setting
- Textarea UI in preferences, human-readable `PATTERN → action` format
- JS parse/save via `/_settings/bash_rules` route

## Tool validation (`main.py`, `custom_tools.html`)
- `_tool_param_warnings()` checks params ↔ script references
- Warnings shown as chips on /tools page after save
- `_default_test_args()` pre-fills test textarea from JSON Schema defaults

## New tools

### Skill system (`tools/skill.py`)
- `skill` tool reads `~/.config/codeagent/skills/NAME.md`
- List skills with no args, load specific one with `name` param
- Read-only, available to subagents

### Explore subagent (`registry.py`)
- Registered as separate tool from `task`
- Same handler (`run_task`) but narrower description for codebase search
- Uses same SUBAGENT_TOOLS as task

### Web search (`tools/web.py`)
- DuckDuckGo Lite HTML scraper (POST-based, no API key)
- Returns up to 10 results with title, snippet, URL
- `_parse_ddg_lite_v2()` extracts from modern DDG Lite HTML structure

### Browser-steps (`tools/browser.py`, `registry.py`)
- Sequence of up to 8 browser actions: goto, click, fill, wait
- Screenshot and vision-analyze after each step
- All actions in one `async with _LOCK`

## Sound upload (`main.py`, `app.js`, `index_content.html`)
- Upload .mp3/.wav/.ogg/.m4a to `~/.config/codeagent/sounds/`
- Routes: GET /list, POST /upload, DELETE /{name}, GET /{name}/play
- Uploaded sounds appear in sound dropdown alongside built-ins
- Preview plays via HTML Audio element (not Web Audio API)

## /init command (`main.py`, `index_content.html`, `app.js`)
- Button on home page: "Generate project rules"
- `_generate_rules()` scans for package.json, tsconfig, Cargo.toml, etc.
- Detects language, test framework, build system, VCS
- Writes AGENTS.md in project root

## Tool config simplification
Removed per-prompt tool checkboxes and subagent tool checkboxes:
- `prompts.html`: removed tools table and subagent tool checkboxes
- `main.py`: removed disabled_tools from save_prompts, removed sa_disabled from save_subagent, removed prompt_disabled/all_tools/sa_disabled from _prompts_context
- `agent.py`: removed disabled_tools loading from _loop
- `task.py`: removed subagent_disabled_tools loading from _run
- Dead code in _save_subagent cleaned up (unreachable after return)
- Fixed prompt dropdown onchange bug (was missing, now navigates to /prompts?selected=KEY)

## Bug fixes from earlier review pass
- `purge_orphans()` now actually commits (was silently dropping DELETEs)
- `connect()` double-connection race fixed with _connect_lock
- `browser._analyze()` fixed — was calling load_image with wrong signature
- Anthropic system prompt duplication removed
- `new_prompt` and `new_custom_tool` handlers fixed (were falling off end without saving)
- `pendingImages` cleared on session switch in app.js
- Playwright `_PW` handle saved and stopped in close_browser()
- `_ensure_browser` partial init failure cleaned up
- `_new_custom_endpoint` now reloads providers after save
- Orphaned CSS declarations removed, `.dim` and `.chip-ok` classes added

## Files modified this session
- agent_server/system_prompt.py
- agent_server/tools/file_ops.py
- agent_server/tools/browser.py
- agent_server/tools/skill.py (new)
- agent_server/tools/web.py
- agent_server/tools/registry.py
- agent_server/tools/task.py
- agent_server/agent.py
- agent_server/permissions.py
- agent_server/database.py
- agent_server/main.py
- web_ui/templates/index_content.html
- web_ui/templates/prompts.html
- web_ui/templates/custom_tools.html
- web_ui/static/js/app.js
- web_ui/static/css/style.css
