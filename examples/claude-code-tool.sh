#!/usr/bin/env bash
# Delegate a question to Claude Code, running as itself on your own plan.
#
# This is not a way to point MyriadCode's model picker at a Claude subscription
# -- that auth belongs to Anthropic's own clients and is not an API. What it is
# instead: the real `claude` CLI, invoked as a tool, so a cheap model driving
# your session can hand its hardest question to Opus 5 and get an answer back.
#
# Install it on the Tools page (Tools -> New Tool) with:
#
#   Name:        claude_code
#   Description: Ask Claude Code (Opus 5) a hard question about this codebase.
#                It reads files and searches on its own; it does not write.
#                Give it one self-contained question -- it sees nothing of this
#                conversation. Slow and expensive: use it for a question you
#                cannot answer yourself, not for a lookup.
#   Parameters:
#     {"type":"object",
#      "properties":{
#        "prompt":{"type":"string","description":"A complete, self-contained question"},
#        "directory":{"type":"string","description":"Where to run. Defaults to the session's project directory."}
#      },
#      "required":["prompt"]}
#
#   Ask permission: on, at least until you trust it. Every call spends your plan.
#
# Arguments arrive JSON-encoded in TOOL_ARG_<NAME>, so they are decoded rather
# than used raw -- a prompt with a quote or a newline in it is otherwise a
# broken command line.
set -uo pipefail

decode() {
  printf '%s' "${1:-}" | python3 -c '
import json, sys
raw = sys.stdin.read()
if not raw:
    sys.exit(0)
try:
    value = json.loads(raw)
except json.JSONDecodeError:
    value = raw
sys.stdout.write("" if value is None else str(value))
'
}

prompt="$(decode "${TOOL_ARG_PROMPT:-}")"
directory="$(decode "${TOOL_ARG_DIRECTORY:-}")"

if [ -z "$prompt" ]; then
  echo "claude_code: a prompt is required" >&2
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "claude_code: the Claude Code CLI is not on PATH." >&2
  echo "Install it, or remove this tool on the Tools page." >&2
  exit 1
fi

cd "${directory:-$PWD}" 2>/dev/null || {
  echo "claude_code: no such directory: $directory" >&2
  exit 1
}

# Read-only by default. Headless Claude Code cannot stop to ask, so anything not
# listed here is simply refused -- which is the right failure for a tool being
# driven by another model. Widen this list only as far as you actually want.
#
# Deliberately not `--bare`: that mode skips keychain reads and restricts auth to
# ANTHROPIC_API_KEY, which is the one thing this tool exists to avoid needing.
# Your subscription lives in the keychain, so `--bare` fails with "Not logged in".
#
# `< /dev/null` because the CLI waits 3s for piped stdin otherwise, and there is
# never any here.
exec claude -p "$prompt" \
  --allowedTools "Read Glob Grep WebFetch WebSearch" < /dev/null
