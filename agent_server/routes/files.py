"""Read, list, and save files for the in-app editor and file browser.

The human user drives these from the browser, so reads and listings are
unrestricted -- exactly like the `read` tool, which the agent runs with no
permission gate. Saves go through the same write gate as `edit`/`write`, so the
editor can never write outside the project or a granted directory without
asking. BOM and line-ending style are preserved by reusing the tool helpers.
"""

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent_server import database as db
from agent_server import permissions
from agent_server.formatting import FormatError, format_text
from agent_server.tools.file_ops import (
    _BOM,
    _detect_line_ending,
    _read_file_text,
    _write_file_text,
    lang_for_path,
)

router = APIRouter(prefix="/api/files", tags=["files"])

# The editor refuses to load a file past this many bytes; a 40MB minified bundle
# is not something anyone edits by hand, and it would freeze the page.
MAX_READ_BYTES = 2 * 1024 * 1024

# A null byte this early in the file marks it binary, which a text editor cannot
# round-trip anyway.
_BINARY_SNIFF = 8000


class SaveRequest(BaseModel):
    session_id: str
    path: str
    content: str


class PathRequest(BaseModel):
    session_id: str
    path: str


class RenameRequest(BaseModel):
    session_id: str
    path: str
    name: str


class MoveRequest(BaseModel):
    session_id: str
    paths: list[str]
    dest: str


class FormatRequest(BaseModel):
    session_id: str
    path: str
    content: str


async def _session(session_id: str) -> dict:
    session = await db.get_session(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    return session


def _resolve(session: dict, path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(session["project_dir"]) / p
    return p


@router.get("/stat")
async def stat_path(session_id: str, path: str):
    """Whether a path exists and is a file or directory, so a clicked reference
    can open the right surface (editor for files, file manager for folders)."""
    session = await _session(session_id)
    p = _resolve(session, path)
    size = None
    if p.is_file():
        try:
            size = p.stat().st_size
        except OSError:
            size = None
    return {
        "path": str(p),
        "exists": p.exists(),
        "is_dir": p.is_dir(),
        "is_file": p.is_file(),
        "size": size,
    }


@router.get("/list")
async def list_dir(session_id: str, path: str = ""):
    """One level of a directory: folders first, then files, each with a size."""
    session = await _session(session_id)
    d = _resolve(session, path)
    if not d.is_dir():
        raise HTTPException(404, f"Not a directory: {d}")
    entries = []
    try:
        children = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for child in children:
            try:
                is_dir = child.is_dir()
            except OSError:
                is_dir = False
            entries.append({
                "name": child.name,
                "is_dir": is_dir,
                "size": None if is_dir else _size(child),
            })
    except (PermissionError, OSError) as e:
        raise HTTPException(403, f"Cannot list {d}: {e}") from None
    return {"path": str(d), "parent": str(d.parent), "entries": entries}


@router.post("/mkdir")
async def make_directory(body: PathRequest):
    """Create a directory (and parents), gated like a file write."""
    session = await _session(body.session_id)
    p = _resolve(session, body.path)
    if permissions.is_denied(p):
        raise HTTPException(403, f"{p} is a protected path.")
    if not await permissions.write_allowed(body.session_id, p, session["project_dir"]):
        raise HTTPException(
            403,
            f"{p} is outside the project and no directory grant covers it.",
        )
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(403, f"Cannot create {p}: {e}") from None
    return {"ok": True, "path": str(p)}


async def _require_write(session_id: str, path: Path, project_dir: str):
    """Raise unless the path exists and may be written."""
    if not path.exists():
        raise HTTPException(404, f"Not found: {path}")
    if permissions.is_denied(path) or not await permissions.write_allowed(
        session_id, path, project_dir
    ):
        raise HTTPException(
            403, f"{path} is outside the project and no directory grant covers it."
        )


@router.post("/rename")
async def rename_entry(body: RenameRequest):
    """Rename a file or folder in place."""
    session = await _session(body.session_id)
    p = _resolve(session, body.path)
    name = Path(body.name).name
    if not name or name in (".", ".."):
        raise HTTPException(400, "Invalid name")
    await _require_write(body.session_id, p, session["project_dir"])
    target = p.parent / name
    if not await permissions.write_allowed(body.session_id, target, session["project_dir"]):
        raise HTTPException(403, f"{target} is outside the project.")
    if target.exists():
        raise HTTPException(409, f"{target} already exists")
    try:
        p.rename(target)
    except OSError as e:
        raise HTTPException(403, f"Cannot rename {p}: {e}") from None
    return {"ok": True, "path": str(target)}


@router.post("/delete")
async def delete_entry(body: PathRequest):
    """Delete a file or folder (recursively)."""
    session = await _session(body.session_id)
    p = _resolve(session, body.path)
    await _require_write(body.session_id, p, session["project_dir"])
    try:
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    except OSError as e:
        raise HTTPException(403, f"Cannot delete {p}: {e}") from None
    return {"ok": True}


@router.post("/move")
async def move_entries(body: MoveRequest):
    """Move one or more files/folders into a destination directory."""
    session = await _session(body.session_id)
    dest = _resolve(session, body.dest)
    if not dest.is_dir():
        raise HTTPException(404, f"Not a directory: {dest}")
    await _require_write(body.session_id, dest, session["project_dir"])
    moved = []
    for src in body.paths:
        p = _resolve(session, src)
        await _require_write(body.session_id, p, session["project_dir"])
        target = dest / p.name
        if target.exists():
            raise HTTPException(409, f"{target} already exists")
        try:
            shutil.move(str(p), str(target))
        except OSError as e:
            raise HTTPException(403, f"Cannot move {p}: {e}") from None
        moved.append(str(target))
    return {"ok": True, "paths": moved}


@router.post("/copy")
async def copy_entry(body: PathRequest):
    """Duplicate a file or folder in place as 'name (copy).ext'."""
    session = await _session(body.session_id)
    p = _resolve(session, body.path)
    await _require_write(body.session_id, p, session["project_dir"])
    target = _copy_name(p)
    if not await permissions.write_allowed(body.session_id, target, session["project_dir"]):
        raise HTTPException(403, f"{target} is outside the project.")
    try:
        shutil.copytree(p, target) if p.is_dir() else shutil.copy2(p, target)
    except OSError as e:
        raise HTTPException(403, f"Cannot copy {p}: {e}") from None
    return {"ok": True, "path": str(target)}


def _copy_name(path: Path) -> Path:
    """'name.ext' -> 'name (copy).ext', then 'name (copy 2).ext', and so on."""
    n = 1
    while True:
        label = "copy" if n == 1 else f"copy {n}"
        candidate = path.parent / f"{path.stem} ({label}){path.suffix}"
        if not candidate.exists():
            return candidate
        n += 1


@router.post("/format")
async def format_file(body: FormatRequest):
    """Reformat text with the formatter matching the file's extension.

    Read-only with respect to the disk: the caller decides whether to save the
    returned text. A missing formatter or unparseable input is a 400 with a
    message the editor shows inline.
    """
    session = await _session(body.session_id)
    p = _resolve(session, body.path)
    try:
        formatted = await format_text(str(p), body.content)
    except FormatError as e:
        raise HTTPException(400, str(e)) from None
    return {"content": formatted}


@router.get("/read")
async def read_file(session_id: str, path: str):
    """File contents as UTF-8 text, with the metadata needed to preserve its
    BOM and line-ending style if the user saves it back."""
    session = await _session(session_id)
    p = _resolve(session, path)
    if not p.is_file():
        raise HTTPException(404, f"Not a file: {p}")
    try:
        size = p.stat().st_size
        # Read only the editor's limit (plus a little for a trailing multi-byte
        # character) so a multi-gigabyte file is not buffered whole and then
        # thrown away. `size` still reports the true file length.
        with open(p, "rb") as f:
            raw = f.read(MAX_READ_BYTES + 4)
    except OSError as e:
        raise HTTPException(403, f"Cannot read {p}: {e}") from None
    if b"\x00" in raw[:_BINARY_SNIFF]:
        raise HTTPException(400, "That is a binary file, not text.")
    has_bom = raw.startswith(_BOM)
    body = raw[len(_BOM):] if has_bom else raw
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        # Non-UTF-8 text: show it lossily and do not try to preserve the BOM or
        # line endings on save, since the encoding cannot be round-tripped.
        return {
            "path": str(p),
            "content": raw.decode("utf-8", errors="replace")[:MAX_READ_BYTES],
            "truncated": size > MAX_READ_BYTES,
            "size": size,
            "has_bom": False,
            "line_ending": "\n",
            "lang": lang_for_path(p),
        }
    line_ending = _detect_line_ending(text)
    if line_ending == "\r\n":
        text = text.replace("\r\n", "\n")
    return {
        "path": str(p),
        "content": text[:MAX_READ_BYTES],
        "truncated": size > MAX_READ_BYTES,
        "size": size,
        "has_bom": has_bom,
        "line_ending": line_ending,
        "lang": lang_for_path(p),
    }


@router.post("/save")
async def save_file(body: SaveRequest):
    """Write text back to a file, gated by the same permissions as the tools."""
    session = await _session(body.session_id)
    p = _resolve(session, body.path)
    if len(body.content) > MAX_READ_BYTES * 4:
        raise HTTPException(400, "File content too large to save.")
    if permissions.is_denied(p):
        raise HTTPException(403, f"{p} is a protected path.")
    if not await permissions.write_allowed(body.session_id, p, session["project_dir"]):
        raise HTTPException(
            403,
            f"{p} is outside the project and no directory grant covers it. "
            "Grant write access first.",
        )
    try:
        has_bom, line_ending = False, "\n"
        if p.is_file():
            try:
                _, has_bom, line_ending = _read_file_text(p)
            except UnicodeDecodeError:
                has_bom, line_ending = False, "\n"
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_file_text(p, body.content, has_bom, line_ending)
    except (OSError, UnicodeDecodeError) as e:
        raise HTTPException(403, f"Cannot write {p}: {e}") from None
    return {"ok": True, "path": str(p)}


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
