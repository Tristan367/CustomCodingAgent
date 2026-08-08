"""Tool registry: schemas, dispatch, and permission policy."""

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Literal

from agent_server.tools.base import ToolContext, ToolResult
from agent_server.tools.bash import is_read_only, run_bash
from agent_server.tools.file_ops import edit_file, read_file, write_file
from agent_server.tools.question import ask_question
from agent_server.tools.search import glob_search, grep_search
from agent_server.tools.task import run_task
from agent_server.tools.vision import screenshot, vision
from agent_server.tools.web import webfetch

Handler = Callable[..., Awaitable[ToolResult]]
PauseKind = Literal["permission", "question"]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler
    # "permission": run only after the user approves.
    # "question":   the user supplies the result; the handler is never called.
    pause: PauseKind | None = None
    # Called with (args) -> bool. Lets a tool waive its own permission prompt.
    auto_allow: Callable[[dict], bool] | None = None
    vision_only: bool = field(default=False)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


TOOLS: dict[str, Tool] = {}


def register(tool: Tool):
    TOOLS[tool.name] = tool


register(Tool(
    name="read",
    description=(
        "Read a file from the filesystem. Returns contents prefixed with line numbers. "
        "Prefer absolute paths. Use offset/limit for large files. You must read a file "
        "before you edit it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filePath": {"type": "string", "description": "Path to the file (absolute preferred)"},
            "offset": {"type": "integer", "description": "1-indexed line to start from"},
            "limit": {"type": "integer", "description": "Maximum lines to return (default 2000)"},
        },
        "required": ["filePath"],
    },
    handler=read_file,
))

register(Tool(
    name="edit",
    description=(
        "Replace an exact string in an existing file. oldString must match the file "
        "byte-for-byte including indentation, and must be unique unless replaceAll is true. "
        "Read the file first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filePath": {"type": "string", "description": "Path to the file"},
            "oldString": {"type": "string", "description": "Exact text to replace"},
            "newString": {"type": "string", "description": "Replacement text"},
            "replaceAll": {"type": "boolean", "description": "Replace every occurrence"},
        },
        "required": ["filePath", "oldString", "newString"],
    },
    handler=edit_file,
))

register(Tool(
    name="write",
    description=(
        "Create a new file, or overwrite an existing one in full. For changes to an "
        "existing file prefer `edit`. If the file exists you must read it first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filePath": {"type": "string", "description": "Path to the file"},
            "content": {"type": "string", "description": "Full file contents"},
        },
        "required": ["filePath", "content"],
    },
    handler=write_file,
))

register(Tool(
    name="bash",
    description=(
        "Run a shell command in the project directory. Use for git, builds, tests, and "
        "package managers. Do not use it to read or search files -- use read/grep/glob, "
        "which are faster and better formatted."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {"type": "integer", "description": "Timeout in milliseconds (default 120000)"},
            "workdir": {"type": "string", "description": "Directory to run in (defaults to project dir)"},
        },
        "required": ["command"],
    },
    handler=run_bash,
    pause="permission",
    auto_allow=lambda args: is_read_only(args.get("command", "")),
))

register(Tool(
    name="grep",
    description=(
        "Search file contents with a regular expression (ripgrep). Returns matching lines "
        "with file paths and line numbers."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression"},
            "path": {"type": "string", "description": "Directory to search (defaults to project dir)"},
            "include": {"type": "string", "description": "Glob filter, e.g. '*.py'"},
        },
        "required": ["pattern"],
    },
    handler=grep_search,
))

register(Tool(
    name="glob",
    description="Find files by name pattern, newest first. Example: 'src/**/*.ts'.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern"},
            "path": {"type": "string", "description": "Directory to search (defaults to project dir)"},
        },
        "required": ["pattern"],
    },
    handler=glob_search,
))

register(Tool(
    name="webfetch",
    description="Fetch a URL and return its content as readable text.",
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Absolute http(s) URL"}},
        "required": ["url"],
    },
    handler=webfetch,
))

register(Tool(
    name="question",
    description=(
        "Ask the user a question and wait for their answer. Use when a decision is "
        "genuinely ambiguous and guessing wrong would waste significant work."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask. Markdown is rendered.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional suggested answers. Markdown is rendered.",
            },
            "multiple": {
                "type": "boolean",
                "description": (
                    "Allow several options to be chosen at once. The answer comes "
                    "back as the chosen options separated by semicolons."
                ),
            },
        },
        "required": ["question"],
    },
    handler=ask_question,
    pause="question",
))

register(Tool(
    name="task",
    description=(
        "Delegate open-ended research to a read-only subagent that works autonomously and "
        "reports back once. Good for 'where is X handled?' style questions across many "
        "files. Give it a self-contained prompt; it sees none of this conversation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "3-5 word label"},
            "prompt": {
                "type": "string",
                "description": "Complete instructions, including exactly what to report back",
            },
        },
        "required": ["description", "prompt"],
    },
    handler=run_task,
))

register(Tool(
    name="vision",
    description=(
        "Look at images with a vision model and get a description back. Pass `paths` "
        "for image files on disk (screenshots, photos, diagrams, anything the user "
        "attached), and/or `url` to capture a web page first. Give several paths to "
        "compare images -- each is labelled by filename, so you can ask what changed "
        "between them. Always include a `prompt` saying what you need to know."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "What to find out, e.g. 'what error is shown?' or "
                               "'what differs between these two?'",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Image files to look at, in order (max 6)",
            },
            "url": {"type": "string", "description": "Page to capture and include"},
            "selector": {"type": "string", "description": "CSS selector to crop the capture to"},
            "full_page": {"type": "boolean", "description": "Capture the whole scrollable page"},
            "width": {"type": "integer", "description": "Viewport width (default 1280)"},
            "height": {"type": "integer", "description": "Viewport height (default 900)"},
        },
        "required": ["prompt"],
    },
    handler=vision,
    vision_only=True,
))

register(Tool(
    name="screenshot",
    description=(
        "Capture a web page to PNG files and return their paths. Use `count` and "
        "`interval_ms` to record a sequence, which is how you inspect animations, "
        "loading states, or anything that changes over time. `actions` lets you click, "
        "fill, hover, or scroll before capturing so you can reach a specific state. "
        "Add `prompt` to have the result described immediately, or feed the returned "
        "paths to `vision` later."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Page to capture (http, https, or file://)"},
            "selector": {"type": "string", "description": "CSS selector to crop to"},
            "full_page": {"type": "boolean", "description": "Whole scrollable page"},
            "width": {"type": "integer", "description": "Viewport width (default 1280)"},
            "height": {"type": "integer", "description": "Viewport height (default 900)"},
            "wait_for": {"type": "string", "description": "Wait for this selector before capturing"},
            "delay_ms": {"type": "integer", "description": "Extra pause before the first frame"},
            "count": {"type": "integer", "description": "Number of frames, 1-12 (default 1)"},
            "interval_ms": {"type": "integer", "description": "Gap between frames (default 500)"},
            "actions": {
                "type": "array",
                "description": "Steps to run before capturing",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["click", "fill", "press", "hover", "scroll", "wait"],
                        },
                        "selector": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["type"],
                },
            },
            "prompt": {"type": "string", "description": "If set, describe the capture immediately"},
        },
        "required": ["url"],
    },
    handler=screenshot,
    vision_only=True,
))


def tool_schemas(names: Iterable[str] | None = None, include_vision: bool = True) -> list[dict]:
    selected = list(names) if names is not None else list(TOOLS)
    return [
        TOOLS[n].schema()
        for n in selected
        if n in TOOLS and (include_vision or not TOOLS[n].vision_only)
    ]


def get_tool(name: str) -> Tool | None:
    return TOOLS.get(name)


async def execute_tool(
    name: str,
    args: dict[str, Any],
    ctx: ToolContext,
    allowed: Iterable[str] | None = None,
) -> ToolResult:
    tool = TOOLS.get(name)
    if tool is None:
        known = ", ".join(sorted(TOOLS))
        return ToolResult.error(f"unknown tool '{name}'. Available tools: {known}", name)
    if allowed is not None and name not in allowed:
        return ToolResult.error(f"tool '{name}' is not available in this context", name)

    # Drop unexpected keys so a hallucinated argument cannot raise TypeError.
    signature = inspect.signature(tool.handler)
    accepts_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    )
    if not accepts_kwargs:
        args = {k: v for k, v in args.items() if k in signature.parameters}

    try:
        result = await tool.handler(ctx, **args)
    except TypeError as e:
        return ToolResult.error(f"invalid arguments for '{name}': {e}", name)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(f"{name} failed: {type(e).__name__}: {e}", name)

    if not isinstance(result, ToolResult):
        result = ToolResult(output=str(result), title=name)
    if not result.title:
        result = ToolResult(output=result.output, is_error=result.is_error, title=name)
    return result


__all__ = [
    "Tool", "TOOLS", "ToolContext", "ToolResult",
    "tool_schemas", "get_tool", "execute_tool",
]
