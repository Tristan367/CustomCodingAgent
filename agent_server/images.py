"""Image handling for uploads and captures.

Nothing here talks to a vision model. Describing an image is a custom tool --
a shell script the user supplies, because it needs a GPU or a paid account
this app cannot assume. What is left is decoding, downscaling and describing
the file itself, which uploads and `capture` both need regardless.
Vision: image normalisation, browser capture, and the Ollama vision client.

This replaces the previous dependency on an external VisionHelper package, which
used Playwright's synchronous API and therefore could never run inside the
server's event loop.

Two hard-won details about the Ollama backend, both verified against the live rig:

* It rejects WebP with *"Failed to load image or audio file"*. Uploads are
  therefore decoded with Pillow and re-encoded as PNG rather than trusting the
  filename, because browsers routinely hand over a WebP named ``.jpg``.
* Passing several images on a single ``/api/chat`` message does not work -- the
  model sees only one of them. Multiple images must each go on their own
  message, which is what makes comparison prompts reliable.
"""

import asyncio
import io
import logging
from pathlib import Path

import httpx

from agent_server.config import (
    VISION_MAX_PIXELS,
)

log = logging.getLogger(__name__)



class ImageError(RuntimeError):
    pass


# ── Image normalisation ─────────────────────────────────────────────────────

def normalize_image(data: bytes) -> bytes:
    """Decode any Pillow-supported image and re-encode as PNG.

    Handles the WebP problem, strips alpha (which some vision models mishandle),
    applies EXIF rotation so phone photos are upright, and caps the pixel count
    so a 48MP photo does not blow up the request.
    """
    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as e:
        raise ImageError(f"could not decode image: {e}") from e

    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "L"):
        if image.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            converted = image.convert("RGBA")
            background.paste(converted, mask=converted.split()[-1])
            image = background
        else:
            image = image.convert("RGB")

    pixels = image.width * image.height
    if pixels > VISION_MAX_PIXELS:
        scale = (VISION_MAX_PIXELS / pixels) ** 0.5
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def load_image(path: str | Path) -> bytes:
    p = Path(path).expanduser()
    if not p.exists():
        raise ImageError(f"image not found: {p}")
    if not p.is_file():
        raise ImageError(f"not a file: {p}")
    if p.stat().st_size > 60 * 1024 * 1024:
        raise ImageError(f"image too large: {p.stat().st_size:,} bytes")
    return normalize_image(p.read_bytes())


def describe_image_file(path: str | Path) -> str:
    """Human-readable dimensions, for tool output."""
    try:
        from PIL import Image

        with Image.open(Path(path).expanduser()) as im:
            return f"{im.width}x{im.height} {im.format}"
    except Exception:
        log.debug("reading image dimensions failed", exc_info=True)
        return "image"


# ── Ollama client ───────────────────────────────────────────────────────────



_starting = asyncio.Lock()
_analyze_lock = asyncio.Lock()




        # the rig may already be gone; nothing to clean up




_http: httpx.AsyncClient | None = None






async def normalize_in_thread(data: bytes) -> bytes:
    """Decode and re-encode off the event loop. A 48MP upload takes a while."""
    return await asyncio.to_thread(normalize_image, data)
