"""Ask the user a question mid-run.

This tool never computes anything. The agent loop recognises it as interactive,
pauses, and the answer submitted from the UI becomes the tool result.
"""

from agent_server.tools.base import ToolContext, ToolResult


async def ask_question(
    ctx: ToolContext,
    *,
    question: str,
    options: list[str] | None = None,
    **_,
) -> ToolResult:
    # Only reached if something bypasses the interactive pause.
    return ToolResult.error("question tool requires user interaction", "question")


def format_prompt(question: str, options: list[str] | None) -> str:
    text = question
    if options:
        text += "\n" + "\n".join(f"  {i + 1}. {o}" for i, o in enumerate(options))
    return text
