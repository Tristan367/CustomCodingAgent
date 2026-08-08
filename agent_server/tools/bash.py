"""Shell execution."""

import asyncio
import os
import shlex

from agent_server.config import MAX_TOOL_RESULT_CHARS
from agent_server.tools.base import ToolContext, ToolResult, truncate

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000

# Commands that only observe state. Used to keep the approval prompt from firing
# on every `ls`, and surfaced in the UI as "read-only".
READ_ONLY_PREFIXES = {
    "ls", "cat", "head", "tail", "pwd", "whoami", "date", "echo", "which", "type",
    "file", "stat", "wc", "du", "df", "tree", "find", "grep", "rg", "fd", "env",
    "printenv", "uname", "hostname", "id", "ps", "top", "uptime", "history",
}
GIT_READ_ONLY = {"status", "log", "diff", "show", "branch", "remote", "blame", "describe", "rev-parse"}


def is_read_only(command: str) -> bool:
    """Conservative check: every segment of the pipeline must be observational."""
    stripped = command.strip()
    if not stripped:
        return True
    # Anything that can redirect into a file or chain unknown commands is unsafe.
    if any(tok in stripped for tok in (">", ">>", "&&", "||", ";", "`", "$(", "sudo")):
        return False
    for segment in stripped.split("|"):
        try:
            parts = shlex.split(segment)
        except ValueError:
            return False
        if not parts:
            return False
        cmd = os.path.basename(parts[0])
        if cmd == "git":
            if len(parts) < 2 or parts[1] not in GIT_READ_ONLY:
                return False
            continue
        if cmd not in READ_ONLY_PREFIXES:
            return False
    return True


async def run_bash(
    ctx: ToolContext,
    *,
    command: str,
    timeout: int | None = None,
    workdir: str | None = None,
    **_,
) -> ToolResult:
    if not command or not command.strip():
        return ToolResult.error("empty command", "bash")

    timeout_ms = min(timeout or DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS)
    timeout_sec = timeout_ms / 1000
    cwd = str(ctx.resolve(workdir)) if workdir else ctx.project_dir
    if not os.path.isdir(cwd):
        cwd = ctx.project_dir
    title = f"bash: {command.strip().splitlines()[0][:90]}"

    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            # New process group so a timeout can kill the whole pipeline, not
            # just the shell that spawned it.
            start_new_session=True,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1", "PAGER": "cat"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        _kill(proc)
        return ToolResult.error(f"command timed out after {timeout_sec:g}s: {command}", title)
    except asyncio.CancelledError:
        _kill(proc)
        raise
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(f"failed to execute: {e}", title)

    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    code = proc.returncode

    parts = []
    if out:
        parts.append(out)
    if err:
        parts.append(f"[stderr]\n{err}")
    if code != 0:
        parts.append(f"[exit code {code}]")
    body = "\n".join(parts) or "(no output)"

    return ToolResult(
        output=truncate(body, MAX_TOOL_RESULT_CHARS),
        is_error=code != 0,
        title=f"{title} (exit {code})",
    )


def _kill(proc):
    if proc is None or proc.returncode is not None:
        return
    import signal

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
