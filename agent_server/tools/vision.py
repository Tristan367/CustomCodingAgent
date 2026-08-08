"""Vision tool — screenshot URLs and analyze with local Ollama vision model."""

import sys
import os

VISION_HELPER_PATH = "/home/tristan/Projects/VisionHelper"


async def vision(
    *,
    url: str,
    prompt: str | None = None,
    selector: str | None = None,
    width: int = 1280,
    height: int = 900,
) -> str:
    """Screenshot a URL and analyze it with the vision model.

    Use this to visually inspect web UIs — check layouts, verify styling,
    confirm interactive elements render correctly.
    """
    sys.path.insert(0, VISION_HELPER_PATH)
    try:
        from core import capture_screenshot, analyze as vision_analyze, DEFAULT_PROMPT

        screenshot_bytes = capture_screenshot(
            url=url,
            selector=selector,
            crop=False,
            width=width,
            height=height,
        )

        result = vision_analyze(
            screenshot_bytes,
            prompt=prompt or DEFAULT_PROMPT,
        )
        return result
    except Exception as e:
        return f"Vision tool error: {e}"


async def analyze_user_image(
    *,
    image_path: str,
    prompt: str | None = None,
) -> str:
    """Analyze a user-uploaded image file with the vision model."""
    sys.path.insert(0, VISION_HELPER_PATH)
    try:
        from core import analyze_image_file, DEFAULT_PROMPT

        result = analyze_image_file(
            image_path=image_path,
            prompt=prompt or "Describe this image in detail.",
        )
        return result
    except Exception as e:
        return f"Vision tool error: {e}"
