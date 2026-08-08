"""Permission policy: what the agent may do without asking.

Two independent gates:

* **Shell** — non-read-only commands prompt. This can be auto-approved per
  session, because the blast radius is something the user opted into for a
  session they are watching.
* **Filesystem writes outside the project directory** — always prompt, and
  deliberately *not* covered by shell auto-approval. Agreeing to let an agent run
  `npm test` in your project is not the same as agreeing to let it rewrite
  `~/.ssh/config`. Grants here are explicit, per-directory, and persistent.
"""

import subprocess
from pathlib import Path

from agent_server import database as db

# Tools whose target path is checked against the write allowlist.
WRITE_TOOLS = {"edit", "write"}

# Never writable, allowlist or not. These break the machine or leak credentials.
DENIED_PREFIXES = (
    "/proc", "/sys", "/dev", "/boot", "/etc/shadow", "/etc/sudoers",
)

_cache: set[str] | None = None


async def _allowed() -> set[str]:
    global _cache
    if _cache is None:
        raw = await db.get_setting("allowed_write_dirs", "")
        _cache = {line.strip() for line in raw.splitlines() if line.strip()}
    return set(_cache)


async def list_allowed() -> list[str]:
    return sorted(await _allowed())


async def allow_directory(path: str) -> list[str]:
    current = await _allowed()
    current.add(str(Path(path).expanduser().resolve()))
    await _save(current)
    return sorted(current)


async def revoke_directory(path: str) -> list[str]:
    current = await _allowed()
    current.discard(path)
    current.discard(str(Path(path).expanduser().resolve()))
    await _save(current)
    return sorted(current)


async def _save(paths: set[str]):
    global _cache
    _cache = paths
    await db.set_setting("allowed_write_dirs", "\n".join(sorted(paths)))


def _is_within(path: Path, parent: str) -> bool:
    try:
        path.resolve().relative_to(Path(parent).expanduser().resolve())
        return True
    except (ValueError, OSError):
        return False


def is_denied(path: Path) -> bool:
    text = str(path.resolve() if path.is_absolute() else path)
    return text.startswith(DENIED_PREFIXES)


async def write_allowed(path: Path, project_dir: str) -> bool:
    """True when this path may be written without asking."""
    if is_denied(path):
        return False
    if _is_within(path, project_dir):
        return True
    return any(_is_within(path, allowed) for allowed in await _allowed())


def grant_scope(path: Path) -> str:
    """The directory a 'always allow' grant should cover.

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
    except Exception:  # noqa: BLE001
        pass
    return str(directory.resolve())


async def check(name: str, args: dict, project_dir: str, shell_auto_approve: bool) -> dict | None:
    """Return a prompt descriptor, or None when the call may proceed.

    Kept async and centralised so the agent loop and the page-reload restore path
    cannot drift apart on what counts as permitted.
    """
    from agent_server.tools.bash import is_read_only

    if name in WRITE_TOOLS:
        raw = args.get("filePath") or ""
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(project_dir) / path
        if await write_allowed(path, project_dir):
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
