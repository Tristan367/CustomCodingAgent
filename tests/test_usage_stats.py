"""Spend is counted for the month in progress, and can be cleared.

It used to be recomputed by scanning every usage record ever written, which
meant it could not be reset without deleting the per-message figures the
context ring is measured from, and it only ever grew.
"""

import pytest

from agent_server import database as db


@pytest.fixture
async def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.close()
    await db.init_db()
    s = await db.create_session(name="s", project_dir=str(tmp_path), model="deepseek-v4-pro")
    yield s["id"]
    await db.close()


async def spend(sid, prompt=10_000, cached=9_000, out=500):
    await db.add_message(sid, "assistant", "hi", usage={
        "prompt_tokens": prompt, "cached_tokens": cached, "completion_tokens": out,
    })


async def test_requests_accumulate_into_the_month(fresh):
    for _ in range(3):
        await spend(fresh)
    total = await db.get_month_usage()
    assert total["requests"] == 3
    assert total["input"] == 30_000
    assert total["cached"] == 27_000
    assert total["cache_hit_rate"] == 90.0
    assert total["cost"] > 0


async def test_a_previous_month_is_dropped_when_a_new_one_records(fresh):
    """Last month is not actionable and would accumulate forever."""
    await db._execute(
        "INSERT INTO usage_monthly (month, cost, requests) VALUES ('2000-01', 99.0, 500)"
    )
    await spend(fresh)
    months = [r["month"] for r in await db._fetchall("SELECT month FROM usage_monthly")]
    assert months == [db.current_month()]
    assert (await db.get_month_usage())["cost"] < 1.0


async def test_reset_zeroes_the_totals(fresh):
    await spend(fresh)
    await db.reset_usage()
    total = await db.get_month_usage()
    assert total["cost"] == 0.0
    assert total["requests"] == 0


async def test_reset_clears_session_spend_without_losing_the_context_ring(fresh):
    """The ring is measured from the same rows, so the reset is a cutoff."""
    await spend(fresh, prompt=50_000)
    before = await db.get_session_usage(fresh)
    assert before["cost"] > 0 and before["context"] == 50_000

    await db.reset_usage()
    after = await db.get_session_usage(fresh)
    assert after["cost"] == 0.0, "spend is cleared"
    assert after["context"] == 50_000, "context must survive the reset"


async def test_spending_resumes_after_a_reset(fresh):
    await spend(fresh)
    await db.reset_usage()
    await spend(fresh, prompt=1_000, cached=0, out=100)
    assert (await db.get_month_usage())["requests"] == 1
    assert (await db.get_session_usage(fresh))["cost"] > 0
