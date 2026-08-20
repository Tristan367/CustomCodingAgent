"""A saved script can be bound to a key.

The shortcut table is a static list in app.js; scripts are rows in the database
that come and go. The seam is `GET /_settings/keybinds`, which hands back both
the overrides and the current script names so the table can build one bindable
action per script.

The deliberate omission here is a default combo. A shortcut nobody chose that
runs a shell script is not a feature, and there is no key worth guessing for a
script this code has never seen -- so a script does nothing until it is bound,
and binding it is the confirmation.
"""

import json
import re
from pathlib import Path

import pytest

from agent_server import database as db
from agent_server.routes.settings import get_keybinds, save_keybinds

APP_JS = Path(__file__).resolve().parent.parent / "web_ui" / "static" / "js" / "app.js"


@pytest.fixture
async def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.init_db()
    yield
    await db.close()


class _Req:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


# ── The seam ─────────────────────────────────────────────────────────────────

async def test_saved_scripts_come_back_with_the_keybinds(clean_db):
    await db.save_script("deploy", "echo deploying")
    await db.save_script("backup", "echo backing up")
    body = await get_keybinds()
    assert set(body["scripts"]) == {"deploy", "backup"}


async def test_no_scripts_is_an_empty_list_not_an_error(clean_db):
    body = await get_keybinds()
    assert body["scripts"] == []
    assert body["keybinds"] == {}


async def test_a_binding_for_a_script_round_trips(clean_db):
    await db.save_script("deploy", "echo hi")
    await save_keybinds(_Req({"script.deploy": "Alt+KeyG"}))
    body = await get_keybinds()
    assert body["keybinds"]["script.deploy"] == "Alt+KeyG"
    assert "deploy" in body["scripts"]


async def test_a_binding_survives_the_script_body_changing(clean_db):
    """Ids key on the name, so editing what a script does keeps its shortcut."""
    await db.save_script("deploy", "echo one")
    await save_keybinds(_Req({"script.deploy": "Alt+KeyG"}))
    await db.save_script("deploy", "echo two")
    body = await get_keybinds()
    assert body["keybinds"]["script.deploy"] == "Alt+KeyG"
    assert (await db.get_script("deploy"))["body"] == "echo two"


async def test_a_deleted_script_leaves_no_runnable_action(clean_db):
    """The stored override may linger, but the action is built from the script
    list, so there is nothing for the key to run."""
    await db.save_script("gone", "echo x")
    await save_keybinds(_Req({"script.gone": "Alt+KeyG"}))
    await db.delete_script("gone")
    body = await get_keybinds()
    assert "gone" not in body["scripts"]


# ── The front-end half ───────────────────────────────────────────────────────

def _js() -> str:
    return APP_JS.read_text(errors="replace")


def test_script_actions_ship_unbound():
    """The one property that keeps this safe: no script has a default key."""
    source = _js()
    block = re.search(r"function syncScripts\(names\) \{.*?\n  \}", source, re.S)
    assert block, "syncScripts is gone"
    assert re.search(r"combo:\s*''", block.group(0)), (
        "a script action has a default combo, so it would run on a key nobody chose"
    )


def test_script_actions_are_rebuilt_rather_than_accumulated():
    """A renamed or deleted script must not leave its action behind, or the
    shortcut list grows every time the page loads."""
    block = re.search(r"function syncScripts\(names\) \{.*?\n  \}", _js(), re.S).group(0)
    assert "splice" in block and "byId.delete" in block, (
        "syncScripts adds without removing, so old script actions persist"
    )


def test_the_page_shortcuts_exist_and_avoid_the_browser_menus():
    """Alt+T is the browser's own Tools menu, so the tools page cannot have it."""
    source = _js()
    assert "id: 'page.profiles'" in source
    assert "id: 'page.tools'" in source
    tools = re.search(r"\{ id: 'page\.tools'.*?\}", source, re.S).group(0)
    assert "'Alt+KeyT'" not in tools, "bound to the browser's Tools menu"
    reserved = re.search(r"const RESERVED = \{.*?\};", source, re.S).group(0)
    for action in ("page.profiles", "page.tools"):
        combo = re.search(rf"\{{ id: '{re.escape(action)}'.*?combo: '([^']+)'", source, re.S)
        assert combo, f"{action} has no combo"
        assert f"'{combo.group(1)}'" not in reserved, (
            f"{action} is bound to {combo.group(1)}, which the browser keeps"
        )


def test_running_a_script_from_a_key_reports_what_happened():
    """A script that fails silently on a keystroke is the worst version of this:
    nothing on screen connects the failure to the key that was pressed."""
    source = _js()
    body = re.search(r"async function runScriptFromKey\(name\) \{.*?\n\}", source, re.S)
    assert body, "runScriptFromKey is gone"
    text = body.group(0)
    assert "ui.alert" in text, "a failure is not surfaced"
    assert "showToast" in text, "a success is not surfaced"
    assert "/_run_script" in text


def test_the_keybind_fetch_reads_the_script_list():
    assert re.search(r"syncScripts\(body\.scripts\)", _js()), (
        "the shortcut table never learns which scripts exist"
    )


async def test_stored_overrides_are_json_and_bounded(clean_db):
    """Guarding the settings row: it is written straight from a request body,
    so a runaway or hostile payload must not become the stored table."""
    await save_keybinds(_Req({f"script.s{i}": "Alt+KeyG" for i in range(300)}))
    stored = json.loads(await db.get_setting("keybinds", ""))
    assert len(stored) <= 200, "the override table is unbounded"


async def test_a_non_object_body_is_refused(clean_db):
    assert (await save_keybinds(_Req(["not", "a", "dict"])))["ok"] is False
    assert await db.get_setting("keybinds", "") == ""
