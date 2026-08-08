"""Code search: grep (ripgrep) and glob."""

import asyncio
import fnmatch
import os
import shutil
from pathlib import Path

from agent_server.config import MAX_TOOL_RESULT_CHARS
from agent_server.tools.base import ToolContext, ToolResult, truncate

MAX_MATCHES = 300
MAX_GLOB_RESULTS = 300
IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".next", ".ruff_cache", "target",
}


async def grep_search(
    ctx: ToolContext,
    *,
    pattern: str,
    path: str | None = None,
    include: str | None = None,
    **_,
) -> ToolResult:
    search_dir = ctx.resolve(path)
    title = f"grep '{pattern[:60]}'"

    if not search_dir.exists():
        return ToolResult.error(f"path not found: {search_dir}", title)
    if not shutil.which("rg"):
        return ToolResult.error(
            "ripgrep (rg) is not installed. Install it (e.g. `pacman -S ripgrep`) "
            "or use `bash` with grep -rn.",
            title,
        )

    cmd = ["rg", "--line-number", "--no-heading", "--color=never",
           "--max-count", "50", "--max-columns", "400", "--smart-case"]
    for d in IGNORED_DIRS:
        cmd += ["--glob", f"!{d}/"]
    if include:
        cmd += ["--glob", include]
    cmd += ["--regexp", pattern, str(search_dir)]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        return ToolResult.error("search timed out after 60s", title)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(f"search failed: {e}", title)

    if proc.returncode not in (0, 1):
        return ToolResult.error(
            stderr.decode("utf-8", errors="replace").strip() or "ripgrep failed", title
        )

    lines = stdout.decode("utf-8", errors="replace").splitlines()
    if not lines:
        return ToolResult(output=f"No matches for '{pattern}' in {search_dir}", title=f"{title} (0)")

    shown = lines[:MAX_MATCHES]
    output = "\n".join(shown)
    if len(lines) > MAX_MATCHES:
        output += f"\n\n... and {len(lines) - MAX_MATCHES:,} more matches. Narrow the pattern or set `include`."
    files = len({ln.split(":", 1)[0] for ln in shown})
    return ToolResult(
        output=truncate(output, MAX_TOOL_RESULT_CHARS),
        title=f"{title} ({len(lines)} matches in {files} files)",
    )


async def glob_search(ctx: ToolContext, *, pattern: str, path: str | None = None, **_) -> ToolResult:
    search_dir = ctx.resolve(path)
    title = f"glob '{pattern}'"

    if not search_dir.is_dir():
        return ToolResult.error(f"directory not found: {search_dir}", title)

    def _walk() -> list[tuple[float, str]]:
        results: list[tuple[float, str]] = []
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
            for name in files:
                full = Path(root) / name
                rel = str(full.relative_to(search_dir))
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                    try:
                        results.append((full.stat().st_mtime, rel))
                    except OSError:
                        continue
            if len(results) > 5000:
                break
        return results

    try:
        matches = await asyncio.to_thread(_walk)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(f"glob failed: {e}", title)

    if not matches:
        return ToolResult(output=f"No files matching '{pattern}' under {search_dir}", title=f"{title} (0)")

    # Most-recently-modified first: usually what the model is looking for.
    matches.sort(key=lambda t: t[0], reverse=True)
    names = [m[1] for m in matches[:MAX_GLOB_RESULTS]]
    output = "\n".join(names)
    if len(matches) > MAX_GLOB_RESULTS:
        output += f"\n\n... and {len(matches) - MAX_GLOB_RESULTS:,} more"
    return ToolResult(output=output, title=f"{title} ({len(matches)} files)")
