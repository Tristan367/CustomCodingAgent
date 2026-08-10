"""Tool registry: schemas, dispatch, and permission policy."""

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_server.tools.base import ToolContext, ToolResult
from agent_server.tools.bash import run_bash
from agent_server.tools.browser import browser as browser_tool
from agent_server.tools.capture import capture
from agent_server.tools.file_ops import edit_file, read_file, write_file
from agent_server.tools.search import glob_search, grep_search
from agent_server.tools.skill import load_skill
from agent_server.tools.task import run_task
from agent_server.tools.web import webfetch, websearch

Handler = Callable[..., Awaitable[ToolResult]]
PauseKind = Literal["permission"]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler
    # "permission": run only after the user approves.
    pause: PauseKind | None = None
    # Read-only and side-effect free, so several may run at once. This is a
    # property of the tool rather than of its name: a custom tool is free to
    # call itself `read`, and shadowing a built-in must not inherit the
    # built-in's right to skip the permission gate and run concurrently.
    parallel_safe: bool = field(default=False)
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


log = logging.getLogger(__name__)

TOOLS: dict[str, Tool] = {}
_custom_tool_names: set[str] = set()
# Filled in once every built-in has been registered, at the bottom of this
# module. A custom tool may not take one of these names: registration is a
# plain dict assignment, so shadowing replaced the built-in outright, and
# deleting the custom tool then removed the built-in with it for the life of
# the process. A shadowing tool also inherited nothing of the built-in's
# safety properties while inheriting its name in every policy check.
BUILT_IN_NAMES: frozenset[str] = frozenset()


def register(tool: Tool):
    TOOLS[tool.name] = tool


def register_custom(tool: Tool) -> str:
    """Register a user-defined tool. Returns an error message, or "" on success."""
    if tool.name in BUILT_IN_NAMES:
        return (
            f"'{tool.name}' is the name of a built-in tool. "
            "Rename the custom tool -- it cannot replace one."
        )
    TOOLS[tool.name] = tool
    _custom_tool_names.add(tool.name)
    return ""


def unregister_custom(names: set[str]):
    for name in names:
        if name in BUILT_IN_NAMES:
            continue
        TOOLS.pop(name, None)
    _custom_tool_names.difference_update(names)


register(Tool(
    name="read",
    description=(
        "Read a file or directory from the filesystem. Prints a `[path#tag]` header, "
        "then lines as `N: text` with N the 1-indexed line number. Pass that tag and "
        "the line numbers to `edit` to change lines without retyping them. The tag "
        "fingerprints the whole file, so it proves nothing moved underneath you.\n"
        "Only lines shown here may be edited; use offset/limit to reach the rest. "
        "Prefer absolute paths. You must read a file before you edit it."
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
    parallel_safe=True,
))

register(Tool(
    name="edit",
    description=(
        "Apply changes to an existing file. Prefer the tagged-line mode: call `read` "
        "first, then pass the tag it printed plus startLine/endLine, with the "
        "replacement in newText.\n"
        "`read` prints a header like `[src/app.py#a3f9]` above the lines, and lines "
        "as `42: return x`. So startLine 42, tag a3f9.\n"
        "The tag fingerprints the whole file, so it changes whenever anything in the "
        "file changes. That is the point: if your tag is stale the file moved under "
        "you and your line numbers may name different code. NEVER invent, guess or "
        "adjust a tag -- copy the one you were given.\n"
        "You can only edit lines `read` actually displayed. Re-read with an offset to "
        "reach lines you have not seen.\n"
        "Each successful edit returns the file's new tag, so consecutive edits need "
        "no re-read.\n"
        "Fallback: oldString/newString for exact text replacement."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filePath": {"type": "string", "description": "Path to the file"},
            "tag": {"type": "string", "description": "4-char tag from the [path#tag] header of your `read`"},
            "startLine": {"type": "integer", "description": "1-indexed first line to replace"},
            "endLine": {"type": "integer", "description": "1-indexed last line to replace (omit for a single line)"},
            "newText": {"type": "string", "description": "Replacement lines (omit to delete the range)"},
            "oldString": {"type": "string", "description": "Exact text to replace (fallback)"},
            "newString": {"type": "string", "description": "Replacement text for oldString mode"},
            "replaceAll": {"type": "boolean", "description": "Replace every occurrence"},
        },
        "required": ["filePath"],
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
    parallel_safe=True,
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
    parallel_safe=True,
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
    parallel_safe=True,
))

register(Tool(
    name="websearch",
    description=(
        "Search the web via DuckDuckGo. Returns titles, snippets, and URLs for up to "
        "10 results. No API key required. Use this when you need current information "
        "not in the codebase — library docs, version changes, error messages."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query"}},
        "required": ["query"],
    },
    handler=websearch,
    parallel_safe=True,
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
            "count": {
                "type": "integer",
                "description": "Number of parallel subagents to launch with the same prompt. Default 1. Use for consensus/exploration.",
            },
        },
        "required": ["description", "prompt"],
    },
    handler=run_task,
    parallel_safe=True,
))

register(Tool(
    name="skill",
    description=(
        "Load reusable Markdown instructions for a technology, framework, or workflow. "
        "Call without `name` to list available skills. Call with `name` to load one. "
        "Skills live in ~/.config/codeagent/skills/ as .md files. Use this instead of "
        "guessing at API signatures or conventions you might misremember."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill filename without .md extension, or leave blank to list"},
        },
        "required": [],
    },
    handler=load_skill,
    parallel_safe=True,
))

register(Tool(
    name="explore",
    description=(
        "Dispatch a narrow subagent to search the codebase for specific facts — "
        "file locations, class definitions, call sites, config patterns. Read-only, "
        "lighter than `task`. Give it a focused question with concrete expected output. "
        "Use `task` for open-ended research; use `explore` when you know exactly what "
        "you need to find."
    ),
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "3-5 word label"},
            "prompt": {
                "type": "string",
                "description": "Specific question with expected output format",
            },
        },
        "required": ["description", "prompt"],
    },
    handler=run_task,
    parallel_safe=True,
))

register(Tool(
    name="capture",
    description=(
        "Screenshot the desktop -- for anything that is not a web page: a native "
        "game, a desktop app, an emulator, a terminal. Use `browser` for web pages; "
        "it can interact with them, and this cannot.\n"
        "Pass `prompt` to have the capture described in the same call, or omit it to "
        "just save the frames and ask about them later with `vision`. `count` with "
        "`interval_ms` records a burst, which is how you inspect an animation or "
        "watch something change. `region` is 'x,y,w,h' if you only want part of the "
        "screen.\n"
        "This needs a screen-capture tool installed; if none is found the error says "
        "which to install for this machine."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "What to find out. Omit to save the frames without describing them.",
            },
            "region": {"type": "string", "description": "'x,y,w,h' to capture part of the screen"},
            "count": {"type": "integer", "description": "Number of frames, 1-24 (default 1)"},
            "interval_ms": {"type": "integer", "description": "Gap between frames (default 400)"},
        },
        "required": [],
    },
    handler=capture,
    parallel_safe=True,
    vision_only=True,
))

register(Tool(
    name="browser",
    description=(
        "Drive a real browser to build, inspect and TEST a web UI. Give it a list of "
        "`steps`; each is reported with its outcome. The browser is kept between calls "
        "in this session, so a login persists and a long flow can be split across "
        "several calls.\n"
        "\n"
        "Start with `snapshot`. It returns the page's accessibility tree, which tells "
        "you exactly what is on the page and what to address it as -- do not guess CSS "
        "selectors. `at` accepts Playwright locators, and the robust ones come straight "
        "off the snapshot:\n"
        "  role=button[name=\"Sign in\"]   text=Save   label=Email   placeholder=Search\n"
        "  #id   .class   css=div > p        (CSS also works, but breaks more easily)\n"
        "\n"
        "Actions: goto(url) click fill(text) press(key) hover select(value) check "
        "uncheck upload(paths) scroll(to) wait(ms|at|until) back forward reload "
        "resize(width,height) snapshot eval(js) shoot record expect.\n"
        "\n"
        "`expect` is an assertion and fails the call if it does not hold -- use it "
        "instead of claiming something works. One of: visible, hidden, text, url, "
        "count, console_clean.\n"
        "\n"
        "`shoot` saves a screenshot and returns its path; add `ask` to have it "
        "described in the same call. `record` takes a burst of frames, which is how "
        "you inspect an animation or a loading state. Console errors, page exceptions "
        "and failed requests are always captured and reported against the step that "
        "caused them -- check them before concluding a click did nothing.\n"
        "\n"
        "On failure it stops and reports the console, the accessibility tree and a "
        "screenshot, so you should not need a second call to find out why.\n"
        "\n"
        "Example: [{\"action\":\"goto\",\"url\":\"http://localhost:3000\"},"
        "{\"action\":\"snapshot\"},"
        "{\"action\":\"fill\",\"at\":\"label=Email\",\"text\":\"a@b.c\"},"
        "{\"action\":\"click\",\"at\":\"text=Sign in\"},"
        "{\"action\":\"expect\",\"visible\":\"text=Dashboard\"},"
        "{\"action\":\"expect\",\"console_clean\":true},"
        "{\"action\":\"shoot\",\"ask\":\"is the sidebar aligned with the header?\"}]\n"
        "\n"
        "For a permanent regression test, write a Playwright spec file and run it with "
        "`bash`. This tool is for exploring and verifying as you work."
    ),
    parameters={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": "Ordered actions to run.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "goto", "click", "fill", "press", "hover", "select",
                                "check", "uncheck", "upload", "scroll", "wait",
                                "back", "forward", "reload", "resize",
                                "snapshot", "eval", "shoot", "record", "expect",
                            ],
                        },
                        "at": {
                            "type": "string",
                            "description": "What to act on. Prefer role=/text=/label= over CSS.",
                        },
                        "url": {"type": "string", "description": "For goto, or expect url"},
                        "text": {"type": "string", "description": "For fill, or expect text"},
                        "key": {"type": "string", "description": "For press, e.g. Enter"},
                        "value": {"type": "string", "description": "For select"},
                        "js": {"type": "string", "description": "For eval"},
                        "ask": {
                            "type": "string",
                            "description": "On shoot/record: have the frames described, "
                                           "answering this question.",
                        },
                        "compare": {
                            "type": "array", "items": {"type": "string"},
                            "description": "On shoot/record with `ask`: image files to put "
                                           "beside the new frames, so one question spans "
                                           "both. This is how you check a page against a "
                                           "mockup, or against an earlier capture.",
                        },
                        "visible": {"type": "string", "description": "expect: must be visible"},
                        "hidden": {"type": "string", "description": "expect: must be gone"},
                        "count": {
                            "type": "integer",
                            "description": "expect: how many `at` should match; "
                                           "on record: how many frames",
                        },
                        "console_clean": {
                            "type": "boolean",
                            "description": "expect: no console errors or failed requests",
                        },
                        "full_page": {"type": "boolean", "description": "shoot: whole scrollable page"},
                        "paths": {
                            "type": "array", "items": {"type": "string"},
                            "description": "For upload",
                        },
                        "ms": {"type": "integer", "description": "For wait"},
                        "interval_ms": {"type": "integer", "description": "For record"},
                        "to": {"type": "string", "description": "For scroll: top, bottom, or pixels"},
                        "state": {"type": "string", "description": "For wait on `at`: visible|hidden|attached"},
                        "until": {"type": "string", "description": "For wait: load|domcontentloaded|networkidle"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "timeout_ms": {"type": "integer", "description": "Override the 10s default"},
                    },
                    "required": ["action"],
                },
            },
            "width": {"type": "integer", "description": "Viewport width (default 1280)"},
            "height": {"type": "integer", "description": "Viewport height (default 900)"},
            "stop_on_error": {
                "type": "boolean",
                "description": "Stop at the first failed step. Default true.",
            },
            "reset": {
                "type": "boolean",
                "description": "Throw away cookies and history and start a clean browser first.",
            },
        },
        "required": ["steps"],
    },
    handler=browser_tool,
    vision_only=True,
))


# Every name registered above. Anything added after this point is a custom
# tool and is held to register_custom's rules.
BUILT_IN_NAMES = frozenset(TOOLS)


def tool_schemas(names: Iterable[str] | None = None, include_vision: bool = True, exclude: set[str] | None = None) -> list[dict]:
    selected = list(names) if names is not None else list(TOOLS)
    return [
        TOOLS[n].schema()
        for n in selected
        if n in TOOLS
        and (include_vision or not TOOLS[n].vision_only)
        and (exclude is None or n not in exclude)
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
    except Exception as e:
        return ToolResult.error(f"{name} failed: {type(e).__name__}: {e}", name)

    if not isinstance(result, ToolResult):
        result = ToolResult(output=str(result), title=name)
    if not result.title:
        result = ToolResult(output=result.output, is_error=result.is_error, title=name)
    if result.is_error:
        log.warning("tool %s failed: %s", name, result.output[:200])
    return result


__all__ = [
    "TOOLS",
    "Tool",
    "ToolContext",
    "ToolResult",
    "execute_tool",
    "get_tool",
    "tool_schemas",
]
