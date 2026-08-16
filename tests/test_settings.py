"""Settings-save semantics: clearing one key must not touch the others."""

import pytest

from agent_server import database as db
from agent_server.routes import settings as settings_mod

pytestmark = pytest.mark.asyncio


def _request(form_dict):
    class _Form:
        def __contains__(self, key):
            return key in form_dict

        def get(self, key, default=""):
            return form_dict.get(key, default)

    class _Request:
        async def form(self):
            return _Form()

    return _Request()


async def test_saving_settings_clears_only_the_submitted_key(tmp_path, monkeypatch):
    """Each provider's form posts only its own field, so an emptied field clears
    exactly that key; the other providers' keys are absent and must survive."""
    calls = {}

    async def fake_set(key, value):
        calls[key] = value

    monkeypatch.setattr(db, "set_setting", fake_set)

    async def _fake_home():
        return {}

    monkeypatch.setattr(settings_mod, "_home_context", _fake_home)

    class _Provider:
        def invalidate_key_cache(self):
            pass

    monkeypatch.setattr(settings_mod, "get_provider", lambda key: _Provider())

    def _fake_response(**kwargs):
        return kwargs

    monkeypatch.setattr(settings_mod.templates, "TemplateResponse", _fake_response)

    await settings_mod.save_settings(_request({"deepseek_api_key": ""}))

    assert calls.get("deepseek_api_key") == "", "an emptied key is a deliberate clear"
    assert "anthropic_api_key" not in calls, "an absent key must not be touched"
    assert "openrouter_api_key" not in calls
