"""Vision: image normalisation, browser capture, and the Ollama vision client.

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
import base64
import io
import time
from pathlib import Path

import httpx

from agent_server.config import (
    VISION_MAX_PIXELS,
    VISION_MODEL,
    VISION_AUTOSTART,
    VISION_KEEP_ALIVE,
    VISION_REMOTE_BIN,
    VISION_SSH_HOST,
    VISION_START_TIMEOUT,
    VISION_NUM_CTX,
    VISION_OLLAMA_URL,
    VISION_TIMEOUT,
)

CAPTURE_DIR = Path("/tmp/codeagent_captures")
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

MAX_SEQUENCE_FRAMES = 12
DEFAULT_VIEWPORT = (1280, 900)


class VisionError(RuntimeError):
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
    except Exception as e:  # noqa: BLE001
        raise VisionError(f"could not decode image: {e}") from e

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
        raise VisionError(f"image not found: {p}")
    if not p.is_file():
        raise VisionError(f"not a file: {p}")
    if p.stat().st_size > 60 * 1024 * 1024:
        raise VisionError(f"image too large: {p.stat().st_size:,} bytes")
    return normalize_image(p.read_bytes())


def describe_image_file(path: str | Path) -> str:
    """Human-readable dimensions, for tool output."""
    try:
        from PIL import Image

        with Image.open(Path(path).expanduser()) as im:
            return f"{im.width}x{im.height} {im.format}"
    except Exception:  # noqa: BLE001
        return "image"


# ── Ollama client ───────────────────────────────────────────────────────────

async def rig_available() -> bool:
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.get(f"{VISION_OLLAMA_URL}/api/tags")
            return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


_starting = asyncio.Lock()


async def ensure_rig() -> tuple[bool, str]:
    """Make sure Ollama is up, starting it over SSH if it is not.

    The machine being on and the server running are two different things, and
    only the second one matters here. Returns (ready, note); `note` is non-empty
    when something had to be done, so the caller can say so.
    """
    if await rig_available():
        return True, ""
    if not (VISION_AUTOSTART and VISION_SSH_HOST):
        return False, ""

    # One attempt at a time: several images in one turn would otherwise each
    # try to start it.
    async with _starting:
        if await rig_available():
            return True, ""
        command = (
            f"setsid env OLLAMA_HOST=0.0.0.0:11434 nohup {VISION_REMOTE_BIN} serve "
            "> ~/ollama.log 2>&1 < /dev/null &"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                VISION_SSH_HOST, command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        except (asyncio.TimeoutError, OSError) as e:
            return False, f"could not reach {VISION_SSH_HOST} to start it ({e})"
        if proc.returncode != 0:
            detail = err.decode("utf-8", "replace").strip().splitlines()
            return False, (
                f"could not start it on {VISION_SSH_HOST}: "
                f"{detail[-1] if detail else f'ssh exit {proc.returncode}'}"
            )

        deadline = time.monotonic() + VISION_START_TIMEOUT
        while time.monotonic() < deadline:
            if await rig_available():
                return True, f"started the vision rig on {VISION_SSH_HOST}"
            await asyncio.sleep(1)
        return False, f"started it on {VISION_SSH_HOST} but it did not come up in time"


async def unload_model():
    """Drop the model from the rig's memory when this app shuts down."""
    try:
        client = await _client()
        await client.post(
            f"{VISION_OLLAMA_URL}/api/chat",
            json={"model": VISION_MODEL, "messages": [], "keep_alive": 0},
            timeout=8,
        )
    except Exception:  # noqa: BLE001
        pass  # the rig may already be gone; nothing to clean up


async def analyze(images: list[bytes], prompt: str, labels: list[str] | None = None) -> str:
    """Ask the vision model about one or more images.

    Each image goes on its own message; sending them together makes the model
    silently ignore all but one.

    The length of the answer is left to the caller's question. A specific
    question already gets a short answer; a vague one gets a long one, which is
    correct. Nothing is injected to make it terser -- that was measured to strip
    detail the question had actually asked for.
    """
    if not images:
        raise VisionError("no images to analyse")

    ready, note = await ensure_rig()
    if not ready:
        raise VisionError(
            f"the vision rig at {VISION_OLLAMA_URL} is not reachable"
            + (f" -- {note}" if note else ". Is vision-host switched on?")
        )

    labels = labels or [f"Image {i + 1}" for i in range(len(images))]
    messages: list[dict] = []

    if len(images) == 1:
        messages.append({
            "role": "user",
            "content": prompt,
            "images": [base64.b64encode(images[0]).decode()],
        })
    else:
        for i, raw in enumerate(images):
            label = labels[i] if i < len(labels) else f"Image {i + 1}"
            last = i == len(images) - 1
            text = f"This is {label}." if not last else (
                f"This is {label}.\n\nNow, considering all {len(images)} images in order "
                f"({', '.join(labels[:len(images)])}):\n{prompt}"
            )
            messages.append({
                "role": "user",
                "content": text,
                "images": [base64.b64encode(raw).decode()],
            })
            if not last:
                messages.append({"role": "assistant", "content": "Noted."})

    payload = {
        "model": VISION_MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": VISION_KEEP_ALIVE,
        # Left unset, Ollama falls back to a small default context and silently
        # truncates, which is easy to miss when comparing several images.
        "options": {"temperature": 0.1, "num_ctx": VISION_NUM_CTX},
    }

    try:
        client = await _client()
        resp = await client.post(f"{VISION_OLLAMA_URL}/api/chat", json=payload)
    except httpx.TimeoutException as e:
        raise VisionError(f"vision model timed out after {VISION_TIMEOUT}s") from e
    except Exception as e:  # noqa: BLE001
        raise VisionError(f"could not reach the vision rig at {VISION_OLLAMA_URL}: {e}") from e

    if resp.status_code != 200:
        raise VisionError(f"vision model returned HTTP {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    content = (body.get("message") or {}).get("content", "").strip()
    truncated = body.get("done_reason") == "length"
    if not content:
        # Checked before "empty": a cap tight enough to leave nothing at all
        # should say so, not report a mysterious empty reply.
        if truncated:
            raise VisionError(
                "ran out of context before producing an answer. "
                "Raise VISION_NUM_CTX or send fewer/smaller images."
            )
        raise VisionError("vision model returned an empty response")
    if truncated:
        # Only reachable by running out of context. Say so rather than handing
        # back a sentence that stops mid-word as though it were the answer.
        content += "\n\n[cut off: ran out of context. Ask about less at once.]"
    return content


_http: httpx.AsyncClient | None = None


async def _client() -> httpx.AsyncClient:
    """One connection pool for the process, rather than a fresh one per call."""
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=VISION_TIMEOUT)
    return _http


async def close_client():
    global _http
    if _http is not None and not _http.is_closed:
        await _http.aclose()
    _http = None


# ── Browser capture ─────────────────────────────────────────────────────────

async def capture(
    url: str,
    *,
    selector: str | None = None,
    full_page: bool = False,
    width: int = DEFAULT_VIEWPORT[0],
    height: int = DEFAULT_VIEWPORT[1],
    wait_for: str | None = None,
    delay_ms: int = 0,
    count: int = 1,
    interval_ms: int = 500,
    actions: list[dict] | None = None,
) -> list[tuple[str, bytes]]:
    """Screenshot a page, optionally as a timed sequence.

    Returns ``[(saved_path, png_bytes), ...]``. A sequence is useful for
    animations, loading states, and anything that changes over time.
    """
    from playwright.async_api import async_playwright

    count = max(1, min(int(count or 1), MAX_SEQUENCE_FRAMES))
    interval_ms = max(0, min(int(interval_ms or 0), 10_000))
    shots: list[tuple[str, bytes]] = []
    stamp = f"{int(time.time())}_{abs(hash(url)) % 10000:04d}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            page = await browser.new_page(viewport={"width": width, "height": height})
            await page.goto(url, wait_until="networkidle", timeout=30_000)

            if wait_for:
                await page.wait_for_selector(wait_for, timeout=15_000)
            for action in actions or []:
                await _perform(page, action)
            if delay_ms:
                await page.wait_for_timeout(min(delay_ms, 15_000))

            target = page
            if selector:
                element = await page.wait_for_selector(selector, timeout=15_000)
                if element is None:
                    raise VisionError(f"selector not found: {selector}")
                target = element

            for i in range(count):
                if i:
                    await page.wait_for_timeout(interval_ms)
                kwargs = {"full_page": full_page} if target is page else {}
                data = await target.screenshot(**kwargs)
                name = f"{stamp}_{i:02d}.png" if count > 1 else f"{stamp}.png"
                path = CAPTURE_DIR / name
                path.write_bytes(data)
                shots.append((str(path), data))
        finally:
            await browser.close()

    return shots


async def _perform(page, action: dict):
    """Run one pre-capture interaction step."""
    kind = (action.get("type") or "").lower()
    selector = action.get("selector") or ""
    value = action.get("value") or ""
    try:
        if kind == "click":
            await page.click(selector, timeout=10_000)
        elif kind == "fill":
            await page.fill(selector, value, timeout=10_000)
        elif kind == "press":
            await page.press(selector or "body", value or "Enter", timeout=10_000)
        elif kind == "hover":
            await page.hover(selector, timeout=10_000)
        elif kind == "scroll":
            await page.evaluate(f"window.scrollBy(0, {int(value or 400)})")
        elif kind == "wait":
            await page.wait_for_timeout(min(int(value or 500), 15_000))
        else:
            raise VisionError(f"unknown action type: {kind}")
    except Exception as e:  # noqa: BLE001
        raise VisionError(f"action {kind}({selector}) failed: {e}") from e


async def capture_in_thread(*args, **kwargs):
    """Kept for callers that expect a blocking-safe wrapper."""
    return await capture(*args, **kwargs)


async def normalize_in_thread(data: bytes) -> bytes:
    return await asyncio.to_thread(normalize_image, data)
