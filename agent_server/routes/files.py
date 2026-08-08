import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/browse", tags=["browse"])


@router.get("")
async def browse_dir(dir: str = ""):
    """List directory contents. Returns parent path, entries, and breadcrumbs."""
    path = Path(dir).expanduser().resolve() if dir else Path.home()

    if not path.exists():
        path = Path.home()
    if not path.is_dir():
        path = path.parent

    # Breadcrumbs
    crumbs = []
    p = path
    while p != p.parent:
        crumbs.append({"name": p.name or str(p), "path": str(p)})
        p = p.parent
    crumbs.reverse()

    # List entries
    entries = []
    try:
        for entry in sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith("."):
                continue
            entries.append({
                "name": entry.name,
                "path": str(entry),
                "is_dir": entry.is_dir(),
            })
    except PermissionError:
        raise HTTPException(403, f"Permission denied: {path}")

    return {
        "current": str(path),
        "parent": str(path.parent) if path != path.parent else None,
        "crumbs": crumbs,
        "entries": entries,
    }


@router.post("/mkdir")
async def create_directory(dir: str = Query(...), name: str = Query(...)):
    """Create a new directory inside `dir`."""
    safe_name = Path(name).name  # strip any path traversal
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(400, "Invalid directory name")
    new_path = Path(dir).expanduser().resolve() / safe_name
    if new_path.exists():
        raise HTTPException(400, f"Already exists: {safe_name}")
    new_path.mkdir(parents=True)
    return {"ok": True, "path": str(new_path)}
