# TODO

## UI / UX

### Collapsible tool responses
Every tool result and thinking block should be collapsible. Three categories of tools:

**Always expanded by default** (code changes the user wants to see):
- `edit`, `write` — diff output always visible

**Collapsed by default** (informational, clutter in long convos):
- `read`, `grep`, `glob`, `webfetch`, `websearch`, `skill`, `vision`, `screenshot`
- `bash` (when exit 0, brief output), `task`, `explore`, `browser-*`

**Settings:**
- [ ] Per-tool-type preference: expanded / collapsed / follow most-recent
- [ ] Session-level "Collapse all" button in the chat window toolbar
- [ ] "Expand all" button (or toggle)

**Most-recent rule:**
- [ ] The most recent tool call / thinking block is ALWAYS expanded (live streaming)
- [ ] Checkbox (checked by default): "Expand last tool call / thinking block"
- [ ] While streaming: the block is expanded so user sees live output as it arrives
- [ ] When an AI text message follows the tool call, the tool block collapses
  - Unless the tool type is set to "always expanded" in preferences
- [ ] Thinking blocks (reasoning_content) — same collapsible behavior, collapsed by default, latest one expanded during streaming

**Behavior:**
- Click header bar (tool name + duration) to toggle expand/collapse
- Collapsed view shows: tool name, duration, status icon, one-line summary
- Expanded view shows: full output, diff (if applicable)
- State persists per-message in the DOM (no server roundtrip)
- On page reload, all collapsed except most-recent (or per preferences)

---

## Small / polish

- [ ] Tool test textarea auto-fill from schema defaults
- [ ] Playwright `browser-steps` — sequence of up to 8 actions, screenshot each step
- [ ] Sound upload (custom .mp3/.wav files)

## Medium

- [ ] `/init` command — auto-analyze project, generate AGENTS.md with project-specific rules
- [ ] Prompt injection hardening — declare XML/system tags as authoritative regardless of message role
- [ ] Model-specific prompt tweaks (thinking-mode nudge for DeepSeek, etc.)

## Heavy (deferred)

- [ ] Inter-session messaging tool (AIs talk across sessions)
- [ ] Working directory rename detection (inotify)
- [ ] Time-traveling stream rules — regex-match mid-stream → abort → inject reminder → retry
- [ ] LSP integration — go-to-definition, references, hover

## Rejected / skipped

- Plan/Build toggle — system prompt handles this, mode switch is unnecessary complexity
- Question tool — bloat, model should just act
- Hidden background agents — overengineering for a personal agent
- Workspace tree in system prompt — breaks prefix cache, model has `glob`/`read` for dirs
- Per-prompt tool checkboxes — removed; system prompt text drives tool usage
- Subagent tool checkboxes — removed; subagents always use fixed SUBAGENT_TOOLS

---

## Already done

- [x] Hashline edit format (4-char line hashes, hashStart/hashEnd params)
- [x] System prompt rewrite (omp-inspired: execution pipeline, anti-lazy rules, delivery contract)
- [x] Doom loop detection (3 identical tool calls → abort)
- [x] Pattern-based bash permissions (glob rules in preferences)
- [x] Tool validation (param/script reference warnings on /tools page)
- [x] Skill system (`skill` tool, `~/.config/codeagent/skills/*.md`)
- [x] Explore subagent (narrower codebase searcher)
- [x] Web search (DuckDuckGo Lite, no API key)
- [x] Multi-subagent launch (`count` param on task tool)
- [x] Deduplicate parallel identical tool calls (shared cache)
- [x] Secrets/env var manager for custom tools
- [x] Tool definition JSON preview on /tools page
- [x] Tool test button with textarea args
- [x] Sound customization (Click/Chime/Knock, volume, preview)
- [x] Playwright browser tools (goto, click, fill, screenshot)
- [x] "+" tab clones current session settings
- [x] Multi-provider support (DeepSeek, OpenRouter, Anthropic, custom endpoints)
- [x] Subagent configuration (custom prompt, model per prompt page)
- [x] Per-prompt tool save bug (fixed — dropdown now navigates on change)
- [x] Tool save handlers (new_prompt, new_custom_tool, new_custom_endpoint)
- [x] Anthropic system prompt duplication bug
- [x] purge_orphans commit bug, connect() race, browser leaks, orphaned CSS
