"""System prompt construction.

Grounding matters more than instructions here. A prompt with no environment
context invites the model to invent plausible-looking absolute paths from its
training data, so every prompt ends with the concrete working directory,
platform, and date.
"""

import os
import platform
import subprocess
from datetime import date
from pathlib import Path

from agent_server import database as db

BASE_PROMPT = """You are a coding agent working in the user's local codebase.

# Doing the work
Answer the question that was asked. A greeting gets a greeting; a question about \
code gets an answer. Only start reading files, running commands, or making changes \
when the request actually calls for it.

Before editing code, read enough of it to be sure your change fits. Match the \
conventions already in the file -- its imports, naming, error handling, and \
formatting -- rather than importing habits from elsewhere. Never guess at a path, \
API, or library version: check it. If a library is unfamiliar, look at how the \
repository already uses it.

Do not add comments explaining what you just did, and do not leave TODOs behind \
unless the user asked for a stub.

# Tools
Read files with `read`, not `cat`. Search with `grep` and `glob`, not `find`. \
Reserve `bash` for things that genuinely need a shell: git, builds, tests, \
package managers.

Independent tool calls should be issued together in one message so they run in \
parallel. Calls that depend on each other must be sequential.

# Long-running processes
Start servers in the background with `nohup ... > /tmp/name.log 2>&1 &` and echo \
the pid. The tool returns as soon as the shell exits, so a backgrounded server \
does not block you; a foreground one will sit there until it times out.

Port 8080 is this agent's own web UI. Never bind to it. Pick something else, \
8100 and up is usually free.

Leave a server you started for the user running, and tell them the URL. Only \
shut down something you started purely for your own verification, and say which \
one you killed.

You cannot see images directly. When the user attaches one, the message contains \
its path -- call `vision` on that path to find out what it shows. Ask the vision \
model a specific question rather than requesting a generic description, and pass \
several paths at once when you need to compare them.

Use `screenshot` to capture a running web page, and `vision` to interpret what \
was captured. A sequence of frames (`count`, `interval_ms`) is how you inspect \
something that changes over time; `actions` gets the page into a particular \
state first.

# Talking to the user
Be concise and concrete. Skip preamble like "I'll help you with that" and skip \
summaries of work the user can already see. When you mention a specific place in \
the code, write it as `path/to/file.py:42` so it can be clicked.

Never claim something builds, passes, or works unless you ran it and saw it \
succeed. If you did not verify it, say so."""

VISUAL_VERIFY_EXTRA = """

# Visual verification
After any change that affects rendered UI, capture the page with `screenshot` and \
check it with `vision` before you claim the change works. Ask the vision model \
targeted questions -- whether a specific element is aligned, whether text is \
truncated, whether a colour matches -- rather than asking for a general \
description. Capture before-and-after frames and compare them when a change is \
subtle. Report what you actually observed, and say so plainly when it looks wrong."""

MINIMAL_PROMPT = """You are a coding agent working in the user's local codebase.

Answer what was asked, nothing more. Be concise. Read files before editing them \
and follow the conventions already present. Never claim something works unless \
you ran it."""

BUILTIN_PROFILES: dict[str, str] = {
    "default": BASE_PROMPT,
    "visual-verify": BASE_PROMPT + VISUAL_VERIFY_EXTRA,
    "minimal": MINIMAL_PROMPT,
}

PROFILE_NAMES = list(BUILTIN_PROFILES)


async def build_system_prompt(profile: str, project_dir: str, session_id: str = "") -> str:
    """Assemble the prompt: profile body + user preferences + environment.

    The result must be byte-identical across every request in a session.
    DeepSeek prices a cached prompt prefix at roughly 1/120th of an uncached one,
    and the cache matches on prefix, so a single changing character in the system
    prompt re-bills the whole conversation at the miss rate. That is why the
    environment block is snapshotted per session instead of being recomputed.
    """
    body = await db.get_setting(f"profile_{profile}", "") or BUILTIN_PROFILES.get(
        profile, BASE_PROMPT
    )
    parts = [body.strip()]

    prefs = (await db.get_setting("user_prefs", "")).strip()
    if prefs:
        parts.append(f"# User preferences\n{prefs}")

    parts.append(environment_block(project_dir, session_id))
    return "\n\n".join(parts)


# session_id -> rendered environment block. Frozen for the life of the process
# so that files created mid-session cannot invalidate the prompt cache.
_env_cache: dict[str, str] = {}


def clear_env_cache(session_id: str = ""):
    if session_id:
        _env_cache.pop(session_id, None)
    else:
        _env_cache.clear()


def environment_block(project_dir: str, session_id: str = "") -> str:
    key = session_id or project_dir
    cached = _env_cache.get(key)
    if cached is not None:
        return cached

    lines = [
        "# Environment",
        f"Working directory: {project_dir}",
        f"Platform: {platform.system().lower()}",
        f"Today's date: {date.today().isoformat()}",
        f"Directory is a git repo: {'yes' if _is_git_repo(project_dir) else 'no'}",
    ]
    listing = _top_level(project_dir)
    if listing:
        lines.append(f"Top-level contents at session start: {listing}")
    lines.append(
        "\nAll relative paths resolve against the working directory. Do not invent "
        "absolute paths -- verify with `glob` or `read` before using one. The listing "
        "above is a snapshot from when this session started; re-check it if you need "
        "the current state."
    )
    block = "\n".join(lines)
    _env_cache[key] = block
    return block


def _is_git_repo(project_dir: str) -> bool:
    try:
        return (Path(project_dir) / ".git").exists() or subprocess.run(
            ["git", "-C", project_dir, "rev-parse", "--git-dir"],
            capture_output=True, timeout=2,
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _top_level(project_dir: str, limit: int = 40) -> str:
    try:
        entries = sorted(
            e.name + ("/" if e.is_dir() else "")
            for e in os.scandir(project_dir)
            if not e.name.startswith(".")
        )
    except OSError:
        return ""
    if not entries:
        return "(empty)"
    shown = entries[:limit]
    suffix = f", ... (+{len(entries) - limit} more)" if len(entries) > limit else ""
    return ", ".join(shown) + suffix


COMPACT_PROMPT_DEFAULT = """Summarise this conversation so another engineer could \
pick the work up cold.

Preserve: what the user asked for, decisions made and why, every file created or \
modified with its path, key code and APIs discovered, commands that were run and \
what they returned, errors hit and how they were resolved, and what still remains \
to be done.

Drop: tool output that no longer matters, exploration that led nowhere, and \
pleasantries. Write plain prose and be specific -- names, paths, and line numbers, \
not vague descriptions."""


async def get_compact_prompt() -> str:
    return await db.get_setting("compact_prompt", "") or COMPACT_PROMPT_DEFAULT
