"""Shell execution."""

import asyncio
import os
import re
import shlex

from agent_server.config import MAX_TOOL_RESULT_CHARS
from agent_server.tools.base import ToolContext, ToolResult, truncate

# Commands that only observe state. Used to keep the approval prompt from firing
# on every `ls`, and surfaced in the UI as "read-only".
READ_ONLY_PREFIXES = {
    "ls", "cat", "head", "tail", "pwd", "whoami", "date", "echo", "which", "type",
    "file", "stat", "wc", "du", "df", "tree", "find", "grep", "rg", "fd", "env",
    "printenv", "uname", "hostname", "id", "ps", "top", "uptime", "history",
}
GIT_READ_ONLY = {"status", "log", "diff", "show", "branch", "remote", "blame", "describe", "rev-parse"}

# Paths whose recursive deletion destroys the machine rather than the project.
# A `rm -rf build/` is fine; `rm -rf /` is not.
PROTECTED_RM_TARGETS = {
    "/", "/*", "/.", "/..", "~", "~/", "$HOME", "${HOME}", "$HOME/",
    "/home", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/boot", "/var", "/opt", "/root", "/srv", "/mnt", "/proc", "/sys", "/dev",
    # The home directory by its real name too. `$HOME` and `~` are the spellings
    # a model reaches for, but the literal path is the same catastrophe.
    os.path.expanduser("~"),
}
_BLOCK_DEV_RE = re.compile(r"/dev/(sd[a-z]+|hd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|mmcblk\d+|disk|mapper)")


def _has_flag(tokens: list[str], flag: str, long: str = "") -> bool:
    """True when a short flag (possibly in a `-rf` cluster) or its `--long`
    spelling is present. Long options never match short flags: `--force` is not
    `-f`, so `rm --force /` must not be mistaken for `rm -rf`. """
    for t in tokens:
        if long and t == long:
            return True
        if not t.startswith("-") or t.startswith("--"):
            continue
        if flag in t[1:].lower():
            return True
    return False


def _tokenize(command: str) -> list[str]:
    """Split a command the way the shell will, so quoting cannot hide a target.

    Splitting on whitespace left `rm -rf "/"` as the token `"/"`, which matched
    nothing in the protected set while the shell cheerfully read it as `/`. The
    same held for every entry: `"$HOME"`, `'/etc'`, even `"rm"` itself, which
    hid the command as well as its argument. Quoting is not an exotic thing for
    a model to do -- it is what you get from asking for a path with a space in
    it once.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes: shlex refuses, so fall back and strip by hand
        # rather than let a malformed command past unexamined.
        tokens = command.split()
    return [t.strip("\"'") for t in tokens]


def danger_reason(command: str) -> str | None:
    """Why `command` must not run, or None when it is allowed.

    A guard against the commands that take the machine down with them, not just
    the project. Deliberately conservative: it only fires on the obvious
    catastrophes and never on an ordinary `rm -rf build/` or `git clean`.

    This is the last line rather than the first: an ordinary session asks before
    running anything that mutates. It is the only line when shell auto-approve
    is on, which is exactly when nobody is watching.
    """
    s = command.strip()

    # Classic fork bomb.
    if re.search(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", s):
        return "fork bomb"

    # rm with recursive+force flags targeting a protected path. Match by token
    # basename so a path-qualified `/bin/rm` is caught as well as a bare `rm`.
    tokens = _tokenize(s)
    if (
        any(os.path.basename(t) == "rm" for t in tokens)
        and _has_flag(tokens, "r", "--recursive")
        and _has_flag(tokens, "f", "--force")
    ):
        for tok in tokens:
            if tok.startswith("-"):
                continue
            target = tok.rstrip("/") or "/"
            if target in PROTECTED_RM_TARGETS:
                return f"rm -rf of {tok}"

    # Writing directly to a raw block device.
    if _BLOCK_DEV_RE.search(s) and re.search(r"\b(dd|mkfs\S*|fdisk|parted|sfdisk)\b", s):
        return "raw disk write"
    if re.search(r"[>]\s*" + _BLOCK_DEV_RE.pattern, s):
        return "raw disk write"

    return None


def is_read_only(command: str) -> bool:
    """Conservative check: every segment of the pipeline must be observational."""
    stripped = command.strip()
    if not stripped:
        return True
    # Anything that can redirect into a file, chain unknown commands, or spawn a
    # subshell is unsafe. `>` also covers `>>`, `2>`, `>&`, `<>`; `<` covers
    # `<<`, `<<<`, and `<(cmd)` process substitution.
    if any(tok in stripped for tok in (">", "&&", "||", ";", "`", "$(", "sudo", "<", "\n", "\r")):
        return False
    # A bare `&` backgrounds or chains (`cmd1 & cmd2`); `&&` was caught above.
    if re.search(r"(?<!&)&(?!&)", stripped):
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
            if not _git_read_only(parts):
                return False
            continue
        if cmd == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for a in parts[1:]):
            return False
        if cmd not in READ_ONLY_PREFIXES:
            return False
    return True


def _git_read_only(parts: list[str]) -> bool:
    """`git <sub>` is observational only when the subcommand and its flags are.

    `git branch` lists, but `git branch -D` deletes; `git remote` lists, but
    `git remote add` mutates config. The bare subcommand whitelist alone was
    therefore wrong.
    """
    if len(parts) < 2 or parts[1] not in GIT_READ_ONLY:
        return False
    flags = parts[2:]
    sub = parts[1]
    destructive = (
        (sub == "branch" and any(a in ("-d", "-D", "--delete", "-m", "-M") for a in flags))
        or (sub == "remote" and any(a in ("add", "remove", "rm", "set-url", "set-head") for a in flags))
    )
    return not destructive


async def run_bash(
    ctx: ToolContext,
    *,
    command: str,
    timeout: int | None = None,
    workdir: str | None = None,
    env: dict[str, str] | None = None,
    sudo_password: str | None = None,
    **_,
) -> ToolResult:
    if not command or not command.strip():
        return ToolResult.error("empty command", "bash")

    reason = danger_reason(command)
    if reason:
        return ToolResult.error(
            f"refusing to run destructive command ({reason}). "
            "The guard only blocks machine-destroying commands; be explicit if "
            "you meant a scoped deletion.",
            "bash",
        )

    has_sudo = "sudo" in command.split()
    if has_sudo:
        command = re.sub(r"\bsudo\b", "sudo -S", command, count=1)
        if sudo_password:
            command = re.sub(r"-n\b\s*", "", command, count=1)

    timeout_ms = timeout  # the model's own, if it asked for one; None means no limit
    cwd = str(ctx.resolve(workdir)) if workdir else ctx.project_dir
    if not os.path.isdir(cwd):
        cwd = ctx.project_dir
    title = command.strip().splitlines()[0][:90]

    proc = None
    detached = False
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if (sudo_password or has_sudo) else asyncio.subprocess.DEVNULL,
            cwd=cwd,
            start_new_session=True,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1", "PAGER": "cat", **(env or {})},
        )
        if sudo_password and proc.stdin is not None:
            proc.stdin.write((sudo_password + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()
        elif has_sudo and not sudo_password and proc.stdin is not None:
            proc.stdin.close()
        # Only stream when someone is watching. A subagent's bash has no
        # transcript of its own, so it pays nothing for this.
        emit = ctx.emit if ctx.progress is not None else None
        if timeout_ms:
            stdout, stderr, detached, truncated = await asyncio.wait_for(
                _collect(proc, emit), timeout=timeout_ms / 1000
            )
        else:
            stdout, stderr, detached, truncated = await _collect(proc, emit)
    except TimeoutError:
        _kill(proc)
        return ToolResult.error(f"command timed out after {timeout_ms / 1000:g}s: {command}", title)
    except asyncio.CancelledError:
        _kill(proc)
        raise
    except Exception as e:
        _kill(proc)
        return ToolResult.error(f"failed to execute: {e}", title)

    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    code = proc.returncode

    parts = []
    # First, so truncate() (which keeps the head) cannot cut this note off.
    if truncated:
        parts.append("[output truncated: exceeded the capture limit]")
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
        output=truncate(body, MAX_TOOL_RESULT_CHARS, spill=True, session_id=ctx.session_id),
        is_error=code != 0,
        title=f"{title} (exit {code})",
    )


# How long to keep reading after the shell itself has exited. Only matters when
# a background grandchild inherited the pipe; a normal command's pipes are
# already at EOF by then, so this costs nothing in the common case.
BACKGROUND_DRAIN_SEC = 0.25

# Upper bound on how much stdout+stderr is buffered in memory. A command that
# floods its pipes must not grow the sink without limit; beyond this the bytes
# are still read (so the process is not blocked on a full pipe) but discarded.
MAX_CAPTURE_BYTES = 5_000_000

# How often the running command's output is offered to the transcript. Ten
# frames a second reads as live, and pinning the rate to a clock rather than to
# chunk arrivals is what keeps this cheap: `yes | head -c 100M` and a quiet
# build cost the same, because both send at most ten frames a second.
STREAM_INTERVAL_SEC = 0.1

# How much of the tail each frame carries. The transcript shows a scrolling
# window rather than the whole log, and the model still receives the complete
# output when the call ends, so there is nothing to gain from sending more.
STREAM_TAIL_BYTES = 4000


class _Tail:
    """The last `limit` bytes of a stream, at a cost independent of its length.

    Keeping the tail by re-joining the capture sink would make every frame cost
    O(total output), so a long-running command would get quadratically more
    expensive the longer it ran -- exactly backwards. This holds a short list of
    recent chunks and collapses it when it grows past twice the limit, so both
    appending and reading are bounded by `limit` no matter how much has gone
    through.
    """

    __slots__ = ("_chunks", "_limit", "_size", "dirty")

    def __init__(self, limit: int = STREAM_TAIL_BYTES):
        self._chunks: list[bytes] = []
        self._size = 0
        self._limit = limit
        self.dirty = False

    def add(self, chunk: bytes) -> None:
        self._chunks.append(chunk)
        self._size += len(chunk)
        self.dirty = True
        if self._size > self._limit * 2:
            joined = b"".join(self._chunks)[-self._limit:]
            self._chunks = [joined]
            self._size = len(joined)

    def text(self) -> str:
        self.dirty = False
        raw = b"".join(self._chunks)[-self._limit:]
        return raw.decode("utf-8", errors="replace")


async def _collect(proc, emit=None) -> tuple[bytes, bytes, bool, bool]:
    """Read stdout/stderr, but stop waiting once the shell itself has exited.

    `communicate()` waits for the pipes to reach EOF, not for the process to
    exit. `python3 -m http.server &` exits the shell immediately while the
    server inherits the pipe and holds it open for as long as it runs, so
    communicate() blocks for the full timeout and the process group then gets
    killed -- taking the server with it. Waiting on the shell instead means a
    backgrounded command returns immediately, as the user expects.

    Returns ``(stdout, stderr, detached, truncated)``.
    """
    out: list[bytes] = []
    err: list[bytes] = []
    state = {"out": 0, "err": 0, "truncated": False}
    # One tail across both streams, appended in arrival order, which is what a
    # terminal shows. Kept even past MAX_CAPTURE_BYTES: once the capture stops
    # growing the model gets a truncated result, but the user watching it run
    # should still see the command's last words.
    tail = _Tail() if emit else None

    async def drain(stream, sink, key):
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            if tail is not None:
                tail.add(chunk)
            used = state[key]
            remaining = MAX_CAPTURE_BYTES - used
            if remaining <= 0:
                state["truncated"] = True
                continue
            if len(chunk) > remaining:
                sink.append(chunk[:remaining])
                state[key] = MAX_CAPTURE_BYTES
                state["truncated"] = True
            else:
                sink.append(chunk)
                state[key] = used + len(chunk)

    async def stream_tail():
        """Offer the tail on a clock, and only when it has changed."""
        while True:
            await asyncio.sleep(STREAM_INTERVAL_SEC)
            if tail.dirty:
                emit(tail.text())

    readers = [
        asyncio.create_task(drain(proc.stdout, out, "out")),
        asyncio.create_task(drain(proc.stderr, err, "err")),
    ]
    ticker = asyncio.create_task(stream_tail()) if tail is not None else None
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
        if ticker is not None:
            ticker.cancel()
            # One last frame, so the transcript ends on what the command
            # actually printed rather than on whatever the last tick caught.
            if tail is not None and tail.dirty:
                emit(tail.text())
        tasks = [*readers, ticker] if ticker is not None else readers
        await asyncio.gather(*tasks, return_exceptions=True)

    return b"".join(out), b"".join(err), detached, state["truncated"]


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
