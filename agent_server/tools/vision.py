"""The `vision` tool: look at images on disk.

Capture is somebody else's job -- `browser` for a web page, `capture` for
anything that is not one. This only ever reads files, which is what makes a
before/after comparison work: capture, act, capture, then pass both paths here
and ask what changed.
"""


from agent_server import vision as engine
from agent_server.tools.base import ToolContext, ToolResult

MAX_IMAGES = 6

# Used only when the caller asked nothing at all. "Describe in detail" invites
# a 4,000 character report that takes a minute to generate; naming what is
# wanted gets the same useful content in a fraction of the time. A prompt the
# caller supplies is passed through untouched.
DEFAULT_PROMPT = (
    "What is on screen? Cover the main sections, the visible text, and anything "
    "that looks broken, misaligned, or out of place."
)


async def vision(
    ctx: ToolContext,
    *,
    prompt: str | None = None,
    paths: list[str] | str | None = None,
    **_,
) -> ToolResult:
    if isinstance(paths, str):
        paths = [paths]
    paths = [p for p in (paths or []) if p]

    if not paths:
        return ToolResult.error(
            "`paths` is required: the image files to look at. To capture a web "
            "page first use `browser` with a `shoot` step; for anything else use "
            "`capture`.",
            "vision",
        )
    if len(paths) > MAX_IMAGES:
        return ToolResult.error(
            f"at most {MAX_IMAGES} images per call, got {len(paths)}", "vision"
        )

    images: list[bytes] = []
    labels: list[str] = []
    described: list[str] = []
    for raw in paths:
        path = ctx.resolve(raw)
        try:
            images.append(engine.load_image(path))
        except engine.VisionError as e:
            return ToolResult.error(str(e), "vision")
        labels.append(path.name)
        described.append(f"{path.name} ({engine.describe_image_file(path)})")

    try:
        answer = await engine.analyze(images, prompt or DEFAULT_PROMPT, labels)
    except engine.VisionError as e:
        return ToolResult.error(str(e), "vision")

    plural = "s" if len(images) != 1 else ""
    header = f"Looked at {len(images)} image{plural}: " + ", ".join(labels)
    return ToolResult(
        output=f"{header}\n\n{answer}",
        title=f"vision: {', '.join(described)[:90]}",
    )


async def capture(
    ctx: ToolContext,
    *,
    prompt: str | None = None,
    region: str = "",
    count: int = 1,
    interval_ms: int = 400,
    **_,
) -> ToolResult:
    """Screenshot the desktop and, unless told otherwise, describe it.

    For anything Playwright cannot drive: a native game, a desktop app, an
    emulator. Web pages should go through `browser`, which can also interact
    with them.
    """
    from agent_server import capture as screen

    try:
        paths = await screen.grab(region, count=count, interval_ms=interval_ms)
    except screen.CaptureError as e:
        return ToolResult.error(str(e), "capture")

    plural = "s" if len(paths) != 1 else ""
    listing = "\n".join(f"  {i + 1}. {p}" for i, p in enumerate(paths))
    body = f"Captured {len(paths)} frame{plural}:\n{listing}"

    if prompt is None:
        return ToolResult(
            output=body + "\n\nNot described. Pass these paths to `vision` to ask about them.",
            title=f"capture ({len(paths)} frame{plural})",
        )

    # Dispatched by name so a `vision` supplied as a custom tool answers here
    # too. Describing an image needs hardware or an account this app cannot
    # assume every install has, so it does not ship one.
    from agent_server.tools.registry import TOOLS, execute_tool

    if "vision" not in TOOLS:
        body += (
            "\n\nNot described: no `vision` tool is installed. "
            "Add one on the Tools page, or ask about these paths another way."
        )
        return ToolResult(output=body, title=f"capture ({len(paths)} frame{plural})")

    result = await execute_tool(
        "vision", {"prompt": prompt or DEFAULT_PROMPT, "paths": paths[:MAX_IMAGES]}, ctx
    )
    if result.is_error:
        body += (
            f"\n\n(could not describe it: {result.output})\n"
            "The frames above were still saved; retry `vision` on them."
        )
    else:
        body += f"\n\n{result.output}"
    return ToolResult(output=body, title=f"capture ({len(paths)} frame{plural})")
