"""Project initialisation and directory browser."""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request

from agent_server.templating import templates

router = APIRouter()


@router.post("/_init")
async def init_project(request: Request):
    """Auto-detect project structure and write a rules file."""
    form = await request.form()
    project_dir = str(form.get("dir", "")).strip()
    if not project_dir:
        return {"ok": False, "error": "No directory provided"}
    p = Path(project_dir).expanduser().resolve()
    if not p.is_dir():
        return {"ok": False, "error": f"Not a directory: {p}"}

    rules_path = p / "AGENTS.md"
    content = _generate_rules(p)
    rules_path.write_text(content)

    return {"ok": True, "path": str(rules_path), "preview": content[:500]}


def _generate_rules(p: Path) -> str:
    """Scan a directory and produce a concise AGENTS.md."""
    try:
        entries = list(p.iterdir())
    except (OSError, PermissionError):
        return f"# Project rules\n\nCould not scan {p}: permission denied or unreadable.\n"
    files = {f.name for f in entries if f.is_file()}
    lines = ["# Project rules (auto-generated)", ""]
    lines.append(f"Generated from {p.name} at {datetime.now().strftime('%Y-%m-%d')}.")
    lines.append("")

    has_pkg = False
    if "package.json" in files:
        has_pkg = True
        try:
            import json as _json
            pkg = _json.loads((p / "package.json").read_text())
            name = pkg.get("name", p.name)
            lines.append(f"- **Project**: {name}")
            if pkg.get("scripts"):
                lines.append("- **Scripts**: " + ", ".join(f"`{k}`" for k in list(pkg["scripts"].keys())[:8]))
        except Exception:
            lines.append("- **Project**: Node.js (package.json)")
    if "tsconfig.json" in files:
        lines.append("- **Language**: TypeScript")
    if "requirements.txt" in files or "pyproject.toml" in files or "setup.py" in files:
        lines.append("- **Language**: Python")
    if "Cargo.toml" in files:
        lines.append("- **Language**: Rust")
    if "go.mod" in files:
        lines.append("- **Language**: Go")
    if "Makefile" in files:
        lines.append("- **Build**: Make")
    if "Dockerfile" in files:
        lines.append("- **Deploy**: Docker")

    # Test framework detection
    if any(f.startswith(".eslint") for f in files):
        lines.append("- **Lint**: ESLint")
    if "pyproject.toml" in files and has_pkg:
        lines.append("- **Lint/Format**: Check pyproject.toml for ruff/black config")
    if ".pylintrc" in files:
        lines.append("- **Lint**: Pylint")
    if "jest.config" in str(files) or "vitest.config" in str(files) or ".jest." in str(files):
        lines.append("- **Test**: Jest/Vitest")
    if "pytest" in str(files) or (p / "tests").is_dir() or (p / "test").is_dir():
        lines.append("- **Test**: Pytest")
    if "cargo" in str(files) and (p / "tests").is_dir():
        lines.append("- **Test**: Cargo test")

    # Git
    if (p / ".git").is_dir():
        lines.append("- **VCS**: Git — commit small, atomic changes with descriptive messages")

    lines.append("")
    lines.append("## Conventions")
    lines.append("")
    lines.append("- Read existing code before writing new code. Match the existing style.")
    lines.append("- Prefer the project's existing patterns over what you remember from elsewhere.")
    lines.append("- Run the project's tests after changes. If no tests exist, verify manually.")
    lines.append("- Delete unused code. Don't leave commented-out blocks or dead paths.")
    lines.append("")

    return "\n".join(lines)


@router.get("/_browse")
async def browse(request: Request, dir: str = "", show_hidden: bool = False):
    path = Path(dir).expanduser() if dir else Path.home()
    try:
        path = path.resolve()
    except OSError:
        path = Path.home()
    if not path.is_dir():
        path = path.parent if path.parent.is_dir() else Path.home()

    crumbs = []
    node = path
    while True:
        crumbs.append({"name": node.name or str(node), "path": str(node)})
        if node.parent == node:
            break
        node = node.parent
    crumbs.reverse()

    entries = []
    error = ""
    try:
        for entry in sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith(".") and not show_hidden:
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            entries.append({"name": entry.name, "path": str(entry), "is_dir": is_dir})
    except PermissionError:
        error = f"Permission denied: {path}"

    return templates.TemplateResponse(
        request=request, name="components/_browse_list.html",
        context={
            "current": str(path),
            "parent": str(path.parent) if path.parent != path else None,
            "crumbs": crumbs,
            "entries": entries[:500],
            "truncated": len(entries) > 500,
            "error": error,
            "show_hidden": show_hidden,
        },
    )


@router.post("/_browse/mkdir")
async def browse_mkdir(request: Request, dir: str = Form(...), name: str = Form(...)):
    parent = Path(dir).expanduser().resolve()
    safe = Path(name.strip()).name
    if not safe or safe in (".", ".."):
        raise HTTPException(400, "Invalid directory name")
    if not parent.is_dir():
        raise HTTPException(400, f"Not a directory: {parent}")
    target = parent / safe
    if target.exists():
        raise HTTPException(400, f"Already exists: {safe}")
    try:
        target.mkdir(parents=True)
    except OSError as e:
        raise HTTPException(400, f"Could not create directory: {e}") from e
    return await browse(request, dir=str(target))
