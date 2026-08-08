import os
from pathlib import Path


def _read_path(filePath: str) -> str:
    return filePath


async def read_file(*, filePath: str, offset: int = 0, limit: int = 2000) -> str:
    path = Path(filePath)
    if not path.exists():
        return f"Error: file not found: {filePath}"
    if not path.is_file():
        return f"Error: not a file: {filePath}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        start = max(0, offset - 1) if offset > 0 else 0
        end = min(total, start + limit)
        result_lines = lines[start:end]
        output = "".join(f"{i+1}: {line}" for i, line in enumerate(result_lines, start=start))
        if not output.strip():
            return f"File is empty: {filePath}"
        if end < total:
            output += f"\n... ({total - end} more lines, use offset={end+1} to continue)"
        return output
    except UnicodeDecodeError:
        return f"Error: cannot read binary file as text: {filePath}"
    except Exception as e:
        return f"Error reading file: {e}"


async def edit_file(*, filePath: str, oldString: str, newString: str, replaceAll: bool = False) -> str:
    path = Path(filePath)
    if not path.exists():
        return f"Error: file not found: {filePath}"
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading file: {e}"

    count = content.count(oldString)
    if count == 0:
        return f"Error: oldString not found in {filePath}"
    if count > 1 and not replaceAll:
        return f"Error: found {count} occurrences of oldString. Use replaceAll=true or provide more context."

    new_content = content.replace(oldString, newString) if replaceAll else content.replace(oldString, newString, 1)
    try:
        path.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return f"Error writing file: {e}"
    return f"File edited successfully: {filePath} ({'all ' + str(count) if replaceAll else '1'} occurrence(s) replaced)"


async def write_file(*, filePath: str, content: str) -> str:
    path = Path(filePath)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content, encoding="utf-8")
        action = "Created" if not path.exists() else "Overwrote"
        return f"{action} file: {filePath}"
    except Exception as e:
        return f"Error writing file: {e}"
