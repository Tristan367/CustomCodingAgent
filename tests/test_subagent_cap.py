"""The parallel subagent cap: defaults, storage round-trip, and batching."""

import asyncio

import pytest

from agent_server import database as db
from agent_server.system_prompt import (
    SYSTEM,
    migrate_prompts,
    subagent_parallel_cap,
)
from agent_server.tools.base import ToolContext
from agent_server.tools.task import run_task


@pytest.fixture
async def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.init_db()
    await migrate_prompts()
    return str(tmp_path)


# ── Cap lookups ──────────────────────────────────────────────────────────────


async def test_main_profile_cap_defaults_to_unlimited(fresh):
    """A profile that has never set subagent_parallel_cap is unlimited (0)."""
    cap = await subagent_parallel_cap("default", tier=0)
    assert cap == 0


async def test_main_profile_cap_can_be_set(fresh):
    """Setting cap on the main profile returns the stored value."""
    await db._execute(
        "UPDATE prompts SET master_spawn_limit = 5 WHERE kind = ? AND name = ?",
        (SYSTEM, "default"),
    )
    cap = await subagent_parallel_cap("default", tier=0)
    assert cap == 5


async def test_tier_cap_defaults_to_three(fresh):
    """Tier 1 defaults to 3 when subagent_parallel_cap is NULL."""
    cap = await subagent_parallel_cap("default", tier=1)
    assert cap == 3


async def test_tier_cap_reads_from_json(fresh):
    """Tiers 2+ read parallel_cap from the stored JSON array (idx = tier - 2)."""
    import json
    await db._execute(
        "UPDATE prompts SET subagent_tiers = ? WHERE kind = ? AND name = ?",
        (json.dumps([{"body": "", "disabled_tools": "", "parallel_cap": 7}]),
         SYSTEM, "default"),
    )
    cap = await subagent_parallel_cap("default", tier=2)
    assert cap == 7


# ── Batching behaviour ───────────────────────────────────────────────────────


class _TrivialProvider:
    """Returns one content line and stops — one round per subagent."""
    def __init__(self):
        self.invocations = 0

    def has_credentials(self):
        return True

    def count_tokens(self, messages):
        return 1

    async def chat_completion(self, messages, tools, model, thinking_effort=None):
        self.invocations += 1
        await asyncio.sleep(0.01)  # let other tasks enter
        yield {"type": "content", "text": "ok"}
        await asyncio.sleep(0.01)  # let other tasks start before we finish
        yield {"type": "finish", "reason": "stop"}


async def test_count_within_cap_runs_in_parallel(fresh, monkeypatch):
    """When count <= cap all subagents run in one batch."""
    await db._execute(
        "UPDATE prompts SET master_spawn_limit = 3 WHERE kind = ? AND name = ?",
        (SYSTEM, "default"),
    )

    provider_class = _TrivialProvider
    monkeypatch.setattr("agent_server.providers.get_provider",
                        lambda _, p=provider_class: p())

    ctx = ToolContext(session_id="s", project_dir=fresh,
                      provider="deepseek", model="deepseek-v4-pro",
                      prompt_profile="default")
    result = await run_task(ctx, description="test", prompt="p", count=3)
    assert result.output
    assert "[agent 1]" in result.output
    assert "[agent 2]" in result.output
    assert "[agent 3]" in result.output


async def test_count_exceeds_cap_runs_in_batches(fresh, monkeypatch):
    """When count > cap subagents are batched sequentially."""
    await db._execute(
        "UPDATE prompts SET master_spawn_limit = 2 WHERE kind = ? AND name = ?",
        (SYSTEM, "default"),
    )

    state = {"in_flight": 0, "max_in_flight": 0}

    class BatchingProvider(_TrivialProvider):
        async def chat_completion(self, messages, tools, model, thinking_effort=None):
            state["in_flight"] += 1
            state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            try:
                async for ev in super().chat_completion(messages, tools, model, thinking_effort):
                    yield ev
            finally:
                state["in_flight"] -= 1

    monkeypatch.setattr("agent_server.providers.get_provider",
                        lambda _, p=BatchingProvider: p())

    ctx = ToolContext(session_id="s", project_dir=fresh,
                      provider="deepseek", model="deepseek-v4-pro",
                      prompt_profile="default")
    result = await run_task(ctx, description="test", prompt="p", count=5)
    assert result.output
    assert "[agent 1]" in result.output
    assert "[agent 5]" in result.output
    # Cap is 2 — at no point should more than 2 be in-flight.
    assert state["max_in_flight"] <= 2


async def test_cap_zero_is_unlimited(fresh, monkeypatch):
    """Cap of 0 means no batching — all run in parallel."""
    await db.save_prompt("default", "x", SYSTEM, subagent_parallel_cap=0)

    state = {"in_flight": 0, "max_in_flight": 0}

    class UProvider(_TrivialProvider):
        async def chat_completion(self, messages, tools, model, thinking_effort=None):
            state["in_flight"] += 1
            state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            try:
                async for ev in super().chat_completion(messages, tools, model, thinking_effort):
                    yield ev
            finally:
                state["in_flight"] -= 1

    monkeypatch.setattr("agent_server.providers.get_provider",
                        lambda _, p=UProvider: p())

    ctx = ToolContext(session_id="s", project_dir=fresh,
                      provider="deepseek", model="deepseek-v4-pro",
                      prompt_profile="default")
    result = await run_task(ctx, description="test", prompt="p", count=5)
    assert result.output
    # Cap is 0 (unlimited) — multiple tasks should overlap. The exact
    # count depends on event-loop scheduling; > 1 proves parallelism.
    assert state["max_in_flight"] >= 2
