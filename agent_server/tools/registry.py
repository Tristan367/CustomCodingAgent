from typing import Any
from agent_server.tools.file_ops import read_file, edit_file, write_file
from agent_server.tools.bash import run_bash
from agent_server.tools.search import grep_search, glob_search
from agent_server.tools.web import webfetch
from agent_server.tools.question import ask_question
from agent_server.tools.vision import vision
from agent_server.tools.task import run_task

VISION_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "vision",
        "description": "Screenshot a web URL and analyze it visually using a vision model. Use this to verify UI changes, check layouts, inspect rendering. The vision model will describe what it sees.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to screenshot (http:// or file://)"},
                "prompt": {"type": "string", "description": "What to ask the vision model about the page"},
                "selector": {"type": "string", "description": "Optional CSS selector to focus on"},
                "width": {"type": "integer", "description": "Viewport width (default 1280)"},
                "height": {"type": "integer", "description": "Viewport height (default 900)"},
            },
            "required": ["url"],
        },
    },
}

BASE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file from the local filesystem. Returns contents with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Absolute path to the file"},
                    "offset": {"type": "integer", "description": "Line number to start from (1-indexed)"},
                    "limit": {"type": "integer", "description": "Max lines to read (default 2000)"},
                },
                "required": ["filePath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Performs exact string replacements in an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Absolute path to the file"},
                    "oldString": {"type": "string", "description": "The text to replace"},
                    "newString": {"type": "string", "description": "The replacement text"},
                    "replaceAll": {"type": "boolean", "description": "Replace all occurrences (default false)"},
                },
                "required": ["filePath", "oldString", "newString"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Create a new file or overwrite an existing one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Absolute path for the file"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["filePath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command in the project directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in milliseconds"},
                    "workdir": {"type": "string", "description": "Working directory for the command"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents using regex. Returns file paths and line numbers with matching lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "The regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory to search in (defaults to cwd)"},
                    "include": {"type": "string", "description": "File pattern filter (e.g. '*.py')"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. 'src/**/*.py')"},
                    "path": {"type": "string", "description": "Directory to search in (defaults to cwd)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "webfetch",
            "description": "Fetch content from a URL and return as markdown/text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "question",
            "description": "Ask the user a question during execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question text"},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "Optional choices"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task",
            "description": "Launch a subagent to handle a complex task autonomously. The subagent runs in a fresh session with access to tools. Use this to delegate research, code exploration, or multi-step work to a focused agent. You can launch multiple task calls in parallel for independent work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Short (3-5 word) description of the task"},
                    "prompt": {"type": "string", "description": "Detailed task for the subagent. Include exactly what to do and what to return."},
                },
                "required": ["description", "prompt"],
            },
        },
    },
]

TOOL_HANDLERS: dict[str, Any] = {
    "read": read_file,
    "edit": edit_file,
    "write": write_file,
    "bash": run_bash,
    "grep": grep_search,
    "glob": glob_search,
    "webfetch": webfetch,
    "question": ask_question,
    "vision": vision,
    "task": run_task,
}


def get_tool_definitions(include_vision: bool = True) -> list[dict]:
    tools = list(BASE_TOOLS)
    if include_vision:
        tools.append(VISION_TOOL_DEF)
    return tools


def get_tool_handler(name: str):
    return TOOL_HANDLERS.get(name)
