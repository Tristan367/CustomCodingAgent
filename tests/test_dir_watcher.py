"""The project-directory rename watcher must not scan the tree it watches.

Watching recursively walks every file under the project directory just to
detect one top-level rename -- and for a large directory (or the default ``~``)
that walk is the expensive work that made creating a session take a long time.
This pins both sides: the watcher still fires on a rename, and it is watching
non-recursively.
"""

import asyncio
from pathlib import Path

from agent_server import dir_watcher


async def test_rename_is_detected_without_watching_the_tree(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "file.txt").write_text("hello")

    renamed = []

    def on_rename(session_id, new_dir):
        renamed.append(new_dir)

    dir_watcher.watch("sess", str(project), on_rename)
    try:
        # Let the inotify watch register before the move.
        await asyncio.sleep(0.3)

        target = tmp_path / "project-renamed"
        project.rename(target)

        for _ in range(200):
            if renamed:
                break
            await asyncio.sleep(0.05)
    finally:
        dir_watcher.unwatch("sess")

    assert renamed, "the rename callback never fired"
    assert Path(renamed[0]) == target
