"""The file routes with no session: the home page's directory picker.

Every other caller of these routes is inside a session, and a session is what
bounds an agent's writes to its project directory plus whatever the user
granted. The picker runs before a session exists, and the whole point of it is
to go anywhere on the disk -- so that gate cannot apply, and something else has
to stop a mis-click from taking out a home directory.

These tests pin the something else.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException

from agent_server import database as db
from agent_server import permissions
from agent_server.routes.files import (
    MoveRequest,
    PathRequest,
    RenameRequest,
    SaveRequest,
    delete_entry,
    list_dir,
    make_directory,
    move_entries,
    rename_entry,
    save_file,
)


@pytest.fixture
async def db_only(tmp_path, monkeypatch):
    """A database with no session in it, which is the picker's whole situation."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.init_db()
    yield tmp_path
    await db.close()


# ── What the picker can do ───────────────────────────────────────────────────

async def test_it_lists_a_directory_with_no_session(db_only):
    (db_only / "proj").mkdir()
    (db_only / "proj" / "a.py").write_text("x=1\n")
    result = await list_dir("", str(db_only / "proj"))
    assert {e["name"] for e in result["entries"]} == {"a.py"}


async def test_it_creates_a_folder_anywhere_the_user_points_it(db_only):
    target = db_only / "brand-new"
    await make_directory(PathRequest(path=str(target)))
    assert target.is_dir()


async def test_it_creates_a_file(db_only):
    target = db_only / "notes.md"
    await save_file(SaveRequest(path=str(target), content="hello\n"))
    assert target.read_text() == "hello\n"


async def test_it_renames(db_only):
    (db_only / "old").mkdir()
    await rename_entry(RenameRequest(path=str(db_only / "old"), name="new"))
    assert (db_only / "new").is_dir()


async def test_it_moves(db_only):
    (db_only / "src").mkdir()
    (db_only / "dest").mkdir()
    (db_only / "src" / "f.txt").write_text("x")
    await move_entries(
        MoveRequest(paths=[str(db_only / "src" / "f.txt")], dest=str(db_only / "dest"))
    )
    assert (db_only / "dest" / "f.txt").read_text() == "x"


async def test_it_deletes_a_directory_tree(db_only):
    tree = db_only / "old-project"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f.txt").write_text("x")
    await delete_entry(PathRequest(path=str(tree)))
    assert not tree.exists()


# ── What it must refuse ──────────────────────────────────────────────────────

async def test_it_refuses_to_delete_the_home_directory(db_only, monkeypatch):
    """The one mis-click that would actually hurt: 'Use this directory' lands on
    ~, the user hits Delete instead."""
    home = db_only / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(HTTPException) as e:
        await delete_entry(PathRequest(path=str(home)))
    assert e.value.status_code == 403
    assert home.is_dir()


async def test_it_refuses_a_top_level_system_directory(db_only):
    with pytest.raises(HTTPException) as e:
        await delete_entry(PathRequest(path="/usr"))
    assert e.value.status_code == 403


async def test_it_refuses_the_filesystem_root(db_only):
    with pytest.raises(HTTPException) as e:
        await delete_entry(PathRequest(path="/"))
    assert e.value.status_code == 403


async def test_it_refuses_the_protected_prefixes(db_only):
    """The same list the agent's own writes are held to."""
    with pytest.raises(HTTPException) as e:
        await save_file(SaveRequest(path="/proc/self/mem", content="x"))
    assert e.value.status_code == 403


async def test_a_directory_inside_home_is_still_fine(db_only, monkeypatch):
    """The refusals above must not spill onto ordinary housekeeping, which is
    the entire reason the picker exists."""
    home = db_only / "home"
    (home / "old-project").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    await delete_entry(PathRequest(path=str(home / "old-project")))
    assert not (home / "old-project").exists()


# ── The session path is unchanged ────────────────────────────────────────────

async def test_a_session_id_that_does_not_exist_is_still_a_404(db_only):
    """An empty id means the picker; a wrong one means a bug, and must not be
    silently promoted to unrestricted access."""
    with pytest.raises(HTTPException) as e:
        await make_directory(
            PathRequest(session_id="nope", path=str(db_only / "x"))
        )
    assert e.value.status_code == 404
    assert not (db_only / "x").exists()


async def test_a_session_still_cannot_write_outside_its_project(db_only):
    project = db_only / "proj"
    project.mkdir()
    s = await db.create_session(name="s", project_dir=str(project))
    outside = db_only / "elsewhere"
    outside.mkdir()
    with pytest.raises(HTTPException) as e:
        await make_directory(
            PathRequest(session_id=s["id"], path=str(outside / "nope"))
        )
    assert e.value.status_code == 403


def test_human_write_allowed_covers_relative_paths_too():
    """`is_denied` resolves, so this must not be fooled by a path that only
    looks harmless before resolution."""
    assert not permissions.human_write_allowed(Path("/proc/../proc/self"))
