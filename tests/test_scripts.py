"""Scripts: save, list, delete, run -- and never reach the model registry."""

import time

import pytest
from httpx import ASGITransport, AsyncClient

from agent_server import database as db
from agent_server.main import app
from agent_server.routes.context import _slug
from agent_server.tools.registry import TOOLS


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.close()
    await db.init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    await db.close()


@pytest.fixture
async def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.close()
    await db.init_db()
    yield tmp_path
    await db.close()


async def test_save_list_get_delete_round_trip(clean_db):
    # Start empty
    assert await db.list_scripts() == []

    await db.save_script("restart-dev", "echo restarting")
    scripts = await db.list_scripts()
    assert len(scripts) == 1
    assert scripts[0]["name"] == "restart-dev"
    assert scripts[0]["body"] == "echo restarting"

    s = await db.get_script("restart-dev")
    assert s is not None
    assert s["body"] == "echo restarting"

    # Upsert
    await db.save_script("restart-dev", "echo restarted")
    s = await db.get_script("restart-dev")
    assert s["body"] == "echo restarted"

    # Delete
    await db.delete_script("restart-dev")
    assert await db.list_scripts() == []
    assert await db.get_script("restart-dev") is None


def test_invalid_slug_is_rejected():
    assert _slug("") == ""
    assert _slug("!!!") == ""
    # A name that is not a valid slug produces an empty string, which the route
    # handler rejects as "Name is required".
    assert _slug("hello world") == "hello-world"
    assert _slug("Valid-Name") == "valid-name"


async def test_running_a_script_returns_its_stdout(client):
    await db.save_script("hello", "echo hello world")
    body = (await client.post("/_run_script", data={"name": "hello"})).text

    assert "hello world" in body
    assert "exit 0" in body


async def test_a_failing_script_looks_different_from_one_that_worked(client):
    """The whole point of the output panel. Same-looking failure is useless."""
    await db.save_script("failer", "echo on stdout; echo on stderr >&2; exit 3")
    body = (await client.post("/_run_script", data={"name": "failer"})).text

    assert "exit 3" in body
    assert "Failed" in body
    assert "on stdout" in body
    assert "on stderr" in body


async def test_a_script_that_backgrounds_something_returns_at_once(client):
    """`ollama serve &` is the motivating case.

    communicate() waits for the pipes to close, not for the shell to exit, so a
    daemon holding stdout open blocks for the full timeout and is then killed
    along with the shell -- the script would appear to hang and achieve nothing.
    """
    await db.save_script("daemon", 'sleep 30 & echo started')

    started = time.monotonic()
    body = (await client.post("/_run_script", data={"name": "daemon"})).text
    elapsed = time.monotonic() - started

    assert elapsed < 10, f"took {elapsed:.1f}s -- it waited on the background process"
    assert "started" in body
    assert "left something running" in body


async def test_the_posted_body_is_ignored_and_the_saved_script_runs(client):
    """Otherwise the endpoint is 'run arbitrary shell', and the confirmation
    dialog names a script that is not what executes."""
    await db.save_script("safe", "echo saved-version")
    body = (await client.post(
        "/_run_script", data={"name": "safe", "body": "echo INJECTED"}
    )).text

    assert "saved-version" in body
    assert "INJECTED" not in body


async def test_running_an_unsaved_name_does_nothing(client):
    body = (await client.post("/_run_script", data={"name": "nope"})).text
    assert "No saved script" in body


async def test_timeout_kills_the_script(client, monkeypatch):
    import agent_server.routes.scripts as scripts_route

    monkeypatch.setattr(scripts_route, "RUN_TIMEOUT_SEC", 1)
    await db.save_script("sleeper", "sleep 60")

    started = time.monotonic()
    body = (await client.post("/_run_script", data={"name": "sleeper"})).text
    elapsed = time.monotonic() - started

    assert "Timed out" in body
    assert elapsed < 15, f"took {elapsed:.1f}s -- the timeout did not apply"


async def test_saving_a_script_does_not_register_a_tool(clean_db):
    """Scripts must never reach the model."""
    await db.save_script("my-script", "echo hi")

    # The TOOLS registry must not contain this name
    assert "my-script" not in TOOLS

    # And it should not be in the custom tool registry either
    from agent_server.tools.registry import _custom_tool_names
    assert "my-script" not in _custom_tool_names


async def test_secrets_are_injected_into_the_script_environment(client):
    """Scripts see the same secret store the tools do, as env vars."""
    await db.save_secret("MY_KEY", "my-value")
    await db.save_script("envcheck", 'echo "MY_KEY=$MY_KEY"')
    body = (await client.post("/_run_script", data={"name": "envcheck"})).text
    assert "MY_KEY=my-value" in body


async def test_saving_a_secret_returns_to_the_back_page(client):
    """The scripts panel manages the same store and returns to itself."""
    resp = await client.post(
        "/_save_secret",
        data={"name": "FOO", "value": "bar", "back": "/?script=myscript"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?script=myscript"
    assert (await db.load_secrets_dict()).get("FOO") == "bar"
