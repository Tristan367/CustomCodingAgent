"""File reading and editing tools."""

from pathlib import Path

from agent_server.tools.base import ToolContext, ToolResult, diff_stats, truncate, unified_diff

MAX_READ_BYTES = 2_000_000
DEFAULT_LIMIT = 2000
MAX_LINE_CHARS = 2000

BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip",
    ".gz", ".tar", ".bz2", ".xz", ".7z", ".exe", ".dll", ".so", ".dylib",
    ".class", ".jar", ".pyc", ".o", ".a", ".wasm", ".mp3", ".mp4", ".mov",
    ".wav", ".ogg", ".woff", ".woff2", ".ttf", ".sqlite", ".db",
}

# Files the model has read this session; `edit`/`write` require a prior read so
# the model cannot blindly clobber a file it has never seen.
_read_files: dict[str, set[str]] = {}


def clear_read_cache(session_id: str = ""):
    """Release the read-tracking for a session, or all of them."""
    if session_id:
        _read_files.pop(session_id, None)
    else:
        _read_files.clear()


def mark_read(session_id: str, path: Path):
    _read_files.setdefault(session_id, set()).add(str(path))


def has_read(session_id: str, path: Path) -> bool:
    return str(path) in _read_files.get(session_id, set())


async def read_file(
    ctx: ToolContext,
    *,
    filePath: str,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    **_,
) -> ToolResult:
    path = ctx.resolve(filePath)
    title = f"read {_display(path, ctx)}"

    if not path.exists():
        suggestion = _suggest(path)
        return ToolResult.error(f"file not found: {path}{suggestion}", title)
    if path.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return ToolResult(
            output=f"{path} is a directory. Contents:\n" + "\n".join(entries[:200]),
            title=f"list {_display(path, ctx)}",
        )
    if path.suffix.lower() in BINARY_SUFFIXES:
        return ToolResult.error(f"cannot read binary file as text: {path}", title)
    if path.stat().st_size > MAX_READ_BYTES:
        return ToolResult.error(
            f"file too large ({path.stat().st_size:,} bytes). Use offset/limit or grep.", title
        )

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(f"reading file: {e}", title)

    if "\x00" in content[:8192]:
        return ToolResult.error(f"cannot read binary file as text: {path}", title)

    lines = content.splitlines()
    total = len(lines)
    if not total:
        return ToolResult(output=f"(file is empty: {path})", title=title)

    limit = max(1, limit or DEFAULT_LIMIT)
    start = max(0, (offset - 1) if offset and offset > 0 else 0)
    if start >= total:
        return ToolResult.error(f"offset {offset} is past end of file ({total} lines)", title)
    end = min(total, start + limit)

    numbered = []
    for idx in range(start, end):
        line = lines[idx]
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + "... [line truncated]"
        numbered.append(f"{idx + 1}: {line}")

    output = "\n".join(numbered)
    if end < total:
        output += f"\n\n... ({total - end:,} more lines; continue with offset={end + 1})"

    mark_read(ctx.session_id, path)
    return ToolResult(output=output, title=f"{title} ({total} lines)")


async def edit_file(
    ctx: ToolContext,
    *,
    filePath: str,
    oldString: str,
    newString: str,
    replaceAll: bool = False,
    **_,
) -> ToolResult:
    path = ctx.resolve(filePath)
    title = f"edit {_display(path, ctx)}"

    if not path.exists():
        return ToolResult.error(f"file not found: {path}. Use `write` to create it.", title)
    if not path.is_file():
        return ToolResult.error(f"not a file: {path}", title)
    if oldString == newString:
        return ToolResult.error("oldString and newString are identical", title)
    if not has_read(ctx.session_id, path):
        return ToolResult.error(
            f"you must read {path} before editing it", title
        )

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(f"reading file: {e}", title)

    count = content.count(oldString)
    if count == 0:
        return ToolResult.error(
            f"oldString not found in {path}. The file may have changed since you read it; "
            "read it again and match the exact text including indentation.",
            title,
        )
    if count > 1 and not replaceAll:
        return ToolResult.error(
            f"found {count} occurrences of oldString in {path}. "
            "Add surrounding context to make it unique, or pass replaceAll=true.",
            title,
        )

    updated = content.replace(oldString, newString) if replaceAll else content.replace(oldString, newString, 1)
    try:
        path.write_text(updated, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(f"writing file: {e}", title)

    replaced = count if replaceAll else 1
    line_no = content[: content.index(oldString)].count("\n") + 1
    diff = unified_diff(content, updated, _display(path, ctx))
    added, removed = diff_stats(diff)
    return ToolResult(
        output=f"Edited {path} ({replaced} replacement{'s' if replaced != 1 else ''} at line ~{line_no}).",
        title=f"{title} (+{added}/-{removed})",
        diff=diff,
    )


async def write_file(ctx: ToolContext, *, filePath: str, content: str, **_) -> ToolResult:
    path = ctx.resolve(filePath)
    title = f"write {_display(path, ctx)}"

    existed = path.exists()
    if existed and path.is_dir():
        return ToolResult.error(f"{path} is a directory", title)
    if existed and not has_read(ctx.session_id, path):
        return ToolResult.error(
            f"{path} already exists and you have not read it. Read it first so you do "
            "not discard existing content, or use `edit` for a targeted change.",
            title,
        )

    previous = ""
    if existed:
        try:
            previous = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            previous = ""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(f"writing file: {e}", title)

    mark_read(ctx.session_id, path)
    verb = "Overwrote" if existed else "Created"
    lines = len(content.splitlines())
    diff = unified_diff(previous, content, _display(path, ctx))
    added, removed = diff_stats(diff)
    summary = f"{title} (+{added}/-{removed})" if existed else f"{title} ({lines} lines)"
    return ToolResult(
        output=f"{verb} {path} ({lines} lines).",
        title=summary,
        diff=diff,
    )


def _display(path: Path, ctx: ToolContext) -> str:
    try:
        return str(path.relative_to(ctx.project_dir))
    except ValueError:
        return str(path)


def _suggest(path: Path) -> str:
    """If the parent exists, hint at similarly-named siblings."""
    parent = path.parent
    if not parent.is_dir():
        return ""
    import difflib

    try:
        names = [p.name for p in parent.iterdir()]
    except OSError:
        return ""
    close = difflib.get_close_matches(path.name, names, n=3, cutoff=0.6)
    return f"\nDid you mean: {', '.join(close)}" if close else ""


__all__ = ["read_file", "edit_file", "write_file", "mark_read", "has_read", "truncate"]
