"""Only the tail of a long transcript is drawn; the rest is a click away.

Switching sessions re-renders the whole transcript, and this app expects a user
to have several sessions open and move between them constantly -- inter-session
messaging and subagent hierarchies make that the normal way to work. Drawing a
thousand messages every time, for scrollback nobody was reading, made the switch
cost about half a second.

The line this must never cross: it bounds what is *drawn*, never what is *sent*.
The model's view is assembled from `get_messages` in the conversation layer and
is not touched by any of this. The last test here is the one that says so.
"""

from types import SimpleNamespace

import pytest

from agent_server import database as db
from agent_server.routes import context as ctx
from agent_server.routes.pages import earlier_messages


@pytest.fixture
async def session(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.init_db()
    s = await db.create_session(name="w", project_dir=str(tmp_path))
    yield s
    await db.close()


def _request():
    """The bit of a Request the template response actually touches."""
    return SimpleNamespace(headers={"HX-Request": "true"}, scope={"type": "http"})


async def _fill(session_id: str, n: int) -> list[int]:
    ids = []
    for i in range(n):
        row = await db.add_message(session_id, "user", f"message {i}")
        ids.append(row["id"])
    return ids


# ── The window ───────────────────────────────────────────────────────────────

async def test_a_short_transcript_is_drawn_whole(session):
    await _fill(session["id"], 10)
    context = await ctx._session_context(session)
    assert len(context["messages"]) == 10
    assert context["older_count"] == 0, "a short session must not offer to load more"


async def test_a_long_transcript_is_cut_to_the_window(session, monkeypatch):
    monkeypatch.setattr(ctx, "TRANSCRIPT_WINDOW", 20)
    ids = await _fill(session["id"], 75)
    context = await ctx._session_context(session)
    assert len(context["messages"]) == 20
    assert context["older_count"] == 55
    assert context["oldest_id"] == ids[-20]


async def test_the_window_keeps_the_newest_messages(session, monkeypatch):
    """The tail, not the head: the useful end of a conversation is the recent
    end, and the composer sits under it."""
    monkeypatch.setattr(ctx, "TRANSCRIPT_WINDOW", 5)
    await _fill(session["id"], 30)
    context = await ctx._session_context(session)
    bodies = [m["content"] for m in context["messages"]]
    assert bodies == [f"message {i}" for i in range(25, 30)]


async def test_an_empty_session_offers_nothing_to_load(session):
    context = await ctx._session_context(session)
    assert context["messages"] == []
    assert context["older_count"] == 0
    assert context["oldest_id"] == 0


# ── Walking backwards ────────────────────────────────────────────────────────

async def test_earlier_returns_the_batch_before_a_point(session):
    ids = await _fill(session["id"], 50)
    rows = await db.get_messages_before(session["id"], ids[30], 10)
    assert [r["content"] for r in rows] == [f"message {i}" for i in range(20, 30)]


async def test_earlier_batches_tile_without_gaps_or_repeats(session, monkeypatch):
    """Walking back to the start must land on every message exactly once."""
    monkeypatch.setattr(ctx, "TRANSCRIPT_WINDOW", 10)
    await _fill(session["id"], 44)
    context = await ctx._session_context(session)
    seen = [m["id"] for m in context["messages"]]
    cursor = context["oldest_id"]
    while True:
        batch = await db.get_messages_before(session["id"], cursor, 10)
        if not batch:
            break
        seen = [r["id"] for r in batch] + seen
        cursor = batch[0]["id"]
    assert len(seen) == 44
    assert seen == sorted(seen), "batches came back out of order"
    assert len(set(seen)) == 44, "a message was rendered twice"


async def test_the_count_of_older_messages_shrinks_as_you_walk_back(session, monkeypatch):
    monkeypatch.setattr(ctx, "TRANSCRIPT_WINDOW", 10)
    await _fill(session["id"], 35)
    context = await ctx._session_context(session)
    assert context["older_count"] == 25
    batch = await db.get_messages_before(session["id"], context["oldest_id"], 10)
    assert await db.count_messages_before(session["id"], batch[0]["id"]) == 15


async def test_the_earlier_route_renders_and_reports_what_is_left(session, monkeypatch):
    monkeypatch.setattr(ctx, "TRANSCRIPT_WINDOW", 10)
    ids = await _fill(session["id"], 40)

    response = await earlier_messages(_request(), session["id"], before=ids[20], limit=10)
    body = response.body.decode()
    for i in range(10, 20):
        assert f"message {i}" in body
    assert "message 20" not in body, "the batch overlapped the messages already shown"
    assert "load-earlier" in body, "the response should say how far back it still goes"


async def test_the_earlier_route_stops_at_the_start(session):
    ids = await _fill(session["id"], 12)

    response = await earlier_messages(_request(), session["id"], before=ids[3], limit=50)
    body = response.body.decode()
    assert "message 0" in body
    assert "load-earlier" not in body, "offered to load more when there is nothing older"


# ── The line this must not cross ─────────────────────────────────────────────

async def test_windowing_the_view_does_not_narrow_what_the_model_sees(session, monkeypatch):
    """The whole risk of this feature in one test.

    `get_messages` is what the conversation layer builds the request from. If
    windowing ever reached it, the agent would quietly forget the start of every
    long session and there would be nothing on screen to say so.
    """
    monkeypatch.setattr(ctx, "TRANSCRIPT_WINDOW", 5)
    await _fill(session["id"], 60)

    drawn = await ctx._session_context(session)
    assert len(drawn["messages"]) == 5

    for_the_model = await db.get_messages(session["id"])
    assert len(for_the_model) == 60

    from agent_server.conversation import build_messages

    built = build_messages("system prompt", [], for_the_model)
    assert len(built) == 61, "the model's view must still hold the whole conversation"
    assert built[1]["content"] == "message 0"
