# Task: make `edit` and `write` safe on real-world files

Scope is `agent_server/tools/file_ops.py` plus a new test file. Do not touch
anything else. Do not reformat code you are not changing.

Three concrete defects. All three are reachable in this app today, because tools
run in parallel batches and `task` subagents run concurrently against the same
working tree.

Run these throughout:

```
.venv/bin/python -m pytest tests/ -q      # currently: 270 passed, 4 deselected
.venv/bin/python -m ruff check .          # currently: All checks passed!
```

Both must still hold at the end, plus your new tests.

---

## Defect 1 — concurrent writes to one file can interleave

`edit_file` and `write_file` do read-modify-write with `await` points in
between. Two calls in the same parallel batch, or a subagent and its parent, can
both read the old content and the second write silently discards the first.

Add a per-path async lock. Something equivalent to:

```python
_file_locks: dict[str, asyncio.Lock] = {}

def _lock_for(path: Path) -> asyncio.Lock:
    key = str(path.resolve())
    lock = _file_locks.get(key)
    if lock is None:
        lock = _file_locks.setdefault(key, asyncio.Lock())
    return lock
```

Hold it around the whole read-modify-write in **both** `edit_file` and
`write_file`. Key on the **resolved** path so `./a.txt` and `a.txt` are the same
lock. Do not hold it across the permission gate or any network call.

The dict grows one entry per file touched. That is acceptable; do not add
eviction, and do not use a `WeakValueDictionary` (the lock has no other
referent between calls and would be collected while still needed).

## Defect 2 — a UTF-8 BOM is destroyed

`path.read_text(encoding="utf-8")` returns a leading `\ufeff` as part of the
string; `write_text(..., encoding="utf-8")` then writes it back as literal bytes
only if it survived the edit. Any edit anchored near the top of the file can
drop it, which changes the file's bytes in a way that breaks tooling that
expects the BOM.

Detect a leading `\ufeff` on read, strip it before matching/editing, and
re-attach it on write. Applies to `edit_file` and `write_file` (a `write` to an
existing file that had a BOM should keep it).

## Defect 3 — CRLF files are rewritten as LF

Same two functions. `read_text` in text mode gives you `\n` regardless, and
`write_text` writes `\n`, so editing one line of a CRLF file rewrites every line
of it. The diff shown to the user is one line; the diff git sees is the whole
file.

Detect the dominant line ending on read (`\r\n` vs `\n`), normalise to `\n`
internally, and convert back on write. A file with mixed endings keeps whichever
is more common. An empty or single-line file with no ending defaults to `\n`.

Do the newline conversion with `open(..., newline="")` or by explicit
replacement — do **not** rely on `write_text`'s platform default, which would
also break on Windows.

---

## Tests

New file `tests/test_edit_safety.py`. Use `tmp_path`. Every test must fail
against the current implementation — check that by stashing your change and
re-running if you are unsure.

Cover at least:

1. CRLF file, edit one line → every other line still ends `\r\n`, and the byte
   count changes by only the edited line's delta.
2. LF file stays LF (no accidental CRLF).
3. BOM file, edit a line → file still starts with the BOM bytes `\xef\xbb\xbf`,
   exactly once.
4. File without a BOM never gains one.
5. `write_file` over an existing CRLF file with a BOM preserves both.
6. Two concurrent `edit_file` calls on one file, launched with
   `asyncio.gather`, both applying different non-overlapping edits → both edits
   are present afterwards. Without the lock this loses one of them. Make it
   deterministic (do not rely on timing) — if you cannot force interleaving,
   assert instead that the two calls serialise by checking both results
   succeeded and the final content contains both changes.
7. The lock is per path, not global: edits to two different files do not block
   each other. Assert on outcome, not on wall-clock timing.

Do not weaken an assertion to make a test pass. If a test reveals a real problem
you cannot fix inside this scope, leave it failing, mark it
`@pytest.mark.xfail(reason="…")`, and say so in your final message.

## Definition of done

- `.venv/bin/python -m pytest tests/ -q` → 270 + your new tests, 0 failed
- `.venv/bin/python -m ruff check .` → All checks passed!
- `git diff --stat` touches only `agent_server/tools/file_ops.py` and adds
  `tests/test_edit_safety.py`

Report: what you changed in each function, how you made the concurrency test
deterministic, and anything that looked wrong but was out of scope.
