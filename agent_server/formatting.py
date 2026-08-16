"""Format file contents with an external formatter chosen by extension.

The editor's Format button shells out to real formatters instead of shipping a
per-language one. ``clang-format`` covers the C family plus Java, C# and
JavaScript/TypeScript; JSON is handled with the stdlib; Python uses ``black``
and CSS/HTML use ``prettier``. The last two are optional: when the binary is
missing the user gets an "install it" message rather than a crash.

Everything runs over stdin/stdout with an argument list (never a shell), so the
file contents can never be interpreted as a command.
"""

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path

_FORMAT_TIMEOUT_SEC = 15

_CLANG_EXTS = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh",
    ".cs", ".java",
}
_PY_EXTS = {".py", ".pyw"}
# JavaScript/TypeScript use prettier rather than clang-format: clang-format's
# JS style (``a : 1``) is not what the ecosystem writes.
_PRETTIER_EXTS = {
    ".css", ".scss", ".sass", ".less", ".html", ".htm", ".svg",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
}
_JSON_EXTS = {".json"}


class FormatError(Exception):
    """A user-facing formatting failure (bad input or missing formatter)."""


def formatter_for(path: str) -> str | None:
    suf = Path(path).suffix.lower()
    if suf in _CLANG_EXTS:
        return "clang-format"
    if suf in _JSON_EXTS:
        return "json"
    if suf in _PY_EXTS:
        return "python"
    if suf in _PRETTIER_EXTS:
        return "prettier"
    return None


def _fmt_json(text: str) -> str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise FormatError(f"Invalid JSON: {e.msg} (line {e.lineno})") from e
    return json.dumps(parsed, indent=2, ensure_ascii=False) + "\n"


async def _run(cmd: list[str], text: str, label: str, hint: str) -> str:
    exe = shutil.which(cmd[0])
    if not exe:
        raise FormatError(f"{label} is not installed — {hint}")
    cmd = [exe, *cmd[1:]]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise FormatError(f"Could not run {label}: {e}") from e
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(text.encode()), _FORMAT_TIMEOUT_SEC
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise FormatError(f"{label} timed out") from None
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise FormatError(f"{label} failed: {detail or f'exit code {proc.returncode}'}")
    return stdout.decode()


async def format_text(path: str, text: str) -> str:
    """Return ``text`` reformatted, or raise :class:`FormatError`."""
    name = Path(path).name
    formatter = formatter_for(path)
    if formatter == "json":
        return _fmt_json(text)
    if formatter == "clang-format":
        return await _run(
            ["clang-format", f"-assume-filename={name}"], text, "clang-format",
            "install clang-format",
        )
    if formatter == "python":
        # black lives in the same venv as this process, so run it through the
        # current interpreter rather than assuming its bin directory is on PATH.
        if importlib.util.find_spec("black") is None:
            raise FormatError("black is not installed — pip install black")
        return await _run(
            [sys.executable, "-m", "black", "--quiet", "-"], text, "black",
            "pip install black",
        )
    if formatter == "prettier":
        return await _run(
            ["prettier", f"--stdin-filepath={name}"], text, "prettier",
            "npm install -g prettier",
        )
    suf = Path(path).suffix.lstrip(".")
    raise FormatError(f"No formatter for .{suf or '?'} files")
