import subprocess
import os
from pathlib import Path


async def grep_search(*, pattern: str, path: str | None = None, include: str | None = None) -> str:
    search_dir = path or os.getcwd()
    if not Path(search_dir).exists():
        return f"Error: directory not found: {search_dir}"

    cmd = ["rg", "--line-number", "--no-heading", "--color=never"]
    if include:
        cmd.extend(["--glob", include])
    cmd.append(pattern)
    cmd.append(search_dir)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout.strip()
        return output[:50000] if output else "No matches found"
    except subprocess.TimeoutExpired:
        return "Search timed out"
    except FileNotFoundError:
        return "Error: ripgrep (rg) not installed. Install it with your package manager."
    except Exception as e:
        return f"Error during search: {e}"


async def glob_search(*, pattern: str, path: str | None = None) -> str:
    from glob import glob as pyglob
    search_dir = path or os.getcwd()
    full_pattern = os.path.join(search_dir, pattern)
    try:
        matches = sorted(pyglob(full_pattern, recursive=True))
        if not matches:
            return f"No files matching '{pattern}'"
        # Show relative paths
        output = "\n".join(os.path.relpath(m, search_dir) for m in matches[:500])
        if len(matches) > 500:
            output += f"\n... and {len(matches) - 500} more"
        return output
    except Exception as e:
        return f"Error during glob: {e}"
