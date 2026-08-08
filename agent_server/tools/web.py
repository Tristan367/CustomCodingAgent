"""Fetch a URL and convert it to readable text."""

import html
import re

import httpx

from agent_server.config import (
    MAX_TOOL_RESULT_CHARS,
    WEBFETCH_ALLOW_PRIVATE,
    WEBFETCH_MAX_BYTES,
    WEBFETCH_TIMEOUT,
)
from agent_server.tools.base import ToolContext, ToolResult, truncate

TIMEOUT = WEBFETCH_TIMEOUT
MAX_BYTES = WEBFETCH_MAX_BYTES


def _is_private(host: str) -> bool:
    """True for anything on this machine or the local network.

    The agent's own API is on localhost, so without this the model can reach
    round and drive this application through its own webfetch tool. Cloud
    metadata endpoints live on link-local addresses for the same reason.
    """
    import ipaddress
    import socket

    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # unresolvable; the request will fail on its own
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return True
    return False


async def webfetch(ctx: ToolContext, *, url: str, **_) -> ToolResult:
    title = f"fetch {url[:80]}"
    if not url.startswith(("http://", "https://")):
        return ToolResult.error(f"invalid URL (must be http/https): {url}", title)

    if not WEBFETCH_ALLOW_PRIVATE:
        from urllib.parse import urlparse

        if _is_private(urlparse(url).hostname or ""):
            return ToolResult.error(
                "refusing to fetch a local or private-network address. "
                "Set WEBFETCH_ALLOW_PRIVATE=1 if that is genuinely wanted.",
                title,
            )

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
