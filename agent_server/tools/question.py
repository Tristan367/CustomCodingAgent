from typing import Any

# Stub: this tool pauses execution and waits for user input via the UI.
# In practice, the chat loop will detect this tool call and yield a special event.


async def ask_question(*, question: str, options: list[str] | None = None, **kwargs: Any) -> str:
    return "[QUESTION_PENDING]"
