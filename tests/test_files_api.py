"""In-app file editor endpoints: list, read, and permission-gated save."""

from pathlib import Path

import pytest
from fastapi import HTTPException

from agent_server import database as db
from agent_server.formatting import formatter_for
from agent_server.routes.files import (
    FormatRequest,
    PathRequest,
    SaveRequest,
    format_file,
    list_dir,
    make_directory,
    read_file,
    save_file,
    stat_path,
)


@pytest.fixture
async def session(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.init_db()
    project = tmp_path / "proj"
    project.mkdir()
    s = await db.create_session(name="s", project_dir=str(project))
    yield s
    await db.close()


async def test_list_lists_one_level(session):
    project = Path(session["project_dir"])
    (project / "a.py").write_text("x=1\n")
    (project / "sub").mkdir()
    result = await list_dir(session["id"], "")
    names = {e["name"] for e in result["entries"]}
    assert names == {"a.py", "sub"}
    assert result["parent"] == str(project.parent)


async def test_stat_distinguishes_file_and_directory(session):
    project = Path(session["project_dir"])
    (project / "a.py").write_text("x=1\n")
    (project / "sub").mkdir()
    file = await stat_path(session["id"], "a.py")
    assert file["is_file"] is True and file["is_dir"] is False
    d = await stat_path(session["id"], "sub")
    assert d["is_dir"] is True and d["is_file"] is False
    missing = await stat_path(session["id"], "nope.txt")
    assert missing["exists"] is False


def test_formatter_for_maps_extensions():
    assert formatter_for("a.c") == "clang-format"
    assert formatter_for("a.java") == "clang-format"
    assert formatter_for("a.cs") == "clang-format"
    assert formatter_for("a.py") == "python"
    assert formatter_for("a.js") == "prettier"
    assert formatter_for("a.ts") == "prettier"
    assert formatter_for("a.css") == "prettier"
    assert formatter_for("a.html") == "prettier"
    assert formatter_for("a.json") == "json"
    assert formatter_for("a.txt") is None


async def test_format_json_through_route(session):
    result = await format_file(
        FormatRequest(session_id=session["id"], path="a.json", content='{"b":2,"a":[1,2,3]}')
    )
    assert result["content"] == '{\n  "b": 2,\n  "a": [\n    1,\n    2,\n    3\n  ]\n}\n'


async def test_format_rejects_invalid_json(session):
    with pytest.raises(HTTPException) as e:
        await format_file(FormatRequest(session_id=session["id"], path="a.json", content="{oops"))
    assert e.value.status_code == 400


async def test_format_no_formatter_is_an_error(session):
    with pytest.raises(HTTPException) as e:
        await format_file(FormatRequest(session_id=session["id"], path="a.xyz", content="x"))
    assert e.value.status_code == 400


async def test_read_returns_text_and_language(session):
    (Path(session["project_dir"]) / "a.py").write_text("print('hi')\n")
    result = await read_file(session["id"], "a.py")
    assert result["content"] == "print('hi')\n"
    assert result["lang"] == "python"


async def test_read_resolves_relative_against_project(session):
    (Path(session["project_dir"]) / "sub").mkdir()
    (Path(session["project_dir"]) / "sub" / "b.txt").write_text("hi")
    result = await read_file(session["id"], "sub/b.txt")
    assert result["content"] == "hi"


async def test_read_rejects_binary(session):
    (Path(session["project_dir"]) / "b.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(HTTPException) as e:
        await read_file(session["id"], "b.bin")
    assert e.value.status_code == 400


async def test_save_writes_inside_the_project(session):
    inside = Path(session["project_dir"]) / "new.py"
    result = await save_file(
        SaveRequest(session_id=session["id"], path=str(inside), content="y=2\n")
    )
    assert result["ok"] is True
    assert inside.read_text() == "y=2\n"


async def test_save_refuses_outside_the_project(session, tmp_path):
    outside = tmp_path / "outside.py"
    with pytest.raises(HTTPException) as e:
        await save_file(
            SaveRequest(session_id=session["id"], path=str(outside), content="nope")
        )
    assert e.value.status_code == 403
    assert not outside.exists()


async def test_save_preserves_crlf_and_bom(session):
    p = Path(session["project_dir"]) / "win.py"
    p.write_bytes(b"\xef\xbb\xbfa=1\r\n")
    await save_file(SaveRequest(session_id=session["id"], path=str(p), content="b=2\nc=3\n"))
    assert p.read_bytes() == b"\xef\xbb\xbfb=2\r\nc=3\r\n"


async def test_mkdir_creates_nested_directories(session):
    d = Path(session["project_dir"]) / "a" / "b"
    result = await make_directory(PathRequest(session_id=session["id"], path=str(d)))
    assert result["ok"] is True
    assert d.is_dir()


async def test_mkdir_refuses_outside_the_project(session, tmp_path):
    outside = tmp_path / "outside_dir"
    with pytest.raises(HTTPException) as e:
        await make_directory(PathRequest(session_id=session["id"], path=str(outside)))
    assert e.value.status_code == 403
    assert not outside.exists()


async def test_rename_entry(session):
    from agent_server.routes.files import RenameRequest, rename_entry

    p = Path(session["project_dir"]) / "old.txt"
    p.write_text("hi")
    result = await rename_entry(
        RenameRequest(session_id=session["id"], path=str(p), name="new.txt")
    )
    assert result["ok"] is True
    assert (Path(session["project_dir"]) / "new.txt").read_text() == "hi"
    assert not p.exists()


async def test_delete_entry(session):
    from agent_server.routes.files import delete_entry

    p = Path(session["project_dir"]) / "gone.txt"
    p.write_text("bye")
    assert (await delete_entry(PathRequest(session_id=session["id"], path=str(p))))["ok"]
    assert not p.exists()


async def test_move_entry(session):
    from agent_server.routes.files import MoveRequest, move_entries

    (Path(session["project_dir"]) / "sub").mkdir()
    p = Path(session["project_dir"]) / "m.txt"
    p.write_text("x")
    result = await move_entries(
        MoveRequest(session_id=session["id"], paths=[str(p)], dest=str(Path(session["project_dir"]) / "sub"))
    )
    assert result["ok"] is True
    assert result["paths"] == [str(Path(session["project_dir"]) / "sub" / "m.txt")]
    assert (Path(session["project_dir"]) / "sub" / "m.txt").exists()
    assert not p.exists()


async def test_copy_entry_duplicates(session):
    from agent_server.routes.files import copy_entry

    p = Path(session["project_dir"]) / "c.txt"
    p.write_text("data")
    result = await copy_entry(PathRequest(session_id=session["id"], path=str(p)))
    assert result["ok"] is True
    assert (Path(session["project_dir"]) / "c (copy).txt").read_text() == "data"


async def test_changes_aggregate_by_file_since_last_user_message(session):
    from agent_server.routes.sessions import session_changes

    await db.add_message(session["id"], "user", "go")
    await db.add_message(
        session["id"], "tool", "wrote", tool_name="write",
        diff="@@ -1 +1 @@\n-old\n+new\n", file_path="/p/a.py",
    )
    await db.add_message(
        session["id"], "tool", "edited", tool_name="edit",
        diff="@@ -1 +1 @@\n-x\n+y\n", file_path="/p/a.py",
    )
    await db.add_message(
        session["id"], "tool", "wrote b", tool_name="write",
        diff="@@ -1 +1 @@\n-a\n+b\n", file_path="/p/b.py",
    )

    result = await session_changes(session["id"])
    by_path = {f["path"]: f for f in result["files"]}
    assert result["added"] == 3
    assert result["removed"] == 3
    assert by_path["/p/a.py"]["added"] == 2
    assert by_path["/p/a.py"]["removed"] == 2
    assert len(by_path["/p/a.py"]["diffs"]) == 2
    assert by_path["/p/b.py"]["added"] == 1

    # A new user message resets the window: nothing has changed since it.
    await db.add_message(session["id"], "user", "go again")
    result = await session_changes(session["id"])
    assert result["files"] == []


async def test_changes_endpoint_404s_for_unknown_session(session):
    from agent_server.routes.sessions import session_changes

    with pytest.raises(HTTPException) as e:
        await session_changes("nope")
    assert e.value.status_code == 404

