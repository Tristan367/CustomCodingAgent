# Task: give this app logging

There is no `import logging` anywhere. When something misbehaves the only
evidence is whatever uvicorn happens to print, and fourteen `except Exception:
pass` blocks throw away the error entirely. Fix that.

Run these throughout. Both must hold at the end, plus your new tests:

```
.venv/bin/python -m pytest tests/ -q     # currently 289 passed, 4 deselected
.venv/bin/python -m ruff check .         # currently All checks passed!
```

## 1. `agent_server/logging_setup.py`

One new module. It must export `configure()` and nothing else needs to be
public.

- `configure(level: str | None = None)` sets up the root logger **once** —
  calling it twice must not double every line. Guard with a module flag.
- Level from `CODEAGENT_LOG_LEVEL`, default `INFO`. Accept lowercase.
- Two handlers:
  - stderr, human format: `%(asctime)s %(levelname)-7s %(name)s: %(message)s`,
    time as `%H:%M:%S`.
  - a rotating file at `DATA_DIR / "codeagent.log"` — import `DATA_DIR` from
    `agent_server.config`, do not rebuild the path. Use
    `logging.handlers.RotatingFileHandler`, `maxBytes=5_000_000`,
    `backupCount=3`, `encoding="utf-8"`.
- Quieten the noisy third parties to WARNING: `httpx`, `httpcore`,
  `urllib3`, `asyncio`, `playwright`, `watchfiles`, `multipart`, `PIL`.
- Call `configure()` from the `lifespan` startup in `agent_server/main.py`,
  before anything else runs.

## 2. Every module gets a logger

`log = logging.getLogger(__name__)` at module level, in each file you touch.
Do not use the root logger directly and do not pass a hand-written name.

## 3. Replace the silent excepts

Fourteen `except Exception:` followed by `pass`, in these files:

```
agent_server/browser.py              6
agent_server/vision.py               2
agent_server/tools/skill.py          2
agent_server/providers/openai_compat.py  2
agent_server/permissions.py          2
agent_server/main.py                 2
```

For each one, **keep the swallowing behaviour** — the surrounding code is
written to continue, and changing that is not this task. Only record it:

```python
except Exception:
    log.debug("closing the browser context failed", exc_info=True)
```

Rules:
- `log.debug(...)` when the failure is genuinely expected and routine (a
  cleanup path, a probe that is allowed to fail).
- `log.warning(...)` when it is not expected but the app can carry on.
- The message says **what was being attempted**, in lower case, no trailing
  period, no f-string interpolation of the exception — `exc_info=True` carries
  it. Include an identifier where one is in scope, using `%s` lazy formatting:
  `log.warning("custom tool %s failed to load", name, exc_info=True)`.
- Judge each one on its surrounding code. Do not apply one level to all
  fourteen.

## 4. Replace the two `print()` calls

- `agent_server/main.py:61` → `log.warning("custom tool problem: %s", problem)`
- `agent_server/config.py:47` → this one runs at import time, before
  `configure()`. Leave it as a `print`, and add a one-line comment saying why.

## 5. Add real log lines where there are none

These are the events you would actually want at 2am. Add exactly these, no
more — do not litter the codebase:

- `agent.py`, at the start of a turn: `log.info("turn start session=%s model=%s", ...)`
- `agent.py`, at the end: `log.info("turn end session=%s outcome=%s tools=%d", ...)`
  (use the existing `outcome` variable)
- `agent.py`, when the doom detector aborts a turn: `log.warning(...)` with the
  session id
- `tools/registry.py` in `execute_tool`, when a tool returns `is_error`:
  `log.warning("tool %s failed: %s", name, result.output[:200])`
- `providers/base.py` or each provider's request path — wherever an API call
  raises — `log.warning("provider %s request failed", ..., exc_info=True)`.
  If there is no single place, put it in the one place each provider catches.

## Tests

`tests/test_logging.py`:

1. `configure()` twice produces one set of handlers, not two.
2. `CODEAGENT_LOG_LEVEL=DEBUG` is honoured, and lowercase `debug` also works.
3. The file handler writes to `DATA_DIR / "codeagent.log"` — monkeypatch
   `DATA_DIR` to `tmp_path` and assert the file appears after a log call.
4. A swallowed exception is recorded: use `caplog`, trigger one of the paths
   you changed, and assert something was logged at the level you chose. Pick a
   path you can trigger without a network or a browser.
5. Third-party loggers are at WARNING after `configure()`.

Use `caplog`, not string-matching on stderr.

## Definition of done

- `.venv/bin/python -m pytest tests/ -q` → 289 + yours, 0 failed
- `.venv/bin/python -m ruff check .` → All checks passed!
- `rg -c 'except Exception:\s*$' -A1 agent_server/ | grep pass` finds nothing
- Start the server, confirm startup lines appear on stderr **and** in
  `codeagent.log`:

```
mkdir -p /tmp/log-check
CODEAGENT_DATA_DIR=/tmp/log-check .venv/bin/python -m uvicorn agent_server.main:app --port 8297 &
sleep 4
curl -s -o /dev/null http://127.0.0.1:8297/
ls -la /tmp/log-check/codeagent.log && head -5 /tmp/log-check/codeagent.log
```

Kill the server afterwards.

Report: the level you chose for each of the fourteen and why, plus anything
that looked wrong but was out of scope.
