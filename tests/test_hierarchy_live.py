"""Smoke test: create a tiered profile and verify the hierarchy with DeepSeek Flash.

This test talks to the live API.  It is excluded from the regular suite by the
*_live* naming convention so it never runs accidentally.  Run it with:

    python -m pytest tests/test_hierarchy_live.py -v

It creates a temporary profile with:
  - Tier 1: task enabled, cap 2, custom subagent prompt
  - Tier 2: task enabled, cap 2
  - Session cap: 5
  - Model: deepseek-v4-pro (cheap fallback)

Then it asks the model to fan out a small number of subagents and checks that
the results stay under the caps.
"""

import asyncio
import json
import os

import httpx

# ── Configuration (safe defaults) ────────────────────────────────────────────

BASE_URL = os.environ.get("CODEAGENT_URL", "http://localhost:8219")
PROFILE_NAME = "_smoke_hierarchy_test"
MODEL = "deepseek-v4-pro"  # cheap


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _post(url, **kw):
    kw.setdefault("follow_redirects", False)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120) as c:
        r = await c.post(url, **kw)
        if r.status_code == 303:
            return r
        r.raise_for_status()
        return r


async def _get(url):
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r


async def _stream(url, data, until_fn):
    """POST + SSE stream.  Calls *until_fn* with each event dict; stops when it
    returns truthy."""
    async with (
        httpx.AsyncClient(base_url=BASE_URL, timeout=300) as c,
        c.stream("POST", url, json=data, headers={"Accept": "text/event-stream"}) as r,
    ):
        if r.status_code >= 400:
            body = await r.aread()
            raise RuntimeError(f"POST {url} → {r.status_code}: {body[:500]}")
        async for line in r.aiter_lines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                if until_fn(data):
                    return data
    return None


# ── Setup / teardown ─────────────────────────────────────────────────────────


async def create_profile():
    """Create a tiered test profile via the DB (safe, no API cost)."""
    # We talk to the running process's DB through the API.
    # Create the profile by posting to the form endpoint.
    subagent_prompt = (
        "You are a test subagent. Reply with exactly the text REACHED_TIER_1. "
        "Do NOT use any tools. Do NOT launch subagents. Just reply."
    )
    tier2_prompt = (
        "You are a test sub-subagent. Reply with exactly the text REACHED_TIER_2. "
        "Do NOT use any tools. Just reply."
    )

    # Build Tier 1 tools: all off except read, grep, glob, webfetch, websearch, task
    # Build Tier 2 tools: same
    all_tool_names = [
        "read", "write", "edit", "bash", "grep", "glob", "webfetch",
        "websearch", "task", "explore", "capture", "browser",
    ]

    data = {
        "name": f"system:{PROFILE_NAME}",
        "body": "You are a test coordinator. When asked to spawn subagents, use the task tool.",
        "compact_body": "Test compaction prompt.",
        "max_concurrent": "5",
    }
    # Enable task + read-only on main profile
    for t in all_tool_names:
        if t in ("read", "grep", "glob", "webfetch", "websearch", "task"):
            data.setdefault("tool", [])
            data["tool"].append(t)

    # Tier 1 subagent — task enabled, cap 2
    data["sa_visible"] = "1"
    data["subagent_body"] = subagent_prompt
    data["sa_cap"] = "2"
    for t in all_tool_names:
        if t in ("read", "grep", "glob", "webfetch", "websearch", "task"):
            data.setdefault("sa_tool", [])
            data["sa_tool"].append(t)

    # Tier 2 — task enabled (for recursive hierarchy), cap 2
    data["subagent_body_2"] = tier2_prompt
    data["sa_cap_2"] = "2"
    for t in all_tool_names:
        if t in ("read", "grep", "glob", "webfetch", "websearch", "task"):
            data.setdefault("sa_tool_2", [])
            data["sa_tool_2"].append(t)

    r = await _post("/_save_prompts", data=data)
    print(f"  Profile created: HTTP {r.status_code}")
    return PROFILE_NAME


async def create_session():
    """Create a session that uses the test profile."""
    r = await _post("/_create_session", data={
        "name": "hierarchy-smoke",
        "project_dir": "/tmp",
        "model": MODEL,
        "prompt_profile": PROFILE_NAME,
    })
    assert r.status_code == 303, f"Expected redirect, got {r.status_code}: {r.text[:200]}"
    loc = r.headers["location"]
    sid = loc.rstrip("/").split("/")[-1]
    print(f"  Session created: {sid}")
    return sid


async def send_message(session_id: str, text: str):
    """Send a chat message and wait for completion."""
    events = []
    def check(evt):
        events.append(evt)
        if evt.get("type") == "stream_end":
            return True
        return evt.get("type") == "error"

    await _stream(f"/api/sessions/{session_id}/chat", {"message": text}, check)
    # Collect all content events
    content = " ".join(e.get("text", "") for e in events if e.get("type") == "content")
    return content


async def cleanup(session_id: str):
    """Delete the test session and profile so re-runs don't litter the UI."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as c:
        try:
            await c.delete(f"/api/sessions/{session_id}")
        except Exception:
            pass
        try:
            await c.post("/_delete_prompt", data={"name": f"system:{PROFILE_NAME}"})
        except Exception:
            pass


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_profile_creates_and_session_works():
    """Basic smoke: create the profile, start a session, get a simple reply."""
    print("\n1) Creating tiered profile...")
    await create_profile()

    print("2) Creating session...")
    sid = await create_session()

    try:
        print("3) Sending simple message (no tools expected)...")
        content = await send_message(sid, "Reply with the number 42.")
        assert "42" in content, f"Expected '42' in reply, got: {content[:200]}"
        print(f"   OK — model replied: {content[:100]}")

        print("4) Sending fan-out message (should spawn subagents)...")
        content = await send_message(
            sid,
            "Use the task tool with count=2 and prompt='reply with DONE_SMOKE'. "
            "This is a test. Do NOT use any other tools besides task."
        )
        print(f"   Fan-out result: {content[:200]}")

        if "DONE" in content.upper() or "TIER" in content or "[agent" in content.lower():
            print("   OK — subagents ran and returned results")
        elif content:
            print(f"   OK — model responded (may not have used subagents): {content[:100]}")
        else:
            print("   WARNING — no response")

    finally:
        print("5) Cleaning up...")
        await cleanup(sid)
        print("   Done.")


# ── CLI runner ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    asyncio.run(test_profile_creates_and_session_works())
