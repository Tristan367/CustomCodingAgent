"""Fetch a URL and convert it to readable text."""

import html
import re

import httpx

from agent_server.config import MAX_TOOL_RESULT_CHARS
from agent_server.tools.base import ToolContext, ToolResult, truncate

TIMEOUT = 30
MAX_BYTES = 5_000_000


async def webfetch(ctx: ToolContext, *, url: str, **_) -> ToolResult:
    title = f"fetch {url[:80]}"
    if not url.startswith(("http://", "https://")):
        return ToolResult.error(f"invalid URL (must be http/https): {url}", title)

    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; CodeAgent/1.0)",
                "Accept": "text/html,application/xhtml+xml,text/plain,*/*",
            },
        ) as client:
            resp = await client.get(url)
    except httpx.TimeoutException:
        return ToolResult.error(f"request timed out after {TIMEOUT}s: {url}", title)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(f"fetching {url}: {e}", title)

    if resp.status_code >= 400:
        return ToolResult.error(f"HTTP {resp.status_code} from {url}", title)

    content_type = resp.headers.get("content-type", "").lower()
    if len(resp.content) > MAX_BYTES:
        return ToolResult.error(f"response too large ({len(resp.content):,} bytes)", title)
    if not any(t in content_type for t in ("text/", "json", "xml", "javascript")):
        return ToolResult.error(f"unsupported content-type '{content_type}' for {url}", title)

    text = resp.text
    if "html" in content_type:
        text = _html_to_text(text)

    text = text.strip()
    if not text:
        return ToolResult(output=f"(empty response from {url}, HTTP {resp.status_code})", title=title)

    return ToolResult(
        output=truncate(text, MAX_TOOL_RESULT_CHARS, "page"),
        title=f"{title} ({len(text):,} chars)",
    )


def _html_to_text(raw: str) -> str:
    """Strip markup while keeping block structure and link targets."""
    text = re.sub(r"<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                  lambda m: f"{re.sub(r'<[^>]+>', '', m.group(2)).strip()} ({m.group(1)})",
                  text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</(p|div|section|article|li|tr|h[1-6]|pre|blockquote)>", "\n", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"<(br|hr)\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.IGNORECASE)
    text = re.sub(r"<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines())
