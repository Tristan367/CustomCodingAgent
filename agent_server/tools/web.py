import httpx

FETCH_TIMEOUT = 30


async def webfetch(*, url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return f"Error: invalid URL: {url}"
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "CodingAgent/1.0"})
            content_type = resp.headers.get("content-type", "")
            text = resp.text
            if "text/html" in content_type:
                # Crude HTML-to-text: strip tags
                import re
                text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text)
            return text[:50000] if text.strip() else f"Fetched {url} ({resp.status_code}) - empty or non-text content"
    except httpx.TimeoutException:
        return f"Error: request timed out for {url}"
    except Exception as e:
        return f"Error fetching {url}: {e}"
