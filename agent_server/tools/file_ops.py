"""File reading and editing tools."""

import hashlib
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

# UTF-8 BOM as raw bytes.
_BOM = b"\xef\xbb\xbf"


def _detect_line_ending(text: str) -> str:
    """Return the dominant line ending: ``\\r\\n`` or ``\\n``."""
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    return "\r\n" if crlf > lf_only else "\n"


def _read_file_text(path: Path) -> tuple[str, bool, str]:
    """Read *path* and return ``(content, has_bom, line_ending)``.

    ``content`` has any leading UTF-8 BOM stripped and all line endings
    normalised to ``\\n`` so edits operate on a canonical form.
    """
    raw = path.read_bytes()
    has_bom = raw.startswith(_BOM)
    if has_bom:
        raw = raw[len(_BOM):]
    text = raw.decode("utf-8")
    line_ending = _detect_line_ending(text)
    if line_ending == "\r\n":
        text = text.replace("\r\n", "\n")
    return text, has_bom, line_ending


def _write_file_text(path: Path, content: str, has_bom: bool, line_ending: str):
    """Write *content* to *path*, prepending a BOM and converting line endings
    back to what the file originally used."""
    if line_ending == "\r\n":
        content = content.replace("\n", "\r\n")
    data = content.encode("utf-8")
    if has_bom:
        data = _BOM + data
    path.write_bytes(data)


def _hash_line(line: str) -> str:
    """4-char content hash so edits can anchor on a line without retyping it."""
    return hashlib.md5(line[:128].encode()).hexdigest()[:4]


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
        try:
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        except PermissionError:
            return ToolResult.error(f"permission denied reading directory: {path}", title)
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
    except Exception as e:
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
        numbered.append(f"{idx + 1}|{_hash_line(line)}| {line}")

    output = "\n".join(numbered)
    if end < total:
        output += f"\n\n... ({total - end:,} more lines; continue with offset={end + 1})"

    mark_read(ctx.session_id, path)
    return ToolResult(output=output, title=f"{title} ({total} lines)")


async def edit_file(
    ctx: ToolContext,
    *,
    filePath: str,
    oldString: str = "",
    newString: str = "",
    replaceAll: bool = False,
    hashStart: str = "",
    hashEnd: str = "",
    startLine: int = 0,
    endLine: int = 0,
    newText: str = "",
    **_,
) -> ToolResult:
    path = ctx.resolve(filePath)
    title = f"edit {_display(path, ctx)}"

    if not path.exists():
        return ToolResult.error(f"file not found: {path}. Use `write` to create it.", title)
    if not path.is_file():
        return ToolResult.error(f"not a file: {path}", title)
    if not has_read(ctx.session_id, path):
        return ToolResult.error(
            f"you must read {path} before editing it", title
        )

    try:
        content, has_bom, line_ending = _read_file_text(path)
    except Exception as e:
        return ToolResult.error(f"reading file: {e}", title)

    # ── hashline mode: anchor edits on 4-char content hashes ──
    if hashStart:
        lines = content.splitlines()
        try:
            start_idx = _resolve_hash(lines, hashStart, startLine, "hashStart")
            end_idx = (
                _resolve_hash(lines, hashEnd, endLine, "hashEnd")
                if hashEnd else start_idx
            )
        except _HashError as e:
            return ToolResult.error(f"{e} (in {path})", title)

        if end_idx < start_idx:
            # Silently swapping these deleted a span the caller never named.
            return ToolResult.error(
                f"hashEnd is at line {end_idx + 1}, above hashStart at line "
                f"{start_idx + 1}. Give them in the order they appear.",
                title,
            )

        replaced_lines = end_idx - start_idx + 1
        replacement_lines = newText.count("\n") + 1 if newText else 0
        new_lines = lines[:start_idx] + (newText.splitlines() if newText else []) + lines[end_idx + 1:]
        updated = "\n".join(new_lines)
        if content.endswith("\n"):
            updated += "\n"

        try:
            _write_file_text(path, updated, has_bom, line_ending)
        except Exception as e:
            return ToolResult.error(f"writing file: {e}", title)

        diff = unified_diff(content, updated, _display(path, ctx))
        added, removed = diff_stats(diff)
        return ToolResult(
            output=f"Edited {path} (replaced {replaced_lines} line{'s' if replaced_lines != 1 else ''}"
                   + (f" with {replacement_lines}" if replacement_lines != replaced_lines else "")
                   + f" starting at line {start_idx + 1}).",
            title=f"{title} (+{added}/-{removed})",
            diff=diff,
        )

    # ── exact-string mode ──
    if not oldString:
        return ToolResult.error("provide oldString or hashStart", title)
    if oldString == newString:
        return ToolResult.error("oldString and newString are identical", title)

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
        _write_file_text(path, updated, has_bom, line_ending)
    except Exception as e:
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
            previous, has_bom, line_ending = _read_file_text(path)
        except Exception:
            previous = ""
            has_bom = False
            line_ending = "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _write_file_text(path, content, has_bom, line_ending)
        except Exception as e:
            return ToolResult.error(f"writing file: {e}", title)
    else:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception as e:
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


class _HashError(Exception):
    """A hash anchor that cannot be resolved to exactly one line."""


# How far from a stated line number to look for the hash. An edit earlier in
# the same file shifts everything below it, and re-reading the whole file to
# recover four characters that have not changed is pure waste.
_DRIFT = 40


def _resolve_hash(lines: list[str], target: str, line_no: int, label: str) -> int:
    """The single line index `target` refers to.

    The hash is of the line's content alone, so every blank line in a file
    shares one, as does every `}` and every `    return`. Returning the first
    match -- which is what this did -- meant an anchor on a repeated line
    silently rewrote the first occurrence in the file rather than the one the
    caller had read. Ambiguity is now an error, and `startLine`/`endLine`
    resolve it.
    """
    matches = [i for i, line in enumerate(lines) if _hash_line(line) == target]

    if line_no:
        index = line_no - 1
        # Exact hit first: the stated line still holds what was read.
        if 0 <= index < len(lines) and _hash_line(lines[index]) == target:
            return index
        # Otherwise the nearest match, so an earlier edit shifting the file
        # does not force a re-read of something that has not changed.
        near = [i for i in matches if abs(i - index) <= _DRIFT]
        if len(near) == 1:
            return near[0]
        if len(near) > 1:
            return min(near, key=lambda i: abs(i - index))
        if not matches:
            raise _HashError(
                f"{label} {target} does not match line {line_no} or anything "
                f"within {_DRIFT} lines of it. The file changed since you read "
                "it -- read it again."
            )
        raise _HashError(
            f"{label} {target} is nowhere near line {line_no}; it matches "
            f"line{'s' if len(matches) > 1 else ''} "
            f"{', '.join(str(i + 1) for i in matches[:8])}. Read the file again."
        )

    if not matches:
        raise _HashError(
            f"{label} {target} not found. The file changed since you read it "
            "-- read it again."
        )
    if len(matches) > 1:
        where = ", ".join(str(i + 1) for i in matches[:8])
        more = f" and {len(matches) - 8} more" if len(matches) > 8 else ""
        raise _HashError(
            f"{label} {target} matches {len(matches)} identical lines "
            f"({where}{more}) -- blank lines and lines like `}}` all hash the "
            f"same. Pass {label.replace('hash', '').lower() or 'start'}Line "
            "with the line number you meant, or anchor on a unique line."
        )
    return matches[0]


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


__all__ = ["edit_file", "has_read", "mark_read", "read_file", "truncate", "write_file"]
