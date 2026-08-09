"""System prompt construction.

Prompts are named, stored in the database, and editable. `default` always
exists; everything else is the user's to create and delete. A session records
which prompt it started from and freezes the rendered text, so editing a prompt
never disturbs a conversation already in flight.

Grounding matters more than instruction here. A prompt with no environment
context invites the model to invent plausible-looking absolute paths from its
training data, so every prompt ends with the concrete working directory and
platform.
"""

import hashlib
import os
import platform
import subprocess
from pathlib import Path

from agent_server import database as db

DEFAULT_PROMPT = """You are a coding agent working in the user's local codebase.

# Working
Answer what was asked. A greeting gets a greeting. Only read files, run commands, or \
change code when the request calls for it.

Never guess at a path, an API, or a library's version -- check it. If a library is \
unfamiliar, look at how this repository already uses it before reaching for what you \
remember. Match the conventions of the file you are editing over your own habits.

Fix causes, not symptoms. When something fails, find out why before adding a \
workaround, a retry, or a special case. If you cannot find out why, say so rather \
than papering over it.

Keep the change to what was asked. Raise a bigger idea; do not build it unasked. Do \
not leave commented-out code, TODOs, or comments narrating what you just changed.

Back up anything you are about to destroy. Before a migration, a bulk delete, or a \
rewrite of something you cannot regenerate, copy it somewhere first and say where.

# Verifying
Never say something builds, passes, or works unless you ran it and read the output. \
If you did not verify it, say which part you did not.

A test only counts if it can fail. When one passes first try, check that it actually \
reproduces the thing you are fixing and that the conditions the bug needs are \
present -- a check that never runs looks exactly like a check that succeeded.

# Tools
You cannot see images. When a path to one appears, call `vision` on it and ask \
something specific rather than for a description. `screenshot` captures a running \
page and describes it in one call.

Start a server in the background or the call blocks until it times out. Port 8080 is \
this app; pick another. The user cannot Ctrl-C anything you leave running, so shut it \
down when you are done or tell them the command.

# Talking to the user
Be concise and concrete. No preamble, no recap of what is already on screen. Write \
code locations as `path/to/file.py:42` so they can be clicked.

Say when you disagree, and say when you are unsure. Agreeing with a bad plan costs \
more than the disagreement would. If an instruction seems wrong, ask -- but do not \
quietly substitute your own judgement for it.

Some messages are dictated, so a word that makes no sense in context may be a \
homophone of the intended one -- "sea sharp" for "C#", "clip board" for \
"clipboard". Read through the sound rather than the spelling. If the meaning is \
genuinely unclear, ask instead of guessing."""

MINIMAL_PROMPT = """You are a coding agent working in the user's local codebase.

Answer what was asked, nothing more. Be concise.

Check paths and APIs instead of recalling them. Match the conventions of the file you \
are editing. Never say something works unless you ran it and read the output.

Issue independent tool calls in the same message. You cannot see images -- use \
`vision` on any image path. Background any server you start; port 8080 is this app."""

COMPACT_PROMPT_DEFAULT = """Summarise this conversation so another engineer could \
pick the work up cold.

Preserve: what the user asked for, decisions made and why, every file created or \
modified with its path, key code and APIs discovered, commands that were run and \
what they returned, errors hit and how they were resolved, and what still remains \
to be done.

Drop: tool output that no longer matters, exploration that led nowhere, and \
pleasantries. Write plain prose and be specific -- names, paths, and line numbers, \
not vague descriptions."""


# Seeded only into a database that has no prompts yet. An existing install keeps
# whatever is already stored, including prompts the user wrote.
STARTER_PROMPTS: dict[str, str] = {
    "default": DEFAULT_PROMPT,
    "minimal": MINIMAL_PROMPT,
}

# Deleting this one would leave sessions pointing at nothing, and there would be
# no prompt to fall back to.
PROTECTED_PROMPT = "default"


SYSTEM = "system"
COMPACTION = "compaction"


async def list_prompt_names(kind: str = SYSTEM) -> list[str]:
    return [row["name"] for row in await db.list_prompts(kind)]


async def migrate_prompts():
    """Move prompts out of the settings table and into their own, once.

    Prompts used to be three fixed slots plus a separate "user preferences" box
    appended to whichever slot was chosen. The preferences box had grown into a
    complete prompt in its own right, so it becomes one, and the slots become
    ordinary rows the user can add to and delete.

    A slot only held text if it had been edited away from what shipped, but
    "edited" also covered text that shipped in an *earlier* version and was
    written back untouched. Those are fingerprinted and dropped so the improved
    wording lands; anything unrecognised is genuinely the user's and is kept.
    """
    if await db.list_prompts():
        await _refresh_untouched_builtins()
        return

    settings = await db.get_all_settings()

    # The summarising prompt was a single setting shared by every session.
    # It becomes the default of its own kind, so sessions can differ.
    saved_compact = (settings.get("compact_prompt") or "").strip()
    await db.save_prompt(
        PROTECTED_PROMPT, saved_compact or COMPACT_PROMPT_DEFAULT.strip(), COMPACTION
    )
    await db.delete_setting("compact_prompt")

    for name, starter in STARTER_PROMPTS.items():
        stored = (settings.get(f"profile_{name}") or "").strip()
        keep = stored and not _is_shipped(stored)
        await db.save_prompt(name, stored if keep else starter.strip())

    # The old preferences text was appended to every profile, so on its own it
    # is the closest thing to the prompt the user was actually running.
    prefs = (settings.get("user_prefs") or "").strip()
    legacy = (settings.get("profile_visual-verify") or "").strip()
    if prefs:
        await db.save_prompt("visual-verify", prefs)
    elif legacy and not _is_shipped(legacy):
        await db.save_prompt("visual-verify", legacy)

    for key in ("user_prefs", "profile_default", "profile_minimal", "profile_visual-verify"):
        await db.delete_setting(key)


# Prompt bodies this app has shipped in the past. A stored prompt matching one
# of these was never written by the user, so replacing it loses nothing.
_SHIPPED_HASHES = {
    "3868d771daffb15aa2d68cd6a0236aefadf83e140259355214e6daeec8870d31",  # default
    "4faaa19aa524bb24e7891374949c59b346d0efcdad6aa182132189570b91a915",  # minimal
    "c6795a039b62f7ae7d9629c8c5b9f938804947258dbb82e03f223987a51025b7",  # visual-verify
    "1f85feeb0ab7af8d341d371cb62afcc36ee4e857fda815bd9e0ac4288fe2c294",  # default, before the parallel-calls line was dropped
}


async def _refresh_untouched_builtins():
    """Carry an improved built-in through to installs that never edited it.

    Seeding once meant a built-in froze at whatever shipped the day the database
    was created, so later wording never reached anyone but new installs. A body
    still matching something this app shipped was not written by the user and is
    safe to move forward; anything else is theirs and is left alone.
    """
    row = await db.get_prompt(PROTECTED_PROMPT, COMPACTION)
    if row is None:
        await db.save_prompt(PROTECTED_PROMPT, COMPACT_PROMPT_DEFAULT.strip(), COMPACTION)

    for name, starter in STARTER_PROMPTS.items():
        row = await db.get_prompt(name)
        if row is None:
            await db.save_prompt(name, starter.strip())
        elif _is_shipped(row["body"]) and row["body"].strip() != starter.strip():
            await db.save_prompt(name, starter.strip())


def _is_shipped(body: str) -> bool:
    digest = hashlib.sha256(body.strip().encode()).hexdigest()
    return digest in _SHIPPED_HASHES




async def prompt_body(name: str, kind: str = SYSTEM) -> str:
    """The text of a named prompt, falling back to `default` if it is gone."""
    row = await db.get_prompt(name, kind)
    if row is None:
        row = await db.get_prompt(PROTECTED_PROMPT, kind)
    if row:
        return row["body"]
    return COMPACT_PROMPT_DEFAULT if kind == COMPACTION else DEFAULT_PROMPT



async def session_system_prompt(session: dict) -> str:
    """The system prompt for a session, frozen the first time it is needed.

    Rendering it fresh each request meant anything that changed underneath --
    editing a shared prompt, a restart picking up files the agent had since
    created -- silently changed the prefix and re-billed the whole conversation
    at the miss rate.
    """
    stored = session.get("system_prompt")
    if stored:
        return stored
    prompt = await build_system_prompt(
        session.get("prompt_profile") or PROTECTED_PROMPT,
        session["project_dir"],
        session["id"],
    )
    await db.update_session(session["id"], system_prompt=prompt)
    return prompt


async def build_system_prompt(
    profile: str, project_dir: str, session_id: str = ""
) -> str:
    """Assemble the prompt: the named body, then the environment block.

    The result must be byte-identical across every request in a session.
    DeepSeek prices a cached prompt prefix at roughly 1/120th of an uncached one,
    and the cache matches on prefix, so a single changing character in the system
    prompt re-bills the whole conversation at the miss rate. That is why the
    environment block is snapshotted per session instead of being recomputed.
    """
    body = (await prompt_body(profile)).strip()
    return f"{body}\n\n{environment_block(project_dir, session_id)}"


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
        f"Directory is a git repo: {'yes' if _is_git_repo(project_dir) else 'no'}",
    ]
    listing = _top_level(project_dir)
    if listing:
        # Capped: a crowded directory turns this into a wall of filenames that
        # crowds out the rest of the prompt for no benefit.
        entries = listing.split(", ")
        if len(entries) > 30:
            listing = ", ".join(entries[:30]) + f", and {len(entries) - 30} more"
        lines.append(f"Top-level contents: {listing}")
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


async def get_compact_prompt(session: dict | None = None) -> str:
    """The summarising prompt for a session, or the default when none is given."""
    name = (session or {}).get("compact_profile") or PROTECTED_PROMPT
    return await prompt_body(name, COMPACTION)
