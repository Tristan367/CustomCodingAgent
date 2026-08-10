"""Skill tool — on-demand loading of SKILL.md files."""

from pathlib import Path

from agent_server.tools.base import ToolContext, ToolResult

SKILL_DIRS = [
    Path.home() / ".config" / "codeagent" / "skills",
    Path.home() / ".skills",
]


async def load_skill(ctx: ToolContext, *, name: str = "", **_) -> ToolResult:
    """List skills or load a specific one by name.

    Without `name`: list available skills.
    With `name`: return the skill's Markdown content.
    """
    if not name:
        skills = _list_skills()
        if not skills:
            return ToolResult(
                output="No skills found. Create .md files in ~/.config/codeagent/skills/",
                title="skill (list)",
            )
        lines = ["Available skills:"]
        for s in skills:
            lines.append(f"  {s['name']} — {s['summary']}")
        return ToolResult(output="\n".join(lines), title=f"skill ({len(skills)} available)")

    safe = name.strip().lower().replace("/", "_").replace("..", "")
    content, path = _load(safe)
    if content is None:
        candidates = [s["name"] for s in _list_skills()]
        hint = ""
        if candidates:
            hint = f" Available: {', '.join(candidates)}"
        return ToolResult.error(f"skill '{name}' not found.{hint}", "skill")
    return ToolResult(output=content, title=f"skill ({name}) — {path}")


def _list_skills() -> list[dict]:
    out = []
    for d in SKILL_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                p = p.resolve(strict=False)
            except OSError:
                continue
            if not p.is_file():
                continue
            name = p.stem.lower()
            if any(s["name"] == name for s in out):
                continue
            summary = _first_para(p)
            out.append({"name": name, "summary": summary, "path": str(p)})
    return out


def _load(name: str) -> tuple[str | None, str]:
    for d in SKILL_DIRS:
        p = d / f"{name}.md"
        try:
            p = p.resolve(strict=False)
        except OSError:
            continue
        # Prevent symlink traversal outside SKILL_DIRS
        try:
            inside = any(p.resolve().is_relative_to(s.resolve()) for s in SKILL_DIRS if s.is_dir())
        except OSError:
            inside = False
        if not inside:
            continue
        if p.is_file():
            return p.read_text(), str(p)
    return None, ""


def _first_para(p: Path) -> str:
    try:
        text = p.read_text()
        stripped = (line.strip() for line in text.splitlines())
        lines = [line for line in stripped if line and not line.startswith("#")]
        if lines:
            return lines[0][:80]
    except Exception:
        pass
    return "..."
