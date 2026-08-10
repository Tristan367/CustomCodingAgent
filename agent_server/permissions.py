"""Permission policy: what the agent may do without asking.

Everything here is scoped to a single session. Nothing granted in one session
leaks into another -- a session is the unit the user is actually watching, and a
grant made while supervising one task should not silently apply to the next.

Two independent gates:

* **Shell** — non-read-only commands prompt. Can be auto-approved for a session.
* **Filesystem writes outside the project directory** — always prompt, and
  deliberately *not* covered by shell auto-approval. Agreeing to let an agent run
  `npm test` in your project is not the same as agreeing to let it rewrite
  `~/.ssh/config`. Grants are per-directory and per-session.
"""

import logging
import subprocess
from pathlib import Path

from agent_server import database as db

log = logging.getLogger(__name__)

# Tools whose target path is checked against the write allowlist.
WRITE_TOOLS = {"edit", "write"}

# Never writable, in any session, however it was granted.
DENIED_PREFIXES = (
    "/proc", "/sys", "/dev", "/boot", "/etc/shadow", "/etc/sudoers",
)


async def list_allowed(session_id: str) -> list[str]:
    return await db.list_write_dirs(session_id)


async def allow_directory(session_id: str, path: str) -> list[str]:
    await db.add_write_dir(session_id, str(Path(path).expanduser().resolve()))
    return await list_allowed(session_id)


async def revoke_directory(session_id: str, path: str) -> list[str]:
    await db.remove_write_dir(session_id, path)
    await db.remove_write_dir(session_id, str(Path(path).expanduser().resolve()))
    return await list_allowed(session_id)


def _is_within(path: Path, parent: str) -> bool:
    try:
        path.resolve().relative_to(Path(parent).expanduser().resolve())
        return True
    except (ValueError, OSError):
        return False


def is_denied(path: Path) -> bool:
    try:
        text = str(path.resolve())
    except OSError:
        text = str(path)
    return text.startswith(DENIED_PREFIXES)


async def write_allowed(session_id: str, path: Path, project_dir: str) -> bool:
    """True when this path may be written without asking."""
    if is_denied(path):
        return False
    if _is_within(path, project_dir):
        return True
    return any(_is_within(path, allowed) for allowed in await list_allowed(session_id))


def grant_scope(path: Path) -> str:
    """The directory an 'always allow' grant should cover.

    Prefers the enclosing git repository, which is what a user means by "this
    project"; falls back to the containing directory.
    """
    directory = path if path.is_dir() else path.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        )
        root = result.stdout.strip()
        if result.returncode == 0 and root:
            return root
    except Exception:
        log.debug("git rev-parse failed for %s", directory, exc_info=True)
    try:
        return str(directory.resolve())
    except OSError:
        return str(directory)


async def check(
    name: str,
    args: dict,
    session_id: str,
    project_dir: str,
    shell_auto_approve: bool,
) -> dict | None:
    """Return a prompt descriptor, or None when the call may proceed.

    Centralised so the agent loop and the page-reload restore path cannot drift
    apart on what counts as permitted.
    """
    from agent_server.tools.bash import is_read_only

    if name in WRITE_TOOLS:
        raw = args.get("filePath") or ""
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(project_dir) / path
        if await write_allowed(session_id, path, project_dir):
            return None
        return {
            "kind": "denied" if is_denied(path) else "path",
            "tool": name,
            "path": str(path),
            "scope": grant_scope(path),
            "project_dir": project_dir,
        }

    if name == "bash":
        command = args.get("command", "")
        if is_read_only(command) or shell_auto_approve:
            return None
        return {
            "kind": "shell",
            "tool": name,
            "command": command,
            "workdir": args.get("workdir") or project_dir,
        }

    return None
