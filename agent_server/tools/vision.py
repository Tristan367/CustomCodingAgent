"""Vision tools: look at images, and capture web pages to look at."""

from pathlib import Path

from agent_server import vision as engine
from agent_server.tools.base import ToolContext, ToolResult

MAX_IMAGES = 6

DEFAULT_SEQUENCE_PROMPT = (
    "These frames were captured in order. Describe what changes between them, "
    "and note anything that looks broken or stuck."
)
# Only used when the caller asked nothing at all. "Describe in detail" invites
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
    url: str | None = None,
    selector: str | None = None,
    full_page: bool = False,
    width: int = 1280,
    height: int = 900,
    **_,
) -> ToolResult:
    """Analyse local image files and/or a freshly captured web page."""
    images: list[bytes] = []
    labels: list[str] = []
    title_bits: list[str] = []

    if isinstance(paths, str):
        paths = [paths]
    paths = [p for p in (paths or []) if p]

    if not paths and not url:
        return ToolResult.error(
            "give either `paths` (image files to look at) or `url` (a page to capture)",
            "vision",
        )
    if len(paths) > MAX_IMAGES:
        return ToolResult.error(f"at most {MAX_IMAGES} images per call", "vision")

    for raw in paths:
        path = ctx.resolve(raw)
        try:
            images.append(engine.load_image(path))
        except engine.VisionError as e:
            return ToolResult.error(str(e), "vision")
        labels.append(path.name)
        title_bits.append(f"{path.name} ({engine.describe_image_file(path)})")

    if url:
        try:
            shots = await engine.capture(
                url, selector=selector, full_page=full_page, width=width, height=height
            )
        except Exception as e:
            return ToolResult.error(f"could not capture {url}: {e}", "vision")
        for saved, data in shots:
            images.append(data)
            labels.append(Path(saved).name)
        title_bits.append(url)

    try:
        answer = await engine.analyze(images, prompt or DEFAULT_PROMPT, labels)
    except engine.VisionError as e:
        return ToolResult.error(str(e), "vision")

    header = f"Looked at {len(images)} image{'s' if len(images) != 1 else ''}: " + ", ".join(labels)
    return ToolResult(
        output=f"{header}\n\n{answer}",
        title=f"vision: {', '.join(title_bits)[:90]}",
    )


async def screenshot(
    ctx: ToolContext,
    *,
    url: str,
    selector: str | None = None,
    full_page: bool = False,
    width: int = 1280,
    height: int = 900,
    wait_for: str | None = None,
    delay_ms: int = 0,
    count: int = 1,
    interval_ms: int = 500,
    actions: list[dict] | None = None,
    prompt: str | None = None,
    analyze: bool = True,
    **_,
) -> ToolResult:
    """Capture a page (optionally a timed sequence) and optionally analyse it."""
    if not url:
        return ToolResult.error("`url` is required", "screenshot")

    try:
        shots = await engine.capture(
            url,
            selector=selector,
            full_page=full_page,
            width=width,
            height=height,
            wait_for=wait_for,
            delay_ms=delay_ms,
            count=count,
            interval_ms=interval_ms,
            actions=actions,
        )
    except Exception as e:
        return ToolResult.error(f"capture failed: {e}", f"screenshot {url[:60]}")

    listing = "\n".join(f"  {i + 1}. {path}" for i, (path, _) in enumerate(shots))
    body = f"Captured {len(shots)} screenshot{'s' if len(shots) != 1 else ''} of {url}:\n{listing}"
    title = f"screenshot {url[:60]} ({len(shots)} frame{'s' if len(shots) != 1 else ''})"

    if not analyze:
        body += "\n\nNot analysed. Pass these paths to `vision` when you want them read."
        return ToolResult(output=body, title=title)

    # Analysing is the default. Returning paths and asking the model to call
    # `vision` itself cost a whole extra round trip to learn what it had just
    # captured, which is the only reason to take a screenshot at all.
    question = prompt or (
        DEFAULT_SEQUENCE_PROMPT if len(shots) > 1 else DEFAULT_PROMPT
    )
    try:
        answer = await engine.analyze(
            [data for _, data in shots[:MAX_IMAGES]],
            question,
            [Path(p).name for p, _ in shots[:MAX_IMAGES]],
        )
        body += f"\n\n{answer}"
    except engine.VisionError as e:
        body += (
            f"\n\n(could not analyse: {e})"
            "\nThe files above were still captured; retry `vision` on them once the "
            "rig is reachable."
        )

    return ToolResult(output=body, title=title)
