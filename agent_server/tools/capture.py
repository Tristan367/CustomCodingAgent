"""The `capture` tool: screenshot the desktop.

For anything Playwright cannot drive -- a native game, a desktop app, an
emulator. Web pages go through `browser`, which can also interact with them.
The frames come from `agent_server.capture` and their paths are returned; what
the agent does with an image after that is not this tool's business.
"""

from agent_server.tools.base import ToolContext, ToolResult


async def capture(
    ctx: ToolContext,
    *,
    region: str = "",
    count: int = 1,
    interval_ms: int = 400,
    **_,
) -> ToolResult:
    """Screenshot the desktop and return the paths of the frames saved."""
    from agent_server import capture as screen

    del ctx  # the desktop is not per-session
    try:
        paths = await screen.grab(region, count=count, interval_ms=interval_ms)
    except screen.CaptureError as e:
        return ToolResult.error(str(e), "capture")

    plural = "s" if len(paths) != 1 else ""
    listing = "\n".join(f"  {i + 1}. {p}" for i, p in enumerate(paths))
    return ToolResult(
        output=f"Captured {len(paths)} frame{plural}:\n{listing}",
        title=f"{len(paths)} frame{plural}",
    )
