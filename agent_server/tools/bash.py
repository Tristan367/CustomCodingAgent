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
    env: dict[str, str] | None = None,
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
    detached = False
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
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1", "PAGER": "cat", **(env or {})},
        )
        stdout, stderr, detached = await asyncio.wait_for(
            _collect(proc), timeout=timeout_sec
        )
    except TimeoutError:
        _kill(proc)
        return ToolResult.error(f"command timed out after {timeout_sec:g}s: {command}", title)
    except asyncio.CancelledError:
        _kill(proc)
        raise
    except Exception as e:
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
    if detached:
        # The shell exited but something it spawned still holds the output pipe,
        # i.e. a real background process. Say so, otherwise the model sees a
        # suspiciously empty result and retries a server it already started.
        parts.append(
            "[note] the shell exited and left a background process running; "
            "it was not killed and any later output is not captured"
        )
    body = "\n".join(parts) or "(no output)"

    return ToolResult(
        output=truncate(body, MAX_TOOL_RESULT_CHARS, spill=True),
        is_error=code != 0,
        title=f"{title} (exit {code})",
    )


# How long to keep reading after the shell itself has exited. Only matters when
# a background grandchild inherited the pipe; a normal command's pipes are
# already at EOF by then, so this costs nothing in the common case.
BACKGROUND_DRAIN_SEC = 0.25


async def _collect(proc) -> tuple[bytes, bytes, bool]:
    """Read stdout/stderr, but stop waiting once the shell itself has exited.

    `communicate()` waits for the pipes to reach EOF, not for the process to
    exit. `python3 -m http.server &` exits the shell immediately while the
    server inherits the pipe and holds it open for as long as it runs, so
    communicate() blocks for the full timeout and the process group then gets
    killed -- taking the server with it. Waiting on the shell instead means a
    backgrounded command returns immediately, as the user expects.
    """
    out: list[bytes] = []
    err: list[bytes] = []

    async def drain(stream, sink):
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            sink.append(chunk)

    readers = [
        asyncio.create_task(drain(proc.stdout, out)),
        asyncio.create_task(drain(proc.stderr, err)),
    ]
    try:
        # NB: neither communicate() nor wait() can be used here. Both only
        # resolve once every pipe has disconnected (see _try_finish in
        # asyncio/base_subprocess.py), which is precisely what a background
        # grandchild prevents. `returncode` is set as soon as the child exits,
        # independently of the pipes, so poll that instead.
        while proc.returncode is None:
            await asyncio.sleep(0.02)
        # Give whatever is already buffered a moment to arrive.
        _, pending = await asyncio.wait(readers, timeout=BACKGROUND_DRAIN_SEC)
        detached = bool(pending)
    finally:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)

    return b"".join(out), b"".join(err), detached


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
