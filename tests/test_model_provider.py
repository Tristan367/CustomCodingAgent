"""A model implies its provider, and the two must never disagree.

The session creation form had a Model dropdown and no provider field, so every
session it made recorded `provider="deepseek"` from the database default.
Picking Claude Opus produced a session that sent `claude-opus-5` to
api.deepseek.com. There was no way to create an Anthropic or OpenRouter session
through the UI at all, which is why neither had ever been exercised.
"""

import pytest

from agent_server import database as db
from agent_server.config import (
    DYNAMIC_DEEPSEEK_MODELS,
    MODELS,
    MODELS_BY_ID,
    dynamic_deepseek_models,
    is_known_model,
    provider_for_model,
    register_dynamic_deepseek_models,
    resolve_model_choice,
)
from agent_server.models import SessionUpdate
from agent_server.providers import _providers, list_providers
from agent_server.providers.custom_openai import CustomOpenAIProvider
from agent_server.routes.sessions import _validate, update_session

# asyncio_mode is "auto", so async tests need no marker and sync ones must not
# carry a module-level asyncio mark.


@pytest.fixture
def clean_dynamic_models():
    DYNAMIC_DEEPSEEK_MODELS.clear()
    yield
    DYNAMIC_DEEPSEEK_MODELS.clear()


@pytest.fixture
async def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.close()
    await db.init_db()
    yield tmp_path
    await db.close()


@pytest.fixture
def custom_endpoint():
    _providers["custom:box"] = CustomOpenAIProvider("box", "http://box:8000/v1", "sk-x")
    yield "custom:box"
    _providers.pop("custom:box", None)


def test_every_model_names_a_registered_provider():
    known = set(list_providers())
    for model in MODELS:
        assert model["provider"] in known, \
            f"{model['id']} is served by '{model['provider']}', which is not registered"


def test_the_provider_comes_from_the_model():
    assert provider_for_model("claude-opus-5") == "anthropic"
    assert provider_for_model("google/gemini-2.5-pro") == "openrouter"
    assert provider_for_model("deepseek-v4-pro") == "deepseek"


def test_picking_a_model_resolves_its_provider():
    for model in MODELS:
        provider, resolved = resolve_model_choice(model["id"])
        assert resolved == model["id"]
        assert provider == model["provider"]


def test_a_custom_endpoint_carries_its_own_model_id():
    provider, model = resolve_model_choice("custom:box", "qwen3-coder:30b")
    assert provider == "custom:box"
    assert model == "qwen3-coder:30b"


def test_a_custom_endpoint_needs_no_model_id():
    """It used to be refused: the operator was the only one who knew what the
    endpoint served. But that fact goes stale the moment a different model is
    loaded on the rig, and the endpoint can simply be asked -- so the session
    stores the endpoint key and the real id is resolved per request."""
    provider, model = resolve_model_choice("custom:box", "   ")
    assert (provider, model) == ("custom:box", "custom:box")


async def test_the_endpoint_is_asked_what_it_is_running():
    """In order of how reliable the signal is: a `loaded` flag if the server
    offers one, the only entry if it serves one model, otherwise the first of
    several -- which is a guess, and why the model field stays as an override."""
    import types

    from agent_server.providers.custom_openai import CustomOpenAIProvider

    def entry(name, **kw):
        e = types.SimpleNamespace(id=name)
        for k, v in kw.items():
            setattr(e, k, v)
        return e

    async def resolved(entries):
        provider = CustomOpenAIProvider(name="x", base_url="http://x/v1", api_key="k")

        async def listing():
            return types.SimpleNamespace(data=entries)

        provider._get_client = lambda: types.SimpleNamespace(
            models=types.SimpleNamespace(list=listing)
        )
        return await provider.resolve_model()

    # A server hosting a library and keeping one in memory.
    assert await resolved(
        [entry("an-image-model"), entry("the-live-one", loaded=True), entry("another")]
    ) == "the-live-one"
    # vLLM, llama.cpp, TGI: one model, listed.
    assert await resolved([entry("Qwen2.5-Coder-32B")]) == "Qwen2.5-Coder-32B"
    # Nothing to go on: valid, but a guess.
    assert await resolved([entry("llama3"), entry("qwen")]) == "llama3"
    # An endpoint that answers with nothing must not invent an id.
    assert await resolved([]) == ""


async def test_a_context_window_the_endpoint_reports_is_used():
    """A rig serving a 262K model would otherwise be sized by the unknown-model
    default of 131K, and compact at half the window it actually has."""
    import types

    from agent_server.config import model_info
    from agent_server.providers.custom_openai import CustomOpenAIProvider

    provider = CustomOpenAIProvider(name="rig", base_url="http://x/v1", api_key="k")

    async def listing():
        return types.SimpleNamespace(
            data=[types.SimpleNamespace(id="big", loaded=True, context_length=262144)]
        )

    provider._get_client = lambda: types.SimpleNamespace(
        models=types.SimpleNamespace(list=listing)
    )
    await provider.resolve_model()
    assert model_info("custom:rig")["context"] == 262144


def test_an_unknown_model_is_refused():
    with pytest.raises(ValueError, match="Unknown model"):
        resolve_model_choice("gpt-9-ultra")


async def test_a_mismatched_pair_is_rejected(clean_db):
    """Belt and braces for the API, which takes both fields separately."""
    from fastapi import HTTPException

    body = SessionUpdate(model="claude-opus-5", provider="deepseek")
    with pytest.raises(HTTPException) as caught:
        await _validate(body)
    assert "served by anthropic" in caught.value.detail


async def test_switching_model_switches_provider(clean_db):
    """The settings form only sends a model. The provider has to follow it, or
    the session keeps asking the old vendor for the new model."""
    session = await db.create_session(
        name="s", project_dir=str(clean_db), provider="deepseek", model="deepseek-v4-pro"
    )
    await update_session(session["id"], SessionUpdate(model="claude-opus-5"))

    updated = await db.get_session(session["id"])
    assert updated["provider"] == "anthropic"
    assert updated["model"] == "claude-opus-5"


async def test_compaction_follows_profile_not_legacy_column(clean_db):
    """compact_profile was dropped from SessionUpdate — the compaction prompt
    is now always the one bundled with the prompt_profile, so there is nothing
    to configure separately."""
    session = await db.create_session(name="s", project_dir=str(clean_db))
    assert "compact_profile" not in SessionUpdate.model_fields, \
        "compact_profile was removed from SessionUpdate — profiles bundle it now"

    # Setting prompt_profile alone should still work fine.
    await db.save_prompt("newbie", "new prompt", "system")
    await update_session(session["id"], SessionUpdate(prompt_profile="newbie"))
    assert (await db.get_session(session["id"]))["prompt_profile"] == "newbie"


async def test_a_custom_endpoints_model_passes_validation(clean_db, custom_endpoint):
    """Its ids cannot be checked against the built-in table -- the endpoint
    serves whatever its operator configured."""
    await _validate(SessionUpdate(provider="custom:box", model="qwen3-coder:30b"))


async def test_an_unknown_model_on_a_known_provider_is_refused(clean_db):
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await _validate(SessionUpdate(provider="deepseek", model="qwen3-coder:30b"))


def test_a_custom_endpoint_authenticates_with_its_own_key(custom_endpoint):
    """Its key lives in the custom_endpoints table, not in `settings`. The base
    class looked the key up under the empty string, cached '', and reported no
    credentials forever."""
    provider = _providers[custom_endpoint]
    assert provider.api_key() == "sk-x"
    assert provider.has_credentials()


def test_a_keyless_local_endpoint_still_counts_as_configured():
    """Ollama and vLLM have no key to give; a URL is the whole configuration."""
    provider = CustomOpenAIProvider("local", "http://127.0.0.1:11434/v1", "")
    assert provider.has_credentials()


def test_an_unknown_custom_endpoint_does_not_fall_back_to_deepseek():
    """It used to, which sent the conversation to another vendor on another
    key and billed it, with nothing said."""
    from agent_server.providers import get_provider

    with pytest.raises(ValueError, match="No custom endpoint named 'gone'"):
        get_provider("custom:gone")


def test_the_custom_sentinel_is_gone():
    """`custom` was a fake row in MODELS with a fake context window, so it
    validated as a real model id and could be sent to an API."""
    assert "custom" not in MODELS_BY_ID


def test_dynamic_models_are_registered_and_deduplicated(clean_dynamic_models):
    """Discovery merges new ids, skips the hand-listed ones and repeats."""
    register_dynamic_deepseek_models(
        ["deepseek-v4-pro", "deepseek-v4-pro-0813", "deepseek-v4-pro-0813", ""]
    )
    assert DYNAMIC_DEEPSEEK_MODELS == ["deepseek-v4-pro-0813"]


def test_dynamic_models_resolve_to_deepseek(clean_dynamic_models):
    register_dynamic_deepseek_models(["deepseek-v4-pro-0813"])
    assert is_known_model("deepseek-v4-pro-0813")
    assert provider_for_model("deepseek-v4-pro-0813") == "deepseek"
    provider, model = resolve_model_choice("deepseek-v4-pro-0813")
    assert (provider, model) == ("deepseek", "deepseek-v4-pro-0813")


def test_dynamic_models_are_humanised(clean_dynamic_models):
    register_dynamic_deepseek_models(["deepseek-v4-flash-0731"])
    entries = dynamic_deepseek_models()
    assert entries == [
        {"id": "deepseek-v4-flash-0731", "name": "DeepSeek V4 Flash 0731", "provider": "deepseek"}
    ]


async def test_a_discovered_model_passes_validation(clean_db, clean_dynamic_models):
    register_dynamic_deepseek_models(["deepseek-v4-pro-0813"])
    await _validate(SessionUpdate(model="deepseek-v4-pro-0813", provider="deepseek"))


async def test_a_discovered_model_still_refuses_a_mismatched_provider(
    clean_db, clean_dynamic_models
):
    from fastapi import HTTPException

    register_dynamic_deepseek_models(["deepseek-v4-pro-0813"])
    with pytest.raises(HTTPException, match="served by deepseek"):
        await _validate(SessionUpdate(model="deepseek-v4-pro-0813", provider="anthropic"))


def test_the_compaction_threshold_leaves_room_for_one_more_round():
    """What matters is the headroom above the threshold, not the threshold.

    The check runs at a round boundary and the request goes out straight after,
    so the window must still hold the model's output plus that round's tool
    results. Both are bounded -- `max_output` by the model, each tool result by
    MAX_TOOL_RESULT_CHARS -- so the reserve is computed rather than assumed.

    A flat 75% was wrong in both directions. DeepSeek's 8K output ceiling left
    140K of a 1M window unused; Haiku's 64K ceiling in a 200K window left 50K of
    headroom for a reply that can be 64K on its own.
    """
    from agent_server.config import compaction_reserve, default_compact_threshold

    for context, output in ((1_000_000, 8_192), (1_000_000, 128_000),
                            (200_000, 64_000), (131_072, 8_192)):
        threshold = default_compact_threshold(context, output)
        headroom = context - threshold
        assert headroom >= output, (
            f"{context:,}/{output:,}: a single maximum-length reply ({output:,}) "
            f"does not fit in {headroom:,} of headroom"
        )
        assert headroom >= compaction_reserve(context, output)
        assert threshold > 0


def test_a_bigger_output_ceiling_means_an_earlier_threshold():
    """The two models differ only in how much they can say in one go."""
    from agent_server.config import default_compact_threshold

    assert default_compact_threshold(1_000_000, 8_192) > default_compact_threshold(
        1_000_000, 128_000)


def test_the_threshold_never_runs_past_the_ceiling():
    from agent_server.config import COMPACT_CEILING_RATIO, default_compact_threshold

    for context in (32_768, 200_000, 1_000_000):
        assert default_compact_threshold(context, 0) <= int(context * COMPACT_CEILING_RATIO)


def test_a_tiny_window_still_gets_a_usable_threshold():
    """A model too small to reserve for must still compact rather than get a
    threshold of zero and never fire."""
    from agent_server.config import MIN_COMPACT_THRESHOLD, default_compact_threshold

    assert default_compact_threshold(8_192, 8_192) >= MIN_COMPACT_THRESHOLD


# ── Gemini, and the console link every provider now carries ──────────────────

def test_gemini_is_registered_and_reachable_by_its_models():
    """Direct Gemini, not the OpenRouter passthrough: the Flash models have a
    free tier on a Google key, which is what makes them worth a second route."""
    assert "gemini" in list_providers()
    gemini_models = [m for m in MODELS if m["provider"] == "gemini"]
    assert gemini_models, "no model routes to the gemini provider"
    for model in gemini_models:
        assert provider_for_model(model["id"]) == "gemini"


def test_gemini_keeps_the_openrouter_route_separate():
    """`google/gemini-2.5-pro` goes through OpenRouter and must not be
    reassigned; one key for everything is still a valid way to run this."""
    assert provider_for_model("google/gemini-2.5-pro") == "openrouter"


def test_gemini_drops_stream_options():
    """Google's compatibility layer rejects it on some models rather than
    ignoring it, which fails the whole request rather than one field."""
    from agent_server.providers.gemini import GeminiProvider

    kwargs = GeminiProvider()._build_kwargs([{"role": "user", "content": "hi"}], [], "gemini-3.7-flash")
    assert "stream_options" not in kwargs


def test_gemini_strips_the_models_prefix_from_discovered_ids():
    """Ids come back as "models/gemini-3.7-flash"; the chat endpoint 404s on
    the prefixed form."""
    from agent_server.providers.gemini import GeminiProvider

    rows = {"data": [{"id": "models/gemini-3.7-flash"}, {"id": "gemini-3.5-flash-lite"}]}
    ids = [
        str(r["id"]).removeprefix("models/") for r in rows["data"]
    ]
    assert ids == ["gemini-3.7-flash", "gemini-3.5-flash-lite"]
    assert GeminiProvider().base_url.endswith("/"), "fetch_model_ids appends 'models' directly"


def test_every_provider_says_where_to_get_a_key():
    """The home page renders this link from the provider class. It used to be an
    if/elif chain over provider names in the template, so a new provider
    rendered `get a key @` with no destination."""
    from agent_server.providers import get_provider_settings_fields

    for entry in get_provider_settings_fields():
        assert entry["console_url"], f"{entry['name']} has no console_url"
        assert not entry["console_url"].startswith("http"), "the template adds the scheme"
