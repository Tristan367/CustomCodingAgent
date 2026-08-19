"""Choosing a speech model says what it is doing.

The sizes on offer run to 3 GB. Fetching one silently and only finding out on
the first dictation -- with the mic held down -- is the worst version of this,
so selecting a model starts the fetch and the page polls for progress.

Nothing here touches the network: `prepare` is driven with the loader stubbed,
which is also the only way to observe the phases deterministically.
"""

import asyncio

import pytest

from agent_server import whisper_engine as we


@pytest.fixture(autouse=True)
def clean_state():
    """Each test starts with no engine and no preparation in flight."""
    we._engine = None
    we._prepare_task = None
    we._prepare.update({"model": "", "phase": "idle", "detail": ""})
    yield
    we._engine = None
    we._prepare_task = None


# ── Measuring what is on disk ────────────────────────────────────────────────

def test_bytes_on_disk_counts_a_tree(tmp_path):
    (tmp_path / "blobs").mkdir()
    (tmp_path / "blobs" / "a").write_bytes(b"x" * 1000)
    (tmp_path / "blobs" / "b.incomplete").write_bytes(b"y" * 500)
    assert we._bytes_on_disk(str(tmp_path)) == 1500


def test_bytes_on_disk_counts_a_partial_download(tmp_path):
    """The in-flight file is the whole point: without counting it the figure
    sits at the size of the config files until the model lands in one jump."""
    blob = tmp_path / "model.bin.incomplete"
    blob.write_bytes(b"z" * 4096)
    assert we._bytes_on_disk(str(tmp_path)) == 4096


def test_bytes_on_disk_is_zero_for_a_directory_that_is_not_there(tmp_path):
    assert we._bytes_on_disk(str(tmp_path / "nope")) == 0


def test_a_repo_dir_is_known_for_every_offered_size():
    """`large-v3-turbo` does not come from the same account as the rest, so a
    hardcoded naming pattern would report it missing forever."""
    for size in we.MODEL_SIZES:
        assert we._repo_dir(size), f"no cache directory known for {size}"


def test_every_offered_size_has_a_download_estimate():
    """The estimate is what the progress figure is measured against, and what
    the dropdown warns with."""
    for size in we.MODEL_SIZES:
        assert we.DOWNLOAD_MB.get(size), f"no size estimate for {size}"


# ── The phases the page renders ──────────────────────────────────────────────

async def test_a_model_already_here_goes_straight_to_loading(monkeypatch):
    monkeypatch.setattr(we, "downloaded_models", lambda: {"base.en"})
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_get_engine(name):
        started.set()
        await release.wait()
        engine = we.WhisperEngine(name)
        engine.device, engine.compute_type = "cuda", "float16"
        we._engine = engine
        return engine

    monkeypatch.setattr(we, "get_engine", fake_get_engine)
    await we.prepare("base.en")
    await asyncio.wait_for(started.wait(), 2)

    status = we.preparation_status()
    assert status["phase"] == "loading", "a cached model should not report a download"
    assert status["model"] == "base.en"

    release.set()
    await we._prepare_task
    done = we.preparation_status()
    assert done["phase"] == "ready"
    assert done["device"] == "cuda"
    assert done["compute_type"] == "float16"


async def test_a_model_that_is_not_here_reports_downloading(monkeypatch):
    monkeypatch.setattr(we, "downloaded_models", lambda: set())
    release = asyncio.Event()

    async def fake_get_engine(name):
        await release.wait()
        we._engine = we.WhisperEngine(name)
        return we._engine

    monkeypatch.setattr(we, "get_engine", fake_get_engine)
    await we.prepare("medium.en")
    status = we.preparation_status()
    assert status["phase"] == "downloading"
    assert status["total_mb"] == we.DOWNLOAD_MB["medium.en"]
    release.set()
    await we._prepare_task


async def test_progress_is_measured_from_the_cache(monkeypatch, tmp_path):
    """Reported from disk rather than from the downloader, so it stays honest
    about a resumed or partial transfer."""
    monkeypatch.setattr(we, "downloaded_models", lambda: set())
    monkeypatch.setattr(we, "_repo_dir", lambda size: str(tmp_path))
    (tmp_path / "part").write_bytes(b"x" * 30_000_000)
    release = asyncio.Event()

    async def fake_get_engine(name):
        await release.wait()
        return we.WhisperEngine(name)

    monkeypatch.setattr(we, "get_engine", fake_get_engine)
    await we.prepare("small.en")
    assert we.preparation_status()["downloaded_mb"] == 30
    release.set()
    await we._prepare_task


async def test_a_failure_is_reported_rather_than_swallowed(monkeypatch):
    monkeypatch.setattr(we, "downloaded_models", lambda: {"base.en"})

    async def boom(name):
        raise RuntimeError("no disk space")

    monkeypatch.setattr(we, "get_engine", boom)
    await we.prepare("base.en")
    await we._prepare_task
    status = we.preparation_status()
    assert status["phase"] == "error"
    assert "no disk space" in status["detail"]


async def test_a_fallback_to_the_default_is_admitted(monkeypatch):
    """`get_engine` falls back rather than leaving dictation dead. Reporting
    that as plain success would tell the user they got the model they picked."""
    monkeypatch.setattr(we, "downloaded_models", lambda: {"large-v3"})

    async def fallback(name):
        we._engine = we.WhisperEngine(we.DEFAULT_MODEL)
        return we._engine

    monkeypatch.setattr(we, "get_engine", fallback)
    await we.prepare("large-v3")
    await we._prepare_task
    status = we.preparation_status()
    assert status["phase"] == "ready"
    assert we.DEFAULT_MODEL in status["detail"]


async def test_asking_twice_does_not_start_a_second_download(monkeypatch):
    monkeypatch.setattr(we, "downloaded_models", lambda: set())
    calls = []
    release = asyncio.Event()

    async def fake_get_engine(name):
        calls.append(name)
        await release.wait()
        return we.WhisperEngine(name)

    monkeypatch.setattr(we, "get_engine", fake_get_engine)
    await we.prepare("medium.en")
    await we.prepare("medium.en")
    await asyncio.sleep(0)
    release.set()
    await we._prepare_task
    assert calls == ["medium.en"], f"started the same download {len(calls)} times"


# ── The route the page polls ─────────────────────────────────────────────────

async def test_the_status_route_returns_the_state(monkeypatch):
    from agent_server.routes.settings import stt_status

    monkeypatch.setattr(we, "downloaded_models", lambda: {"base.en"})

    async def fake_get_engine(name):
        return we.WhisperEngine(name)

    monkeypatch.setattr(we, "get_engine", fake_get_engine)
    await we.prepare("base.en")
    await we._prepare_task
    body = await stt_status()
    assert body["model"] == "base.en"
    assert body["phase"] == "ready"
    assert "total_mb" in body and "downloaded_mb" in body
