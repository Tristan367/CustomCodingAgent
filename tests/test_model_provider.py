"""A model implies its provider, and the two must never disagree.

The session creation form had a Model dropdown and no provider field, so every
session it made recorded `provider="deepseek"` from the database default.
Picking Claude Opus produced a session that sent `claude-opus-5` to
api.deepseek.com. There was no way to create an Anthropic or OpenRouter session
through the UI at all, which is why neither had ever been exercised.
"""

import pytest

from agent_server import database as db
from agent_server.config import MODELS, MODELS_BY_ID, provider_for_model, resolve_model_choice
from agent_server.models import SessionUpdate
from agent_server.providers import _providers, list_providers
from agent_server.providers.custom_openai import CustomOpenAIProvider
from agent_server.routes.sessions import _validate, update_session

# asyncio_mode is "auto", so async tests need no marker and sync ones must not
# carry a module-level asyncio mark.


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


def test_a_custom_endpoint_without_a_model_id_is_refused():
    """The endpoint's operator is the only one who knows what it serves, so an
    empty box has to be an error rather than a session that cannot run."""
    with pytest.raises(ValueError, match="model id"):
        resolve_model_choice("custom:box", "   ")


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


async def test_the_summariser_choice_is_no_longer_dropped(clean_db):
    """compact_profile was missing from SessionUpdate, so pydantic discarded it
    and the dropdown silently did nothing."""
    session = await db.create_session(name="s", project_dir=str(clean_db))
    assert "compact_profile" in SessionUpdate.model_fields

    await db.save_prompt("terse", "be terse", "compaction")
    await update_session(session["id"], SessionUpdate(compact_profile="terse"))

    assert (await db.get_session(session["id"]))["compact_profile"] == "terse"


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


def test_pricing_is_marked_unknown_rather_than_zero():
    """A custom endpoint can serve anything. Reporting $0.0000 as though it
    were measured is worse than saying the price is not known."""
    from agent_server.config import model_info

    assert model_info("deepseek-v4-pro")["priced"] is True
    assert model_info("qwen3-coder:30b")["priced"] is False


def test_the_custom_sentinel_is_gone():
    """`custom` was a fake row in MODELS with a fake context window and zero
    prices, so it validated as a real model id and could be sent to an API."""
    assert "custom" not in MODELS_BY_ID
