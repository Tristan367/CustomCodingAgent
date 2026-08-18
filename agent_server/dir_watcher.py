"""Detect when a session's project directory is renamed (inotify via watchfiles)."""

import asyncio
from pathlib import Path

from watchfiles import Change, awatch

# session_id -> task
_watchers: dict[str, asyncio.Task] = {}
# Callback invoked when a rename is detected
_on_rename: dict[str, callable] = {}


async def _watch(session_id: str, project_dir: str):
    """Watch a directory path. When it disappears, search parent for its rename."""
    path = Path(project_dir).expanduser().resolve()

    # Cache the inode before it disappears
    try:
        st = path.stat()
        cached_inode = (st.st_dev, st.st_ino)
    except OSError:
        return

    # Non-recursive on purpose: the only thing we care about is the directory
    # itself being renamed, not every file inside it. Watching recursively walks
    # the whole tree (a project with .git/.venv, or the default `~`, can be
    # hundreds of thousands of files) just to detect one top-level move, and
    # that walk was the expensive work that made creating a session feel slow.
    try:
        async for changes in awatch(str(path), recursive=False):
            for change_type, _changed_path in changes:
                if change_type not in (Change.deleted, Change.modified):
                    continue
                if path.exists():
                    continue  # still there, not renamed

                # Directory disappeared — look for it in the parent
                parent = path.parent
                if not parent.is_dir():
                    _remove(session_id)
                    return
                for entry in parent.iterdir():
                    if not entry.is_dir():
                        continue
                    try:
                        st2 = entry.stat()
                        if (st2.st_dev, st2.st_ino) == cached_inode:
                            cb = _on_rename.get(session_id)
                            if cb:
                                await cb(session_id, str(entry))
                            # Re-watch the new location
                            _watchers.pop(session_id, None)
                            _watchers[session_id] = asyncio.create_task(
                                _watch(session_id, str(entry))
                            )
                            return
                    except OSError:
                        continue
                # Not found — give up
                _remove(session_id)
                return
    except asyncio.CancelledError:
        pass
    except Exception:
        _remove(session_id)


def watch(session_id: str, project_dir: str, callback):
    """Start watching a session's project directory for renames."""
    _remove(session_id)
    _on_rename[session_id] = callback
    _watchers[session_id] = asyncio.create_task(_watch(session_id, project_dir))


def unwatch(session_id: str):
    """Stop watching and clean up."""
    _remove(session_id)


def _remove(session_id: str):
    _on_rename.pop(session_id, None)
    task = _watchers.pop(session_id, None)
    if task:
        task.cancel()
